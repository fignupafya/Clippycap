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
from pathlib import Path
from typing import Literal

from clippycap.core.entities import Asset, Reference
from clippycap.core.errors import ConflictError, InvalidInputError, NotFoundError, UnsupportedError
from clippycap.core.events import AssetUpdated, EventBus
from clippycap.core.ports import Database, IdentityStrategy, MediaTypeProvider, UnitOfWork, VideoEditor
from clippycap.infra.config import Config
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
        config: Config,
        thumbnail_dir: Path,
    ) -> None:
        self._db = database
        self._editor = video_editor
        self._media_types = media_types
        self._identity = identity_strategies
        self._bus = event_bus
        self._config = config
        self._thumb_dir = thumbnail_dir

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
            if not self._editor.keep_range(source_path, out_path, start_ms=start_ms, end_ms=end_ms):
                out_path.unlink(missing_ok=True)
                raise UnsupportedError("the video edit failed -- see the logs")
            new_asset = self._register_file(uow, out_path, asset.media_type, provider)
            self._link_excerpt(uow, new_asset, asset)
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
            # What source-time range the result will actually cover (the cut snaps to a frame /
            # keyframe), so notes are shifted by the real amount instead of the requested one.
            if mode == "keep":
                eff_start = self._editor.resolve_cut_start(path, start_ms)
                note_lo, note_hi = eff_start, eff_start + (end_ms - start_ms)
            else:
                note_lo, note_hi = start_ms, self._editor.resolve_cut_start(path, end_ms)
            tmp = path.with_name(f"{path.stem}{_TMP_SUFFIX}{path.suffix}")
            edit = self._editor.keep_range if mode == "keep" else self._editor.remove_range
            if not edit(path, tmp, start_ms=start_ms, end_ms=end_ms):
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
            self._shift_notes(uow, asset_id, note_lo, note_hi, mode)
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
        uow.assets.update(asset)                                          # update() now also writes identity_hash
        uow.hash_cache.put(str(path), st.st_size, st.st_mtime_ns, identity_hash)  # so re-scans stay consistent
        if asset.id is not None:
            purge_asset_thumbnails(self._thumb_dir, asset.id)         # drop any stale variant first
            provider.make_thumbnail(path, self._thumb_dir / f"{asset.id}.{self._config.thumbnails.format}",
                                    metadata=asset.metadata)

    def _shift_notes(self, uow: UnitOfWork, asset_id: int, lo: int, hi: int, mode: _EditMode) -> None:
        # (lo, hi) in source-time: for "keep" the range the trimmed clip covers; for "remove" the
        # range that was removed (the head ends at lo, the tail resumes at hi).
        gap = hi - lo
        for note in uow.notes.list_for_asset(asset_id):
            if note.timestamp_ms is None or note.id is None:
                continue
            if mode == "keep":
                if not (lo <= note.timestamp_ms <= hi):
                    uow.notes.delete(note.id)
                    continue
                new_start = note.timestamp_ms - lo
                new_end: int | None = None
                if note.end_timestamp_ms is not None:
                    new_end = min(note.end_timestamp_ms, hi) - lo
                    if new_end <= new_start:
                        new_end = None
                uow.notes.retime(note.id, new_start, new_end)
            else:  # "remove": [lo, hi) is gone
                if lo <= note.timestamp_ms < hi:
                    uow.notes.delete(note.id)
                    continue
                new_start = note.timestamp_ms if note.timestamp_ms < lo else note.timestamp_ms - gap
                new_end = note.end_timestamp_ms
                if new_end is not None:
                    if new_end > hi:
                        new_end -= gap
                    elif new_end > lo:
                        new_end = lo                    # the interval's tail fell in the cut -- clamp to it
                    if new_end <= new_start:
                        new_end = None
                uow.notes.retime(note.id, new_start, new_end)

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
    ) -> Asset:
        st = path.stat()
        identity_hash = self._strategy(provider.identity_strategy_name).compute(path, st.st_size)
        existing = uow.assets.get_by_hash(identity_hash)
        if existing is not None and existing.id is not None:
            uow.assets.upsert_path(existing.id, str(path), None)
            return existing
        metadata = provider.extract_metadata(path)
        asset = uow.assets.add(Asset(
            identity_hash=identity_hash, media_type=media_type,
            title=provider.display_title(path, metadata), size_bytes=st.st_size, metadata=metadata,
        ))
        assert asset.id is not None
        uow.assets.upsert_path(asset.id, str(path), None)
        uow.hash_cache.put(str(path), st.st_size, st.st_mtime_ns, identity_hash)
        provider.make_thumbnail(path, self._thumb_dir / f"{asset.id}.{self._config.thumbnails.format}",
                                metadata=metadata)
        return asset

    def _link_excerpt(self, uow: UnitOfWork, child: Asset, parent: Asset) -> None:
        type_name = self._config.editing.excerpt_reference_type
        if not type_name or child.id is None or parent.id is None or child.id == parent.id:
            return
        ref_type = uow.reference_types.get_by_name(type_name)
        uow.references.add(Reference(
            from_asset_id=child.id, to_asset_id=parent.id,
            type_id=ref_type.id if ref_type is not None else None,
            label="" if ref_type is not None else type_name,
        ))
