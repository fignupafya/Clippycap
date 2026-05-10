"""Source-folder and saved-view use cases."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from clippycap.core.entities import SavedView, Source
from clippycap.core.errors import InvalidInputError, NotFoundError
from clippycap.core.events import EventBus, SourceAdded, SourceRemoved
from clippycap.core.ports import Database


class SourceService:
    def __init__(self, database: Database, event_bus: EventBus) -> None:
        self._db = database
        self._bus = event_bus

    def list_all(self) -> list[Source]:
        with self._db.transaction() as uow:
            return uow.sources.list_all()

    def create(self, path: str, *, recursive: bool = True, media_types: Sequence[str] | None = None) -> Source:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise InvalidInputError(f"not an existing directory: {resolved}")
        with self._db.transaction() as uow:
            source = uow.sources.add(
                Source(path=str(resolved), recursive=recursive, media_types=list(media_types or []))
            )
        assert source.id is not None
        self._bus.publish(SourceAdded(source_id=source.id, path=source.path))
        return source

    def update(
        self, source_id: int, *, recursive: bool, enabled: bool, media_types: Sequence[str]
    ) -> Source:
        with self._db.transaction() as uow:
            source = uow.sources.get(source_id)
            if source is None:
                raise NotFoundError(f"no source with id {source_id!r}")
            source.recursive, source.enabled, source.media_types = recursive, enabled, list(media_types)
            uow.sources.update(source)
            return source

    def delete(self, source_id: int) -> None:
        with self._db.transaction() as uow:
            if uow.sources.get(source_id) is None:
                raise NotFoundError(f"no source with id {source_id!r}")
            uow.sources.delete(source_id)
        self._bus.publish(SourceRemoved(source_id=source_id))


class SavedViewService:
    def __init__(self, database: Database) -> None:
        self._db = database

    def list_all(self) -> list[SavedView]:
        with self._db.transaction() as uow:
            return uow.saved_views.list_all()

    def create(self, *, name: str, filter_json: str, sort_key: str, sort_order: int = 0) -> SavedView:
        with self._db.transaction() as uow:
            return uow.saved_views.add(
                SavedView(name=name, filter_json=filter_json, sort_key=sort_key, sort_order=sort_order)
            )

    def update(
        self, view_id: int, *, name: str, filter_json: str, sort_key: str, sort_order: int
    ) -> SavedView:
        with self._db.transaction() as uow:
            view = uow.saved_views.get(view_id)
            if view is None:
                raise NotFoundError(f"no saved view with id {view_id!r}")
            view.name, view.filter_json, view.sort_key, view.sort_order = name, filter_json, sort_key, sort_order
            uow.saved_views.update(view)
            return view

    def delete(self, view_id: int) -> None:
        with self._db.transaction() as uow:
            if uow.saved_views.get(view_id) is None:
                raise NotFoundError(f"no saved view with id {view_id!r}")
            uow.saved_views.delete(view_id)

    def reorder(self, ordered_ids: Sequence[int]) -> None:
        with self._db.transaction() as uow:
            uow.saved_views.reorder(ordered_ids)
