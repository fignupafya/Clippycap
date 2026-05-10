"""Integration tests for the library scanner (discovery, hashing, rename/move/missing/re-find)."""

from __future__ import annotations

from pathlib import Path

from clippycap.core.entities import Source, Tag
from clippycap.core.events import AssetAdded, AssetMissing, Event
from clippycap.core.query import AssetFilter
from clippycap.infra.config import load_config
from clippycap.infra.db.database import SqliteDatabase
from clippycap.infra.scan.hashing import Blake3IdentityStrategy
from clippycap.infra.scan.scanner import LibraryScanner
from clippycap.plugins_runtime.event_bus import InProcessEventBus

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOML = REPO_ROOT / "config" / "default.toml"


class _StubVideoProvider:
    """A minimal MediaTypeProvider for tests (the real VideoMediaType uses ffmpeg)."""

    media_type = "video"
    extensions = frozenset({"mp4"})
    identity_strategy_name = "blake3"
    player_kind = "video"

    def detect(self, path: Path) -> bool:
        return path.suffix.lstrip(".").lower() in self.extensions

    def extract_metadata(self, path: Path) -> dict[str, object]:
        return {"recorded_at": "2026-01-01T00:00:00+00:00", "duration_ms": 1000}

    def make_thumbnail(self, path: Path, out_path: Path, *, metadata: object) -> bool:
        return False

    def display_title(self, path: Path, metadata: object) -> str:
        return path.stem


def _make_scanner(db: SqliteDatabase) -> tuple[LibraryScanner, list[Event]]:
    cfg = load_config(
        default_path=DEFAULT_TOML, data_dir_override=db.path.parent / "data",
        install_dir_override=db.path.parent / "install", write_local_on_first_run=False,
        env={"CLIPPYCAP__SCAN__SKIP_MODIFIED_WITHIN_SECONDS": "0"},   # scan files the test just made
    )
    bus = InProcessEventBus()
    events: list[Event] = []
    bus.subscribe_all(events.append)
    scanner = LibraryScanner(db, [_StubVideoProvider()], {"blake3": Blake3IdentityStrategy()}, bus, cfg)
    return scanner, events


def _new_db(tmp_path: Path) -> SqliteDatabase:
    db = SqliteDatabase(tmp_path / "library.sqlite")
    db.initialise()
    return db


def _add_source(db: SqliteDatabase, folder: Path) -> Source:
    with db.transaction() as uow:
        return uow.sources.add(Source(path=str(folder.resolve()), recursive=True, media_types=["video"]))


def _missing_count(db: SqliteDatabase) -> int:
    with db.transaction() as uow:
        _, total = uow.assets.search(filter=AssetFilter(only_missing=True), sort_key="added_desc", offset=0, limit=99)
        return total


def test_scan_discovers_dedups_and_re_scan_is_clean(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    library.mkdir()
    (library / "a.mp4").write_text("AAAA")
    (library / "b.mp4").write_text("BBBB")
    (library / "copy_of_a.mp4").write_text("AAAA")     # identical content to a.mp4
    (library / "notes.txt").write_text("not a video")
    db = _new_db(tmp_path)
    scanner, events = _make_scanner(db)
    source = _add_source(db, library)

    result = scanner.scan(source)
    assert result.added == 2 and result.errors == []
    assert sum(isinstance(e, AssetAdded) for e in events) == 2
    with db.transaction() as uow:
        _, total = uow.assets.search(filter=AssetFilter(), sort_key="added_desc", offset=0, limit=10)
        assert total == 2
        a = uow.assets.find_by_path(str((library / "a.mp4").resolve()))
        assert a is not None and a.id is not None
        paths = {p.path for p in uow.assets.get_paths(a.id)}
        assert paths == {str((library / "a.mp4").resolve()), str((library / "copy_of_a.mp4").resolve())}

    events.clear()
    result = scanner.scan(source)
    assert result.added == 0 and result.missing == 0
    assert result.updated == 3   # 3 .mp4 files re-found (2 distinct assets: a.mp4 and its copy share content)


def test_scan_marks_missing_then_clears_on_return(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    library.mkdir()
    clip = library / "clip.mp4"
    clip.write_text("DATA")
    db = _new_db(tmp_path)
    scanner, events = _make_scanner(db)
    source = _add_source(db, library)
    scanner.scan(source)

    clip.unlink()
    events.clear()
    result = scanner.scan(source)
    assert result.missing == 1
    assert sum(isinstance(e, AssetMissing) for e in events) == 1
    assert _missing_count(db) == 1

    clip.write_text("DATA")
    result = scanner.scan(source)
    assert result.missing == 0 and result.updated == 1
    assert _missing_count(db) == 0


def test_scan_handles_move_within_library_keeping_tags(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    (library / "sub").mkdir(parents=True)
    clip = library / "clip.mp4"
    clip.write_text("MOVED")
    db = _new_db(tmp_path)
    scanner, _ = _make_scanner(db)
    source = _add_source(db, library)
    scanner.scan(source)

    with db.transaction() as uow:
        asset = uow.assets.find_by_path(str(clip.resolve()))
        assert asset is not None and asset.id is not None
        tag = uow.tags.add(Tag(name="kill", color="#56c271"))
        uow.tags.apply(asset.id, tag.id)
        asset_id = asset.id

    moved = library / "sub" / "renamed.mp4"
    clip.rename(moved)
    result = scanner.scan(source)
    assert result.added == 0 and result.missing == 0
    with db.transaction() as uow:
        again = uow.assets.find_by_path(str(moved.resolve()))
        assert again is not None and again.id == asset_id
        assert uow.tags.tag_ids_for_asset(asset_id)
        present = {p.path for p in uow.assets.get_paths(asset_id) if p.present}
        assert present == {str(moved.resolve())}
