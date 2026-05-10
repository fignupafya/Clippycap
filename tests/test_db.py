"""Integration tests for the SQLite persistence layer (schema, migrations, repositories, FTS)."""

from __future__ import annotations

from pathlib import Path

import pytest

from clippycap.core.entities import (
    Asset,
    Note,
    Reference,
    ReferenceType,
    SavedView,
    Source,
    Tag,
)
from clippycap.core.errors import ConflictError, InvalidInputError
from clippycap.core.query import AssetFilter
from clippycap.infra.db.database import SqliteDatabase


@pytest.fixture
def db(tmp_path: Path) -> SqliteDatabase:
    database = SqliteDatabase(tmp_path / "library.sqlite")
    database.initialise()
    return database


def _add_asset(uow, h: str, title: str = "clip", media_type: str = "video", **meta) -> Asset:
    return uow.assets.add(Asset(identity_hash=h, media_type=media_type, title=title, size_bytes=1, metadata=dict(meta)))


def test_initialise_is_idempotent(tmp_path: Path) -> None:
    database = SqliteDatabase(tmp_path / "x.sqlite")
    database.initialise()
    database.initialise()  # second call must not fail
    with database.transaction() as uow:
        assert uow.tags.list_all() == []


def test_tags_crud_and_links(db: SqliteDatabase) -> None:
    with db.transaction() as uow:
        a = _add_asset(uow, "b3:a", "video a")
        kill = uow.tags.add(Tag(name="kill", color="#56c271", icon="boom"))
        pos = uow.tags.add(Tag(name="position", color="#ef5b5b", sort_order=5))
        assert kill.id is not None and kill.created_at is not None
        assert uow.tags.get_by_name("kill").name == "kill"
        assert [t.name for t in uow.tags.list_all()] == ["kill", "position"]
        with pytest.raises(ConflictError):
            uow.tags.add(Tag(name="kill", color="#000000"))
        assert uow.tags.apply(a.id, kill.id) is True
        assert uow.tags.apply(a.id, kill.id) is False          # already applied
        assert uow.tags.apply(a.id, pos.id) is True
        assert uow.tags.tag_ids_for_asset(a.id) == [kill.id, pos.id]
        assert uow.tags.tag_ids_for_assets([a.id]) == {a.id: [kill.id, pos.id]}
        assert uow.tags.asset_count(kill.id) == 1
        assert uow.tags.unapply(a.id, pos.id) is True
        assert uow.tags.unapply(a.id, pos.id) is False
        uow.tags.reorder([pos.id, kill.id])
        assert [t.name for t in uow.tags.list_all()] == ["position", "kill"]
        pos.color = "#aa0000"
        uow.tags.update(pos)
        assert uow.tags.get(pos.id).color == "#aa0000"
        uow.tags.delete(pos.id)
        assert uow.tags.get(pos.id) is None


def test_asset_search_filters_and_paging(db: SqliteDatabase) -> None:
    with db.transaction() as uow:
        kill = uow.tags.add(Tag(name="kill", color="#56c271"))
        funny = uow.tags.add(Tag(name="funny", color="#ef7bb5"))
        a1 = _add_asset(uow, "b3:1", "soldier midfrag", recorded_at="2026-05-09T19:00:00+00:00", duration_ms=58000)
        a2 = _add_asset(uow, "b3:2", "scout double",   recorded_at="2026-05-09T20:00:00+00:00", duration_ms=12000)
        a3 = _add_asset(uow, "b3:3", "demoman pipes",  recorded_at="2026-05-08T10:00:00+00:00", duration_ms=40000)
        uow.tags.apply(a1.id, kill.id)
        uow.tags.apply(a2.id, kill.id)
        uow.tags.apply(a2.id, funny.id)
        # by media type / all
        items, total = uow.assets.search(filter=AssetFilter(), sort_key="recorded_desc", offset=0, limit=10)
        assert total == 3 and [x.identity_hash for x in items] == ["b3:2", "b3:1", "b3:3"]
        # tags_all
        items, total = uow.assets.search(filter=AssetFilter(tags_all=[kill.id, funny.id]), sort_key="added_desc", offset=0, limit=10)
        assert [x.identity_hash for x in items] == ["b3:2"]
        # tags_any
        items, total = uow.assets.search(filter=AssetFilter(tags_any=[kill.id]), sort_key="recorded_asc", offset=0, limit=10)
        assert [x.identity_hash for x in items] == ["b3:1", "b3:2"]
        # tags_none
        items, total = uow.assets.search(filter=AssetFilter(tags_none=[kill.id]), sort_key="added_desc", offset=0, limit=10)
        assert [x.identity_hash for x in items] == ["b3:3"]
        # untagged_only
        items, total = uow.assets.search(filter=AssetFilter(untagged_only=True), sort_key="added_desc", offset=0, limit=10)
        assert total == 1 and items[0].identity_hash == "b3:3"
        # text / FTS over titles
        items, total = uow.assets.search(filter=AssetFilter(text="scout"), sort_key="added_desc", offset=0, limit=10)
        assert [x.identity_hash for x in items] == ["b3:2"]
        # paging
        items, total = uow.assets.search(filter=AssetFilter(), sort_key="recorded_desc", offset=1, limit=1)
        assert total == 3 and [x.identity_hash for x in items] == ["b3:1"]
        # unknown sort key
        with pytest.raises(InvalidInputError):
            uow.assets.search(filter=AssetFilter(), sort_key="nonsense", offset=0, limit=10)


