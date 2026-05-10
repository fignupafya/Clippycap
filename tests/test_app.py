"""Tests for the application services (asset / tag / note use cases)."""

from __future__ import annotations

from pathlib import Path

import pytest

from clippycap.app.services import AssetService, NoteService, TagService
from clippycap.core.entities import Asset
from clippycap.core.errors import InvalidInputError, NotFoundError
from clippycap.core.events import Event, TagApplied
from clippycap.core.query import AssetFilter
from clippycap.infra.db.database import SqliteDatabase
from clippycap.plugins_runtime.event_bus import InProcessEventBus


@pytest.fixture
def env(tmp_path: Path) -> tuple[SqliteDatabase, InProcessEventBus, list[Event]]:
    db = SqliteDatabase(tmp_path / "library.sqlite")
    db.initialise()
    bus = InProcessEventBus()
    events: list[Event] = []
    bus.subscribe_all(events.append)
    return db, bus, events


def _seed_asset(db: SqliteDatabase, h: str, title: str = "clip") -> int:
    with db.transaction() as uow:
        asset = uow.assets.add(Asset(identity_hash=h, media_type="video", title=title, size_bytes=1))
        assert asset.id is not None
        return asset.id


def test_tag_lifecycle_and_apply(env: tuple[SqliteDatabase, InProcessEventBus, list[Event]]) -> None:
    db, bus, events = env
    tags = TagService(db, bus)
    asset_id = _seed_asset(db, "b3:a")
    kill = tags.create(name="kill", color="#56c271", icon="boom")
    assert kill.sort_order == 0  # first tag -> order 0
    other = tags.create(name="funny", color="#ef7bb5")
    assert other.sort_order == 1
    assert [t.name for t in tags.list_all()] == ["kill", "funny"]
    assert tags.list_with_counts() == [(kill, 0), (other, 0)]
    tags.apply_to_asset(asset_id, kill.id)
    tags.apply_to_asset(asset_id, kill.id)  # idempotent: no second event
    assert sum(isinstance(e, TagApplied) for e in events) == 1
    tags.remove_from_asset(asset_id, other.id)  # not applied -> no event, no error
    kill_updated = tags.update(kill.id, name="kill (clean)", color="#00ff00", icon=None,
                               image_ref="img42.png", description="a clean kill", sort_order=2)
    assert kill_updated.name == "kill (clean)" and kill_updated.image_ref == "img42.png"
    tags.delete(other.id)
    assert [t.name for t in tags.list_all()] == ["kill (clean)"]
    with pytest.raises(NotFoundError):
        tags.update(9999, name="x", color="#000000", icon=None, image_ref=None, description="", sort_order=0)
    with pytest.raises(NotFoundError):
        tags.apply_to_asset(asset_id, 9999)


def test_asset_listing_detail_and_lifecycle(
    env: tuple[SqliteDatabase, InProcessEventBus, list[Event]],
) -> None:
    db, bus, events = env
    assets = AssetService(db, bus)
    tags = TagService(db, bus)
    notes = NoteService(db, bus)
    a1 = _seed_asset(db, "b3:1", "first")
    a2 = _seed_asset(db, "b3:2", "second")
    kill = tags.create(name="kill", color="#56c271")
    tags.apply_to_asset(a1, kill.id)
    notes.set_general(a1, "general blah")
    notes.add_timestamped(a1, 5000, "at five seconds")

    page = assets.list_assets(filter=AssetFilter(), sort_key="added_desc", offset=0, limit=10)
    assert page.total == 2
    by_hash = {s.asset.identity_hash: s for s in page.items}
    assert by_hash["b3:1"].tag_ids == [kill.id]
    assert by_hash["b3:1"].note_count == 2
    assert by_hash["b3:1"].is_new is True
    assert by_hash["b3:2"].tag_ids == [] and by_hash["b3:2"].note_count == 0

    detail = assets.get_detail(a1)
    assert detail.asset.id == a1
    assert detail.tag_ids == [kill.id]
    assert detail.general_note is not None and detail.general_note.body == "general blah"
    assert [n.note.body for n in detail.timestamped_notes] == ["at five seconds"]

    assets.update_title(a1, "renamed")
    assert assets.get(a1).title == "renamed"  # type: ignore[union-attr]
    assets.mark_opened(a1)
    page2 = assets.list_assets(filter=AssetFilter(never_opened=True), sort_key="added_desc", offset=0, limit=10)
    assert [s.asset.identity_hash for s in page2.items] == ["b3:2"]

    assets.delete(a2)
    page3 = assets.list_assets(filter=AssetFilter(), sort_key="added_desc", offset=0, limit=10)
    assert page3.total == 1
    with pytest.raises(NotFoundError):
        assets.get_detail(a2)


def test_note_validation_and_general_note_upsert(
    env: tuple[SqliteDatabase, InProcessEventBus, list[Event]],
) -> None:
    db, bus, _events = env
    notes = NoteService(db, bus)
    asset_id = _seed_asset(db, "b3:n")
    first = notes.set_general(asset_id, "v1")
    second = notes.set_general(asset_id, "v2")
    assert first.note.id == second.note.id and second.note.body == "v2"  # upsert, same row
    with pytest.raises(InvalidInputError):
        notes.add_timestamped(asset_id, -1, "bad")
    ts = notes.add_timestamped(asset_id, 1000, "ok")
    assert ts.note.timestamp_ms == 1000
    notes.update(ts.note.id, "edited")
    assert [n.note.body for n in notes.list_for_asset(asset_id) if n.note.timestamp_ms == 1000] == ["edited"]
    notes.delete(ts.note.id)
    assert [n for n in notes.list_for_asset(asset_id) if n.note.timestamp_ms == 1000] == []
