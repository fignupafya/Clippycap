"""Application services: use-case orchestration over the repositories and the event bus.

Each service opens a :meth:`~clippycap.core.ports.Database.transaction`, calls the relevant
repositories, publishes domain events, and translates "not found" into :class:`NotFoundError`. The
composite result types (asset summaries/details, note views) live here too -- they are not pure
entities, so they belong to the use cases. (Reference, source, saved-view, config and scan services
are in their own modules.)
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clippycap.core.entities import Asset, AssetPath, Note, Reference, Tag, TagGroup
from clippycap.core.errors import ConflictError, InvalidInputError, NotFoundError, UnsupportedError
from clippycap.core.events import (
    AssetOpened,
    AssetRemoved,
    AssetUpdated,
    EventBus,
    NoteCreated,
    NoteDeleted,
    NoteUpdated,
    TagApplied,
    TagCreated,
    TagDeleted,
    TagUnapplied,
    TagUpdated,
)
from clippycap.core.ports import Database, UnitOfWork
from clippycap.core.query import AssetFilter
from clippycap.infra.media.video_thumbnail import purge_asset_thumbnails

# Note bodies may embed "@{<asset id>}" or "@{<asset id>#<note id>}" tokens (inserted by the UI's
# @-mention picker). They turn a note into a cross-reference: when such a note is saved, a Reference
# to the mentioned clip is created (with the mentioned note's timestamp on it, when one is given).
MENTION_RE = re.compile(r"@\{(\d+)(?:#(\d+))?\}")


# --------------------------------------------------------------------------- result types


@dataclass
class AssetSummary:
    asset: Asset
    tag_ids: list[int]
    note_count: int
    reference_count: int
    is_new: bool          # never opened in the app


@dataclass
class AssetPage:
    items: list[AssetSummary]
    total: int
    offset: int
    limit: int


@dataclass
class NoteView:
    note: Note
    tag_ids: list[int]


@dataclass
class AssetDetail:
    asset: Asset
    tag_ids: list[int]
    paths: list[AssetPath]
    general_note: Note | None
    timestamped_notes: list[NoteView]
    mentioned: dict[int, str]                 # clip ids @-mentioned in this asset's notes -> their titles
    mentioned_notes: dict[int, dict[str, Any]]  # note ids @-mentioned ("@{clip#note}") -> {clip_id, body, timestamp_ms}


def _require[T](value: T | None, what: str, key: object) -> T:
    if value is None:
        raise NotFoundError(f"no {what} with id {key!r}")
    return value


def _is_path_under(path: str, root: str) -> bool:
    """True if ``path`` is ``root`` itself or strictly inside it (either separator)."""
    return path == root or path.startswith(root + "\\") or path.startswith(root + "/")


def _compute_folder_counts(paths: Sequence[str], sources: Sequence[str]) -> list[tuple[str, int]]:
    """Walk every clip's path up to its source root, accumulating a count in each ancestor folder.

    A clip is counted in EVERY folder from its parent dir up to (and including) its source root,
    so a parent folder's count is the cumulative descendant clip count (clicking that folder
    surfaces every clip below it). Returns sorted by path so the frontend gets a stable order to
    build a tree from. Folders below a source that's not currently enabled are skipped.
    """
    norm_sources = [str(Path(s).resolve()) for s in sources]
    # Prefer the deepest matching source root when sources nest (one inside the other).
    sorted_sources = sorted(norm_sources, key=len, reverse=True)
    counts: dict[str, int] = {}
    for raw in paths:
        src_root = next((s for s in sorted_sources if _is_path_under(raw, s)), None)
        if src_root is None:
            continue
        parent_dir = str(Path(raw).parent)
        while True:
            counts[parent_dir] = counts.get(parent_dir, 0) + 1
            if parent_dir == src_root:
                break
            new_parent = str(Path(parent_dir).parent)
            if new_parent == parent_dir:                  # reached the filesystem root somehow
                break
            parent_dir = new_parent
    return sorted(counts.items())


# --------------------------------------------------------------------------- assets


class AssetService:
    def __init__(self, database: Database, event_bus: EventBus, thumbnail_dir: Path | None = None) -> None:
        self._db = database
        self._bus = event_bus
        self._thumbnail_dir = thumbnail_dir

    def list_assets(
        self, *, filter: AssetFilter, sort_key: str, offset: int, limit: int
    ) -> AssetPage:
        with self._db.transaction() as uow:
            items, total = uow.assets.search(filter=filter, sort_key=sort_key, offset=offset, limit=limit)
            ids = [a.id for a in items if a.id is not None]
            tag_ids = uow.tags.tag_ids_for_assets(ids)
            note_counts = uow.notes.counts_for_assets(ids)
            ref_counts = uow.references.counts_for_assets(ids)
            summaries = [
                AssetSummary(
                    asset=a, tag_ids=tag_ids.get(a.id, []), note_count=note_counts.get(a.id, 0),
                    reference_count=ref_counts.get(a.id, 0), is_new=a.last_opened_at is None,
                )
                for a in items
                if a.id is not None
            ]
        return AssetPage(items=summaries, total=total, offset=offset, limit=limit)

    def get_detail(self, asset_id: int) -> AssetDetail:
        with self._db.transaction() as uow:
            asset = _require(uow.assets.get(asset_id), "asset", asset_id)
            tag_ids = uow.tags.tag_ids_for_asset(asset_id)
            paths = uow.assets.get_paths(asset_id)
            general = uow.notes.general_note(asset_id)
            all_notes = uow.notes.list_for_asset(asset_id)
            timestamped = [
                NoteView(note=n, tag_ids=uow.notes.tag_ids_for_note(n.id))
                for n in all_notes
                if n.timestamp_ms is not None and n.id is not None
            ]
            mentioned: dict[int, str] = {}
            mentioned_notes: dict[int, dict[str, Any]] = {}
            for note in all_notes:
                for mm in MENTION_RE.finditer(note.body):
                    mid = int(mm.group(1))
                    if mid == asset_id:
                        continue
                    if mid not in mentioned:
                        other = uow.assets.get(mid)
                        mentioned[mid] = other.title if other is not None else "(deleted)"
                    if mm.group(2) is not None:
                        nid = int(mm.group(2))
                        if nid not in mentioned_notes:
                            tnote = uow.notes.get(nid)
                            if tnote is not None and tnote.asset_id == mid and tnote.timestamp_ms is not None:
                                mentioned_notes[nid] = {
                                    "clip_id": mid, "body": tnote.body, "timestamp_ms": tnote.timestamp_ms,
                                }
        return AssetDetail(
            asset=asset, tag_ids=tag_ids, paths=paths, general_note=general,
            timestamped_notes=timestamped, mentioned=mentioned, mentioned_notes=mentioned_notes,
        )

    def get(self, asset_id: int) -> Asset | None:
        with self._db.transaction() as uow:
            return uow.assets.get(asset_id)

    def present_file_path(self, asset_id: int) -> Path | None:
        with self._db.transaction() as uow:
            paths = uow.assets.get_paths(asset_id)
        for entry in paths:
            candidate = Path(entry.path)
            if entry.present and candidate.is_file():
                return candidate
        return None

    def rename_file(self, asset_id: int, new_name: str) -> Asset:
        cleaned = new_name.strip().strip(". ")
        if not cleaned or "/" in cleaned or "\\" in cleaned:
            raise InvalidInputError("the new name must be a plain file name (no path separators)")
        with self._db.transaction() as uow:
            asset = _require(uow.assets.get(asset_id), "asset", asset_id)
            old_path = next(
                (Path(p.path) for p in uow.assets.get_paths(asset_id) if p.present and Path(p.path).is_file()), None
            )
            if old_path is None:
                raise UnsupportedError("this asset has no readable file on disk to rename")
            keep_ext = bool(old_path.suffix) and not cleaned.lower().endswith(old_path.suffix.lower())
            new_path = old_path.with_name(cleaned + old_path.suffix if keep_ext else cleaned)
            if new_path == old_path:
                return asset
            if str(new_path).lower() != str(old_path).lower() and new_path.exists():
                raise ConflictError(f"a file named {new_path.name!r} already exists in that folder")
            try:
                old_path.rename(new_path)
            except OSError as exc:
                raise UnsupportedError(
                    f"couldn't rename {old_path.name!r}: {exc} -- the file may be in use; close it and try again"
                ) from exc
            st = new_path.stat()
            uow.assets.rename_path(str(old_path), str(new_path))
            uow.hash_cache.forget(str(old_path))
            uow.hash_cache.put(str(new_path), st.st_size, st.st_mtime_ns, asset.identity_hash)
            asset.title = new_path.stem                  # the title IS the file name -- keep it in sync
            uow.assets.update(asset)
        self._bus.publish(AssetUpdated(asset_id=asset_id))
        return asset

    def merge_metadata(self, asset_id: int, partial: dict[str, Any]) -> Asset:
        with self._db.transaction() as uow:
            asset = _require(uow.assets.get(asset_id), "asset", asset_id)
            asset.metadata = {**asset.metadata, **partial}
            uow.assets.update(asset)
        self._bus.publish(AssetUpdated(asset_id=asset_id))
        return asset

    def mark_opened(self, asset_id: int) -> None:
        with self._db.transaction() as uow:
            _require(uow.assets.get(asset_id), "asset", asset_id)
            uow.assets.touch_opened(asset_id)
        self._bus.publish(AssetOpened(asset_id=asset_id))

    def delete(self, asset_id: int, *, delete_files: bool = False) -> None:
        with self._db.transaction() as uow:
            _require(uow.assets.get(asset_id), "asset", asset_id)
            file_paths = [p.path for p in uow.assets.get_paths(asset_id) if p.present] if delete_files else []
            uow.assets.delete(asset_id)
        if self._thumbnail_dir is not None:
            purge_asset_thumbnails(self._thumbnail_dir, asset_id)
        for file_path in file_paths:
            with contextlib.suppress(OSError):
                Path(file_path).unlink(missing_ok=True)
        self._bus.publish(AssetRemoved(asset_id=asset_id))

    def all_matching_ids(self, *, filter: AssetFilter, sort_key: str = "added_desc") -> list[int]:
        """Every asset id matching ``filter`` (no pagination). Used by the "select all matching"
        bulk-bar shortcut so a bulk operation can target clips beyond the current page."""
        with self._db.transaction() as uow:
            items, _ = uow.assets.search(filter=filter, sort_key=sort_key, offset=0, limit=10_000_000)
        return [a.id for a in items if a.id is not None]

    def folder_counts(self) -> list[tuple[str, int]]:
        """Folders (flat, path-sorted): one entry per folder reachable from a present asset path,
        each with the cumulative descendant clip count. The frontend assembles a tree from this."""
        with self._db.transaction() as uow:
            paths = uow.assets.distinct_present_paths()
            sources = [s.path for s in uow.sources.list_all() if s.enabled]
        return _compute_folder_counts(paths, sources)


# --------------------------------------------------------------------------- tags


_TAG_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}
_TAG_IMAGE_CONTENT_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
_MAX_TAG_IMAGE_BYTES = 4 * 1024 * 1024


class TagService:
    def __init__(self, database: Database, event_bus: EventBus, tag_images_dir: Path | None = None) -> None:
        self._db = database
        self._bus = event_bus
        self._images_dir = tag_images_dir

    def list_all(self) -> list[Tag]:
        with self._db.transaction() as uow:
            return uow.tags.list_all()

    def list_with_counts(self) -> list[tuple[Tag, int]]:
        with self._db.transaction() as uow:
            return [(t, uow.tags.asset_count(t.id)) for t in uow.tags.list_all() if t.id is not None]

    def create(  # noqa: PLR0913 -- one keyword per independent tag attribute is the point
        self, *, name: str, color: str, icon: str | None = None, image_ref: str | None = None,
        description: str = "", sort_order: int | None = None,
        group_id: int | None = None, has_page: bool = False, notes: str = "",
    ) -> Tag:
        with self._db.transaction() as uow:
            if group_id is not None and uow.tag_groups.get(group_id) is None:
                raise InvalidInputError(f"no tag category with id {group_id!r}")
            order = (
                sort_order if sort_order is not None
                else 1 + max((t.sort_order for t in uow.tags.list_all()), default=-1)
            )
            tag = uow.tags.add(Tag(
                name=name, color=color, icon=icon, image_ref=image_ref, description=description,
                sort_order=order, group_id=group_id, has_page=has_page, notes=notes,
            ))
        assert tag.id is not None
        self._bus.publish(TagCreated(tag_id=tag.id, name=tag.name))
        return tag

    def update(  # noqa: PLR0913 -- one keyword per independent tag attribute is the point
        self, tag_id: int, *, name: str, color: str, icon: str | None, image_ref: str | None,
        description: str, sort_order: int,
        group_id: int | None = None, has_page: bool = False, notes: str = "",
    ) -> Tag:
        with self._db.transaction() as uow:
            tag = _require(uow.tags.get(tag_id), "tag", tag_id)
            if group_id is not None and uow.tag_groups.get(group_id) is None:
                raise InvalidInputError(f"no tag category with id {group_id!r}")
            old_ref = tag.image_ref
            tag.name, tag.color, tag.icon = name, color, icon
            tag.image_ref, tag.description, tag.sort_order = image_ref, description, sort_order
            tag.group_id, tag.has_page, tag.notes = group_id, has_page, notes
            uow.tags.update(tag)
        if old_ref != image_ref:
            self._prune_image(old_ref)
        self._bus.publish(TagUpdated(tag_id=tag_id))
        return tag

    def set_notes(self, tag_id: int, notes: str) -> Tag:
        """Update only a tag's page notes (used by the tag page's autosaving notes editor)."""
        with self._db.transaction() as uow:
            tag = _require(uow.tags.get(tag_id), "tag", tag_id)
            tag.notes = notes
            uow.tags.update(tag)
        self._bus.publish(TagUpdated(tag_id=tag_id))
        return tag

    def delete(self, tag_id: int) -> None:
        with self._db.transaction() as uow:
            tag = _require(uow.tags.get(tag_id), "tag", tag_id)
            old_ref = tag.image_ref
            uow.tags.delete(tag_id)
        self._prune_image(old_ref)
        self._bus.publish(TagDeleted(tag_id=tag_id))

    def store_image(self, data: bytes, *, ext: str = "", content_type: str = "") -> str:
        """Persist an uploaded image under the data dir and return its (stable) reference name."""
        images_dir = self._images_dir
        if images_dir is None:
            raise UnsupportedError("tag-image storage is unavailable")
        chosen = ext.lower().lstrip(".")
        if chosen not in _TAG_IMAGE_EXTS:
            chosen = _TAG_IMAGE_CONTENT_TYPES.get(content_type.split(";", maxsplit=1)[0].strip().lower(), "")
        if not chosen:
            raise InvalidInputError("the image must be a PNG, JPEG, WEBP or GIF")
        if not data:
            raise InvalidInputError("the uploaded image is empty")
        if len(data) > _MAX_TAG_IMAGE_BYTES:
            raise InvalidInputError("the image is too large (4 MiB max)")
        images_dir.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}.{'jpg' if chosen == 'jpeg' else chosen}"
        (images_dir / name).write_bytes(data)
        return name

    def _prune_image(self, image_ref: str | None) -> None:
        """Delete an uploaded tag image once no tag references it (keeps the data dir tidy)."""
        images_dir = self._images_dir
        if not image_ref or images_dir is None:
            return
        with self._db.transaction() as uow:
            if any(t.image_ref == image_ref for t in uow.tags.list_all()):
                return
        candidate = images_dir / image_ref
        if candidate.parent == images_dir and candidate.is_file():
            with contextlib.suppress(OSError):
                candidate.unlink()

    def reorder(self, ordered_ids: Sequence[int]) -> None:
        with self._db.transaction() as uow:
            uow.tags.reorder(ordered_ids)

    def apply_to_asset(self, asset_id: int, tag_id: int) -> None:
        with self._db.transaction() as uow:
            _require(uow.assets.get(asset_id), "asset", asset_id)
            _require(uow.tags.get(tag_id), "tag", tag_id)
            applied = uow.tags.apply(asset_id, tag_id)
        if applied:
            self._bus.publish(TagApplied(asset_id=asset_id, tag_id=tag_id))

    def remove_from_asset(self, asset_id: int, tag_id: int) -> None:
        with self._db.transaction() as uow:
            removed = uow.tags.unapply(asset_id, tag_id)
        if removed:
            self._bus.publish(TagUnapplied(asset_id=asset_id, tag_id=tag_id))

    def bulk_tag_counts(self, asset_ids: Sequence[int]) -> dict[int, int]:
        """For each tag id, how many of ``asset_ids`` currently have that tag. Drives the bulk-tag
        modal's per-tag "N of M selected have this" indicator."""
        with self._db.transaction() as uow:
            per_asset = uow.tags.tag_ids_for_assets(list(asset_ids))
        counts: dict[int, int] = {}
        for tag_ids in per_asset.values():
            for tid in tag_ids:
                counts[tid] = counts.get(tid, 0) + 1
        return counts

    def bulk_apply(
        self, *, asset_ids: Sequence[int],
        add: Sequence[int] = (), remove: Sequence[int] = (),
        replace_with: Sequence[int] | None = None,
    ) -> dict[str, int]:
        """Apply a bulk tag change in one transaction. When ``replace_with`` is given each asset's
        tag set becomes EXACTLY that (any other tags are removed). Otherwise ``add`` / ``remove``
        are applied as a diff -- tags already present are not re-added, tags not present are not
        re-removed. Returns the number of links actually created / removed."""
        added = 0
        removed = 0
        with self._db.transaction() as uow:
            current_tags = uow.tags.tag_ids_for_assets(list(asset_ids))
            for asset_id in asset_ids:
                if uow.assets.get(asset_id) is None:
                    continue
                current = set(current_tags.get(asset_id, []))
                if replace_with is not None:
                    target = set(replace_with)
                    for tid in current - target:
                        if uow.tags.unapply(asset_id, tid):
                            removed += 1
                    for tid in target - current:
                        if uow.tags.apply(asset_id, tid):
                            added += 1
                else:
                    for tid in add:
                        if tid not in current and uow.tags.apply(asset_id, tid):
                            added += 1
                            current.add(tid)
                    for tid in remove:
                        if tid in current and uow.tags.unapply(asset_id, tid):
                            removed += 1
                            current.discard(tid)
        return {"added": added, "removed": removed}