def test_asset_paths_and_missing(db: SqliteDatabase) -> None:
    with db.transaction() as uow:
        a = _add_asset(uow, "b3:p", "clip")
        uow.assets.upsert_path(a.id, r"D:\Yakalamalar\a.mp4", "vol-c")
        uow.assets.upsert_path(a.id, r"D:\Yakalamalar\sub\a.mp4", "vol-c")
        uow.assets.upsert_path(a.id, r"E:\Other\a.mp4", "vol-e")
        paths = uow.assets.get_paths(a.id)
        assert {p.path for p in paths} == {r"D:\Yakalamalar\a.mp4", r"D:\Yakalamalar\sub\a.mp4", r"E:\Other\a.mp4"}
        assert all(p.present for p in paths)
        assert uow.assets.find_by_path(r"E:\Other\a.mp4").identity_hash == "b3:p"
        # reconcile: a scan of D:\Yakalamalar saw only the top-level path
        affected = uow.assets.reconcile_paths_under(r"D:\Yakalamalar", [r"D:\Yakalamalar\a.mp4"])
        assert affected == [a.id]
        present = {p.path for p in uow.assets.get_paths(a.id) if p.present}
        assert present == {r"D:\Yakalamalar\a.mp4", r"E:\Other\a.mp4"}      # E:\ untouched (different root)
        assert uow.assets.all_paths_absent(a.id) is False
        # now drop the E:\ one too and mark D:\ absent -> all absent
        uow.assets.reconcile_paths_under(r"E:\Other", [])
        uow.assets.reconcile_paths_under(r"D:\Yakalamalar", [])
        assert uow.assets.all_paths_absent(a.id) is True
        uow.assets.set_missing(a.id, True)
        items, total = uow.assets.search(filter=AssetFilter(only_missing=True), sort_key="added_desc", offset=0, limit=10)
        assert total == 1


def test_assets_touch_and_never_opened(db: SqliteDatabase) -> None:
    with db.transaction() as uow:
        a = _add_asset(uow, "b3:o", "clip")
        b = _add_asset(uow, "b3:n", "clip2")
        uow.assets.touch_opened(a.id)
        items, total = uow.assets.search(filter=AssetFilter(never_opened=True), sort_key="added_desc", offset=0, limit=10)
        assert [x.identity_hash for x in items] == ["b3:n"]
        assert uow.assets.get(a.id).last_opened_at is not None


def test_notes_general_and_timestamped(db: SqliteDatabase) -> None:
    with db.transaction() as uow:
        a = _add_asset(uow, "b3:nn", "clip")
        tag = uow.tags.add(Tag(name="mistake", color="#ef5b5b"))
        g = uow.notes.add(Note(asset_id=a.id, body="general note"))
        assert uow.notes.general_note(a.id).body == "general note"
        with pytest.raises(ConflictError):
            uow.notes.add(Note(asset_id=a.id, body="second general"))   # only one general note allowed
        n1 = uow.notes.add(Note(asset_id=a.id, body="at 8s", timestamp_ms=8000))
        n2 = uow.notes.add(Note(asset_id=a.id, body="at 3s", timestamp_ms=3000))
        listed = uow.notes.list_for_asset(a.id)
        assert [x.body for x in listed] == ["general note", "at 3s", "at 8s"]   # general first, then by timestamp
        uow.notes.set_tags(n1.id, [tag.id])
        assert uow.notes.tag_ids_for_note(n1.id) == [tag.id]
        assert uow.notes.count_for_asset(a.id) == 3
        assert uow.notes.counts_for_assets([a.id]) == {a.id: 3}
        # text search hits note bodies too
        items, _ = uow.assets.search(filter=AssetFilter(text="general"), sort_key="added_desc", offset=0, limit=10)
        assert [x.identity_hash for x in items] == ["b3:nn"]
        g.body = "edited"
        uow.notes.update(g)
        assert uow.notes.get(g.id).body == "edited"
        uow.notes.delete(n2.id)
        assert uow.notes.count_for_asset(a.id) == 2


