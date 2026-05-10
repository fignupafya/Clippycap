"""Application services: use-case orchestration over the repositories and the event bus.

Each service opens a :meth:`~clippycap.core.ports.Database.transaction`, calls the relevant
repositories, publishes domain events, and translates "not found" into :class:`NotFoundError`. The
composite result types (asset summaries/details, note views) live here too -- they are not pure
entities, so they belong to the use cases. (Reference, source, saved-view, config and scan services
are in their own modules.)
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clippycap.core.entities import Asset, AssetPath, Note, Tag
from clippycap.core.errors import InvalidInputError, NotFoundError
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
from clippycap.core.ports import Database
from clippycap.core.query import AssetFilter
from clippycap.infra.media.video_thumbnail import purge_asset_thumbnails

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


def _require[T](value: T | None, what: str, key: object) -> T:
    if value is None:
        raise NotFoundError(f"no {what} with id {key!r}")
    return value


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
            timestamped = [
                NoteView(note=n, tag_ids=uow.notes.tag_ids_for_note(n.id))
                for n in uow.notes.list_for_asset(asset_id)
                if n.timestamp_ms is not None and n.id is not None
            ]
        return AssetDetail(
            asset=asset, tag_ids=tag_ids, paths=paths, general_note=general, timestamped_notes=timestamped
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

    def update_title(self, asset_id: int, title: str) -> Asset:
        with self._db.transaction() as uow:
            asset = _require(uow.assets.get(asset_id), "asset", asset_id)
            asset.title = title
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


# --------------------------------------------------------------------------- tags


class TagService:
    def __init__(self, database: Database, event_bus: EventBus) -> None:
        self._db = database
        self._bus = event_bus

    def list_all(self) -> list[Tag]:
        with self._db.transaction() as uow:
            return uow.tags.list_all()

    def list_with_counts(self) -> list[tuple[Tag, int]]:
        with self._db.transaction() as uow:
            return [(t, uow.tags.asset_count(t.id)) for t in uow.tags.list_all() if t.id is not None]

    def create(
        self, *, name: str, color: str, icon: str | None = None, image_ref: str | None = None,
        description: str = "", sort_order: int | None = None,
    ) -> Tag:
        with self._db.transaction() as uow:
            order = (
                sort_order if sort_order is not None
                else 1 + max((t.sort_order for t in uow.tags.list_all()), default=-1)
            )
            tag = uow.tags.add(Tag(
                name=name, color=color, icon=icon, image_ref=image_ref, description=description, sort_order=order
            ))
        assert tag.id is not None
        self._bus.publish(TagCreated(tag_id=tag.id, name=tag.name))
        return tag

    def update(
        self, tag_id: int, *, name: str, color: str, icon: str | None, image_ref: str | None,
        description: str, sort_order: int,
    ) -> Tag:
        with self._db.transaction() as uow:
            tag = _require(uow.tags.get(tag_id), "tag", tag_id)
            tag.name, tag.color, tag.icon = name, color, icon
            tag.image_ref, tag.description, tag.sort_order = image_ref, description, sort_order
            uow.tags.update(tag)
        self._bus.publish(TagUpdated(tag_id=tag_id))
        return tag

    def delete(self, tag_id: int) -> None:
        with self._db.transaction() as uow:
            _require(uow.tags.get(tag_id), "tag", tag_id)
            uow.tags.delete(tag_id)
        self._bus.publish(TagDeleted(tag_id=tag_id))

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


# --------------------------------------------------------------------------- notes


class NoteService:
    def __init__(self, database: Database, event_bus: EventBus) -> None:
        self._db = database
        self._bus = event_bus

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
        assert note.id is not None
        self._bus.publish(NoteCreated(note_id=note.id, asset_id=asset_id))
        return NoteView(note=note, tag_ids=[])

    def update(self, note_id: int, body: str) -> NoteView:
        with self._db.transaction() as uow:
            note = _require(uow.notes.get(note_id), "note", note_id)
            note.body = body
            uow.notes.update(note)
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
