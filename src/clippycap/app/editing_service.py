"""Clip editing: trim a clip, remove a segment, extract a segment to a new clip.

Every operation needs ffmpeg (:attr:`EditingService.available`). *Trim* / *remove-segment* / *cut*
overwrite the asset's file in place -- the asset is then re-hashed and its metadata, thumbnail and
timestamped/interval notes are updated to the new timeline (there is no undo; ``[editing].
keep_original_backup`` keeps a copy of the pre-edit file first). *Extract* writes a new file in the
same directory, registers it as a new asset, and (when a reference type is configured) links it to
the source as an excerpt.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from clippycap.core.entities import Asset, Note, Reference
from clippycap.core.errors import ConflictError, InvalidInputError, NotFoundError, UnsupportedError
from clippycap.core.events import AssetUpdated, EventBus
from clippycap.core.ports import (
    Database,
    IdentityStrategy,
    KeptSegment,
    MediaTypeProvider,
    UnitOfWork,
    VideoEditor,
)
from clippycap.infra.config import Config, ConfigHolder
from clippycap.infra.media.video_thumbnail import purge_asset_thumbnails

_log = logging.getLogger(__name__)
_EditMode = Literal["keep", "remove"]
_TMP_SUFFIX = ".clippycap-tmp"
_REPLACE_ATTEMPTS = 8       # the destination is often momentarily locked on Windows (it's being streamed)
_REPLACE_DELAY_S = 0.25


class EditingService:
    def __init__(
        self,
        database: Database,
        video_editor: VideoEditor,
        media_types: dict[str, MediaTypeProvider],
        identity_strategies: dict[str, IdentityStrategy],
        event_bus: EventBus,
        config_holder: ConfigHolder,
        thumbnail_dir: Path,
    ) -> None:
        self._db = database
        self._editor = video_editor
        self._media_types = media_types
        self._identity = identity_strategies
        self._bus = event_bus
        self._config_holder = config_holder        # read [editing] / [thumbnails] live (see ConfigHolder)
        self._thumb_dir = thumbnail_dir

    @property
    def _config(self) -> Config:
        return self._config_holder.current

    @property
    def available(self) -> bool:
        return self._editor.available

    # ------------------------------------------------------------------ public ops

    def trim(self, asset_id: int, *, start_ms: int, end_ms: int) -> Asset:
        """Keep only ``[start_ms, end_ms]`` of the clip (overwrites the file)."""
        return self._edit_in_place(asset_id, start_ms, end_ms, mode="keep")

    def remove_segment(self, asset_id: int, *, start_ms: int, end_ms: int) -> Asset:
        """Cut ``[start_ms, end_ms]`` out of the clip, keeping the rest (overwrites the file)."""
        return self._edit_in_place(asset_id, start_ms, end_ms, mode="remove")

    def extract_segment(
        self, asset_id: int, *, start_ms: int, end_ms: int, remove_from_source: bool
    ) -> Asset:
        """Save ``[start_ms, end_ms]`` as a new clip; if ``remove_from_source`` also cut it from the original."""
        self._require_available()
        self._validate_range(start_ms, end_ms)
        with self._db.transaction() as uow:
            asset = self._get(uow, asset_id)
            source_path = self._on_disk_path(uow, asset_id)
            provider = self._provider(asset.media_type)
            out_path = self._unused_name(source_path, start_ms, end_ms)
            segments = self._editor.keep_range(source_path, out_path, start_ms=start_ms, end_ms=end_ms)
            if segments is None:
                out_path.unlink(missing_ok=True)
                raise UnsupportedError("the video edit failed -- see the logs")
            new_asset, created = self._register_file(uow, out_path, asset.media_type, provider)
            if created and new_asset.id is not None:
                self._copy_notes_into_extract(
                    uow, source_id=asset_id, new_id=new_asset.id, segments=segments
                )
            self._link_excerpt(uow, new_asset, asset, start_ms)
        if remove_from_source:
            self.remove_segment(asset_id, start_ms=start_ms, end_ms=end_ms)
        return new_asset

    # ------------------------------------------------------------------ internals

    def _require_available(self) -> None:
        if not self._editor.available:
            raise UnsupportedError("clip editing requires ffmpeg, which is not configured")

    def _validate_range(self, start_ms: int, end_ms: int) -> None:
        if start_ms < 0 or end_ms <= start_ms:
            raise InvalidInputError("require 0 <= start_ms < end_ms")

    def _get(self, uow: UnitOfWork, asset_id: int) -> Asset:
        asset = uow.assets.get(asset_id)
        if asset is None:
            raise NotFoundError(f"no asset with id {asset_id!r}")
        return asset

    def _provider(self, media_type: str) -> MediaTypeProvider:
        provider = self._media_types.get(media_type)
        if provider is None:
            raise UnsupportedError(f"no handler registered for media type {media_type!r}")
        return provider

    def _strategy(self, name: str) -> IdentityStrategy:
        strategy = self._identity.get(name)
        if strategy is None:
            raise UnsupportedError(f"unknown identity strategy {name!r}")
        return strategy

    def _on_disk_path(self, uow: UnitOfWork, asset_id: int) -> Path:
        for entry in uow.assets.get_paths(asset_id):
            candidate = Path(entry.path)
            if entry.present and candidate.is_file():
                return candidate
        raise UnsupportedError("this asset has no readable file on disk to edit")

    def _replace_locked(self, src: Path, dst: Path) -> None:
        """``src.replace(dst)``, retrying briefly: on Windows ``dst`` is often momentarily open
        (e.g. the player is streaming it). Cleans up ``src`` and raises if it stays locked."""
        last: OSError | None = None
        for _ in range(_REPLACE_ATTEMPTS):
            try:
                src.replace(dst)
                return
            except OSError as exc:
                last = exc
                time.sleep(_REPLACE_DELAY_S)
        src.unlink(missing_ok=True)
        raise UnsupportedError(
            f"couldn't overwrite {dst.name!r} -- it is in use (the clip may still be streaming to "
            "the player); pause/close the clip and try the edit again"
        ) from last

    def _edit_in_place(self, asset_id: int, start_ms: int, end_ms: int, *, mode: _EditMode) -> Asset:
        self._require_available()
        self._validate_range(start_ms, end_ms)
        with self._db.transaction() as uow:
            asset = self._get(uow, asset_id)
            path = self._on_disk_path(uow, asset_id)
            provider = self._provider(asset.media_type)
            tmp = path.with_name(f"{path.stem}{_TMP_SUFFIX}{path.suffix}")
            cut = self._editor.keep_range if mode == "keep" else self._editor.remove_range
            # The editor returns the *measured* kept segments of the result, so notes and references
            # remap onto the new timeline frame-exactly instead of being shifted by an estimate.
            segments = cut(path, tmp, start_ms=start_ms, end_ms=end_ms)
            if segments is None:
                tmp.unlink(missing_ok=True)
                raise UnsupportedError("the video edit failed -- see the logs")
            # Identify the result *before* touching the original, so a collision leaves the file intact.
            new_hash = self._strategy(provider.identity_strategy_name).compute(tmp, tmp.stat().st_size)
            clash = uow.assets.get_by_hash(new_hash)
            if clash is not None and clash.id != asset_id:
                tmp.unlink(missing_ok=True)
                raise ConflictError(
                    f"the edited clip would be byte-identical to clip #{clash.id} ({clash.title!r}); "
                    "nothing was changed"
                )
            if self._config.editing.keep_original_backup:
                try:
                    shutil.copy2(path, path.with_name(f"{path.stem} (pre-edit backup){path.suffix}"))
                except OSError as exc:
                    tmp.unlink(missing_ok=True)
                    raise UnsupportedError(f"couldn't write the pre-edit backup copy: {exc}") from exc
            self._replace_locked(tmp, path)
            self._refresh_file(uow, asset, path, provider, identity_hash=new_hash)
            self._remap_notes(uow, asset_id, segments)
            self._remap_references(uow, asset_id, segments)
        self._bus.publish(AssetUpdated(asset_id=asset_id))
        with self._db.transaction() as uow:
            refreshed = uow.assets.get(asset_id)
        assert refreshed is not None
        return refreshed

    def _refresh_file(
        self, uow: UnitOfWork, asset: Asset, path: Path, provider: MediaTypeProvider, *, identity_hash: str
    ) -> None:
        st = path.stat()
        asset.identity_hash = identity_hash
        asset.size_bytes = st.st_size
        asset.metadata = {**asset.metadata, **provider.extract_metadata(path)}
        # metadata was just re-read with the tool available; leave it pending only if it could not be.
        asset.metadata_pending = not provider.metadata_extraction_available
        uow.assets.update(asset)                                          # update() now also writes identity_hash
        uow.hash_cache.put(str(path), st.st_size, st.st_mtime_ns, identity_hash)  # so re-scans stay consistent
        if asset.id is not None:
            purge_asset_thumbnails(self._thumb_dir, asset.id)         # drop any stale variant first
            provider.make_thumbnail(path, self._thumb_dir / f"{asset.id}.{self._config.thumbnails.format}",
                                    metadata=asset.metadata)

    @staticmethod
    def _remap_span(
        start_ms: int, end_ms: int, segments: Sequence[KeptSegment]
    ) -> tuple[int, int] | None:
        """Where the source span ``[start_ms, end_ms]`` lands in an edited clip described by
        ``segments``. Each kept segment contributes its overlap; since the segments are concatenated
        in the output the surviving pieces are contiguous there, so the result is the min/max output
        position. ``None`` means the whole span was cut away. A point is just ``start == end``."""
        lo: int | None = None
        hi: int | None = None
        for seg in segments:
            overlap_start = max(start_ms, seg.source_start_ms)
            overlap_end = min(end_ms, seg.source_end_ms)
            if overlap_start <= overlap_end:
                out_lo = seg.output_start_ms + (overlap_start - seg.source_start_ms)
                out_hi = seg.output_start_ms + (overlap_end - seg.source_start_ms)
                lo = out_lo if lo is None else min(lo, out_lo)
                hi = out_hi if hi is None else max(hi, out_hi)
        return None if lo is None or hi is None else (lo, hi)

    def _remap_notes(self, uow: UnitOfWork, asset_id: int, segments: Sequence[KeptSegment]) -> None:
        """Move each timestamped note of an edited clip onto the new timeline; delete a note whose
        moment was cut away. The general note has no timeline position -- it is left untouched."""
        for note in uow.notes.list_for_asset(asset_id):
            if note.timestamp_ms is None or note.id is None:
                continue
            end = note.end_timestamp_ms if note.end_timestamp_ms is not None else note.timestamp_ms
            remapped = self._remap_span(note.timestamp_ms, end, segments)
            if remapped is None:
                uow.notes.delete(note.id)
            else:
                new_start, new_end = remapped
                is_interval = note.end_timestamp_ms is not None and new_end > new_start
                uow.notes.retime(note.id, new_start, new_end if is_interval else None)

    def _remap_references(self, uow: UnitOfWork, asset_id: int, segments: Sequence[KeptSegment]) -> None:
        """Remap references pinned to a *moment* of the edited clip -- ``from_timestamp_ms`` on its
        outgoing references, ``to_timestamp_ms`` on its incoming ones. If the moment was cut away the
        pin is dropped (set to ``None``); the reference itself is always kept."""
        changed: list[Reference] = []
        for ref in uow.references.list_outgoing(asset_id):
            if ref.from_timestamp_ms is not None:
                remapped = self._remap_span(ref.from_timestamp_ms, ref.from_timestamp_ms, segments)
                moved = remapped[0] if remapped is not None else None
                if moved != ref.from_timestamp_ms:
                    ref.from_timestamp_ms = moved
                    changed.append(ref)
        for ref in uow.references.list_incoming(asset_id):
            if ref.to_timestamp_ms is not None:
                remapped = self._remap_span(ref.to_timestamp_ms, ref.to_timestamp_ms, segments)
                moved = remapped[0] if remapped is not None else None
                if moved != ref.to_timestamp_ms:
                    ref.to_timestamp_ms = moved
                    changed.append(ref)
        for ref in changed:
            uow.references.update(ref)

    def _unused_name(self, source: Path, start_ms: int, end_ms: int) -> Path:
        def mmss(ms: int) -> str:
            total = ms // 1000
            return f"{total // 60:02d}-{total % 60:02d}"

        base = self._config.editing.new_clip_name_template.format(
            stem=source.stem, start=mmss(start_ms), end=mmss(end_ms), ext=source.suffix,
        )
        suffix = source.suffix
        candidate = source.with_name(base)
        counter = 2
        while candidate.exists():
            stem = base[: -len(suffix)] if suffix and base.endswith(suffix) else base
            candidate = source.with_name(f"{stem} ({counter}){suffix}")
            counter += 1
        return candidate

    def _register_file(
        self, uow: UnitOfWork, path: Path, media_type: str, provider: MediaTypeProvider
    ) -> tuple[Asset, bool]:
        """Register a freshly-written file as an asset. Returns ``(asset, created)`` -- ``created``
        is ``False`` when the file is byte-identical to one already in the library (deduped)."""
        st = path.stat()
        identity_hash = self._strategy(provider.identity_strategy_name).compute(path, st.st_size)
        existing = uow.assets.get_by_hash(identity_hash)
        if existing is not None and existing.id is not None:
            uow.assets.upsert_path(existing.id, str(path), None)
            return existing, False
        metadata = provider.extract_metadata(path)
        asset = uow.assets.add(Asset(
            identity_hash=identity_hash, media_type=media_type,
            title=provider.display_title(path, metadata), size_bytes=st.st_size, metadata=metadata,
            # the new clip's metadata was just read; only pending if the extractor was unavailable.
            metadata_pending=not provider.metadata_extraction_available,
        ))
        assert asset.id is not None
        uow.assets.upsert_path(asset.id, str(path), None)
        uow.hash_cache.put(str(path), st.st_size, st.st_mtime_ns, identity_hash)
        provider.make_thumbnail(path, self._thumb_dir / f"{asset.id}.{self._config.thumbnails.format}",
                                metadata=metadata)
        return asset, True

    def _copy_notes_into_extract(
        self, uow: UnitOfWork, *, source_id: int, new_id: int, segments: Sequence[KeptSegment]
    ) -> None:
        """Give a freshly-extracted clip a copy of the source's general note and of every
        timestamped note whose moment falls inside the extracted range (remapped, tags carried)."""
        for note in uow.notes.list_for_asset(source_id):
            if note.timestamp_ms is None:
                uow.notes.add(Note(asset_id=new_id, body=note.body))           # the general note
                continue
            end = note.end_timestamp_ms if note.end_timestamp_ms is not None else note.timestamp_ms
            remapped = self._remap_span(note.timestamp_ms, end, segments)
            if remapped is None:
                continue                                                       # outside the extracted range
            new_start, new_end = remapped
            is_interval = note.end_timestamp_ms is not None and new_end > new_start
            copy = uow.notes.add(Note(
                asset_id=new_id, body=note.body, timestamp_ms=new_start,
                end_timestamp_ms=new_end if is_interval else None,
            ))
            if note.id is not None and copy.id is not None:
                tag_ids = uow.notes.tag_ids_for_note(note.id)
                if tag_ids:
                    uow.notes.set_tags(copy.id, tag_ids)

    def _link_excerpt(self, uow: UnitOfWork, child: Asset, parent: Asset, source_start_ms: int) -> None:
        label = self._config.editing.excerpt_reference_type     # a plain description now, not a reference type
        if not label or child.id is None or parent.id is None or child.id == parent.id:
            return
        uow.references.add(Reference(
            from_asset_id=child.id, to_asset_id=parent.id, type_id=None, note=label,
            to_timestamp_ms=source_start_ms,        # the excerpt starts here in the source
        ))