def test_references_and_types(db: SqliteDatabase) -> None:
    with db.transaction() as uow:
        a = _add_asset(uow, "b3:ra", "a")
        b = _add_asset(uow, "b3:rb", "b")
        rt = uow.reference_types.add(ReferenceType(name="better version of", reverse_name="worse version of", color="#56c271"))
        assert [x.name for x in uow.reference_types.list_all()] == ["better version of"]
        ref = uow.references.add(Reference(from_asset_id=a.id, to_asset_id=b.id, type_id=rt.id, label="compare",
                                           from_timestamp_ms=1000, to_timestamp_ms=2000, note="x"))
        assert [r.id for r in uow.references.list_outgoing(a.id)] == [ref.id]
        assert [r.id for r in uow.references.list_incoming(b.id)] == [ref.id]
        assert uow.references.count_for_asset(a.id) == 1
        assert uow.references.count_for_asset(b.id) == 1
        assert uow.references.counts_for_assets([a.id, b.id]) == {a.id: 1, b.id: 1}
        uow.reference_types.delete(rt.id)
        assert uow.references.get(ref.id).type_id is None      # ON DELETE SET NULL
        uow.references.delete(ref.id)
        assert uow.references.get(ref.id) is None


def test_sources_and_saved_views(db: SqliteDatabase) -> None:
    with db.transaction() as uow:
        s = uow.sources.add(Source(path=r"D:\Yakalamalar", recursive=True, media_types=["video"]))
        assert uow.sources.get_by_path(r"D:\Yakalamalar").id == s.id
        with pytest.raises(ConflictError):
            uow.sources.add(Source(path=r"D:\Yakalamalar"))
        s.enabled = False
        uow.sources.update(s)
        uow.sources.touch_scanned(s.id)
        reread = uow.sources.get(s.id)
        assert reread.enabled is False and reread.last_scanned_at is not None and reread.media_types == ["video"]
        v = uow.saved_views.add(SavedView(name="untagged clips", filter_json='{"untagged_only": true}', sort_key="added_desc"))
        v2 = uow.saved_views.add(SavedView(name="best", filter_json="{}", sort_key="added_desc"))
        uow.saved_views.reorder([v2.id, v.id])
        assert [x.name for x in uow.saved_views.list_all()] == ["best", "untagged clips"]
        uow.saved_views.delete(v2.id)
        assert len(uow.saved_views.list_all()) == 1
        uow.sources.delete(s.id)
        assert uow.sources.list_all() == []


def test_hash_cache_and_meta(db: SqliteDatabase) -> None:
    with db.transaction() as uow:
        assert uow.hash_cache.get("/x/a.mp4", 100, 111) is None
        uow.hash_cache.put("/x/a.mp4", 100, 111, "b3:abc")
        assert uow.hash_cache.get("/x/a.mp4", 100, 111) == "b3:abc"
        assert uow.hash_cache.get("/x/a.mp4", 100, 222) is None     # mtime changed -> stale
        assert uow.hash_cache.get("/x/a.mp4", 200, 111) is None     # size changed -> stale
        uow.hash_cache.put("/x/a.mp4", 100, 222, "b3:def")          # upsert
        assert uow.hash_cache.get("/x/a.mp4", 100, 222) == "b3:def"
        uow.hash_cache.forget("/x/a.mp4")
        assert uow.hash_cache.get("/x/a.mp4", 100, 222) is None
        assert uow.meta.get("first_run") is None
        uow.meta.set("first_run", "done")
        assert uow.meta.get("first_run") == "done"
        uow.meta.set("first_run", "redone")
        assert uow.meta.get("first_run") == "redone"


def test_rollback_on_exception(db: SqliteDatabase) -> None:
    with pytest.raises(RuntimeError), db.transaction() as uow:
        uow.tags.add(Tag(name="temp", color="#000000"))
        raise RuntimeError("boom")
    with db.transaction() as uow:
        assert uow.tags.get_by_name("temp") is None