# --------------------------------------------------------------------------- tag groups (categories)


class TagGroupService:
    """CRUD for user-defined tag categories. Nothing is seeded -- the library starts with zero
    groups and the whole concept stays invisible in the UI until the user creates one."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_all(self) -> list[TagGroup]:
        with self._db.transaction() as uow:
            return uow.tag_groups.list_all()

    def create(self, *, name: str, color: str = "", has_page: bool = False,
               parent_id: int | None = None, notes: str = "",
               sort_order: int | None = None) -> TagGroup:
        clean = name.strip()
        if not clean:
            raise InvalidInputError("a category needs a name")
        with self._db.transaction() as uow:
            if parent_id is not None and uow.tag_groups.get(parent_id) is None:
                raise InvalidInputError(f"no parent category with id {parent_id!r}")
            order = (
                sort_order if sort_order is not None
                else 1 + max((g.sort_order for g in uow.tag_groups.list_all()), default=-1)
            )
            return uow.tag_groups.add(TagGroup(
                name=clean, color=color, sort_order=order, has_page=has_page,
                parent_id=parent_id, notes=notes,
            ))

    def update(self, group_id: int, *, name: str, color: str, has_page: bool,
               sort_order: int, parent_id: int | None = None) -> TagGroup:
        clean = name.strip()
        if not clean:
            raise InvalidInputError("a category needs a name")
        with self._db.transaction() as uow:
            group = _require(uow.tag_groups.get(group_id), "tag category", group_id)
            if parent_id is not None:
                self._guard_parent(uow, group_id, parent_id)
            group.name, group.color = clean, color
            group.has_page, group.sort_order = has_page, sort_order
            group.parent_id = parent_id
            uow.tag_groups.update(group)
            return group

    def set_notes(self, group_id: int, notes: str) -> TagGroup:
        """Update only a category's page notes (its page's autosaving notes editor calls this)."""
        with self._db.transaction() as uow:
            group = _require(uow.tag_groups.get(group_id), "tag category", group_id)
            group.notes = notes
            uow.tag_groups.update(group)
            return group

    @staticmethod
    def _guard_parent(uow: UnitOfWork, group_id: int, parent_id: int) -> None:
        """Reject a parent that would create a cycle (self, or a descendant of this category)."""
        if parent_id == group_id:
            raise InvalidInputError("a category cannot be its own parent")
        if uow.tag_groups.get(parent_id) is None:
            raise InvalidInputError(f"no parent category with id {parent_id!r}")
        by_id = {g.id: g for g in uow.tag_groups.list_all()}
        cursor: int | None = parent_id
        seen: set[int] = set()
        while cursor is not None and cursor not in seen:
            if cursor == group_id:
                raise InvalidInputError("that parent would create a category cycle")
            seen.add(cursor)
            parent = by_id.get(cursor)
            cursor = parent.parent_id if parent is not None else None

    def delete(self, group_id: int) -> None:
        """Delete a category. Its tags are NOT deleted -- they fall back to uncategorised
        (ON DELETE SET NULL on tags.group_id); child categories are promoted to top-level
        (ON DELETE SET NULL on tag_groups.parent_id)."""
        with self._db.transaction() as uow:
            _require(uow.tag_groups.get(group_id), "tag category", group_id)
            uow.tag_groups.delete(group_id)

    def reorder(self, ordered_ids: Sequence[int]) -> None:
        with self._db.transaction() as uow:
            uow.tag_groups.reorder(ordered_ids)


# --------------------------------------------------------------------------- notes


class NoteService:
    def __init__(self, database: Database, event_bus: EventBus) -> None:
        self._db = database
        self._bus = event_bus

    def _sync_mention_refs(self, uow: UnitOfWork, asset_id: int, body: str, from_ts: int | None) -> None:
        """Ensure a Reference exists from ``asset_id`` to every clip @-mentioned in ``body``.

        ``@{clip#note}`` mentions pin the reference to that note's timestamp (``to_timestamp_ms``).
        """
        targets: set[tuple[int, int | None]] = set()        # (clip id, to_timestamp_ms)
        for mm in MENTION_RE.finditer(body):
            clip_id = int(mm.group(1))
            if clip_id == asset_id:
                continue
            to_ts: int | None = None
            if mm.group(2) is not None:
                note = uow.notes.get(int(mm.group(2)))
                if note is not None and note.asset_id == clip_id and note.timestamp_ms is not None:
                    to_ts = note.timestamp_ms
            targets.add((clip_id, to_ts))
        if not targets:
            return
        have = {(r.to_asset_id, r.to_timestamp_ms) for r in uow.references.list_outgoing(asset_id)}
        for (clip_id, to_ts) in targets:
            if (clip_id, to_ts) in have or uow.assets.get(clip_id) is None:
                continue
            uow.references.add(Reference(
                from_asset_id=asset_id, to_asset_id=clip_id, type_id=None,
                from_timestamp_ms=from_ts, to_timestamp_ms=to_ts,
            ))

    def list_for_asset(self, asset_id: int) -> list[NoteView]:
        with self._db.transaction() as uow:
            _require(uow.assets.get(asset_id), "asset", asset_id)
            return [
                NoteView(note=n, tag_ids=uow.notes.tag_ids_for_note(n.id))
                for n in uow.notes.list_for_asset(asset_id)
                if n.id is not None
            ]

    def set_general(self, asset_id: int, body: str) -> NoteView:
        created = False
        with self._db.transaction() as uow:
            _require(uow.assets.get(asset_id), "asset", asset_id)
            existing = uow.notes.general_note(asset_id)
            if existing is not None:
                existing.body = body
                uow.notes.update(existing)
                note = existing
            else:
                note = uow.notes.add(Note(asset_id=asset_id, body=body))
                created = True
            self._sync_mention_refs(uow, asset_id, body, None)
            tag_ids = uow.notes.tag_ids_for_note(note.id) if note.id is not None else []
        assert note.id is not None
        self._bus.publish(
            NoteCreated(note_id=note.id, asset_id=asset_id) if created
            else NoteUpdated(note_id=note.id, asset_id=asset_id)
        )
        return NoteView(note=note, tag_ids=tag_ids)

    def add_timestamped(
        self, asset_id: int, timestamp_ms: int, body: str, *, end_timestamp_ms: int | None = None
    ) -> NoteView:
        if timestamp_ms < 0:
            raise InvalidInputError("timestamp_ms must be >= 0")
        if end_timestamp_ms is not None and end_timestamp_ms <= timestamp_ms:
            raise InvalidInputError("end_timestamp_ms must be greater than timestamp_ms")
        with self._db.transaction() as uow:
            _require(uow.assets.get(asset_id), "asset", asset_id)
            note = uow.notes.add(Note(
                asset_id=asset_id, body=body, timestamp_ms=timestamp_ms, end_timestamp_ms=end_timestamp_ms,
            ))
            self._sync_mention_refs(uow, asset_id, body, timestamp_ms)
        assert note.id is not None
        self._bus.publish(NoteCreated(note_id=note.id, asset_id=asset_id))
        return NoteView(note=note, tag_ids=[])

    def update(self, note_id: int, body: str) -> NoteView:
        with self._db.transaction() as uow:
            note = _require(uow.notes.get(note_id), "note", note_id)
            note.body = body
            uow.notes.update(note)
            self._sync_mention_refs(uow, note.asset_id, body, note.timestamp_ms)
            tag_ids = uow.notes.tag_ids_for_note(note_id)
        self._bus.publish(NoteUpdated(note_id=note_id, asset_id=note.asset_id))
        return NoteView(note=note, tag_ids=tag_ids)

    def retime(self, note_id: int, timestamp_ms: int, end_timestamp_ms: int | None = None) -> NoteView:
        if timestamp_ms < 0:
            raise InvalidInputError("timestamp_ms must be >= 0")
        if end_timestamp_ms is not None and end_timestamp_ms <= timestamp_ms:
            raise InvalidInputError("end_timestamp_ms must be greater than timestamp_ms")
        with self._db.transaction() as uow:
            note = _require(uow.notes.get(note_id), "note", note_id)
            if note.timestamp_ms is None:
                raise InvalidInputError("the general note has no timestamp to change")
            uow.notes.retime(note_id, timestamp_ms, end_timestamp_ms)
            note.timestamp_ms, note.end_timestamp_ms = timestamp_ms, end_timestamp_ms
            tag_ids = uow.notes.tag_ids_for_note(note_id)
        self._bus.publish(NoteUpdated(note_id=note_id, asset_id=note.asset_id))
        return NoteView(note=note, tag_ids=tag_ids)

    def delete(self, note_id: int) -> None:
        with self._db.transaction() as uow:
            note = _require(uow.notes.get(note_id), "note", note_id)
            asset_id = note.asset_id
            uow.notes.delete(note_id)
        self._bus.publish(NoteDeleted(note_id=note_id, asset_id=asset_id))

    def set_tags(self, note_id: int, tag_ids: Sequence[int]) -> None:
        with self._db.transaction() as uow:
            _require(uow.notes.get(note_id), "note", note_id)
            for tid in tag_ids:
                _require(uow.tags.get(tid), "tag", tid)
            uow.notes.set_tags(note_id, tag_ids)
