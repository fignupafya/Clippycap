"""Tests for the remaining application services: scan, references, sources, saved views, job queue."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from clippycap.app.jobs import ThreadJobQueue
from clippycap.app.reference_service import ReferenceService, ReferenceTypeService
from clippycap.app.scan_service import ScanService
from clippycap.app.source_service import SavedViewService, SourceService
from clippycap.core.entities import Asset
from clippycap.core.errors import InvalidInputError, NotFoundError
from clippycap.core.events import Event, ScanCompleted, ScanStarted
from clippycap.core.query import AssetFilter
from clippycap.infra.config import load_config
from clippycap.infra.db.database import SqliteDatabase
from clippycap.infra.scan.hashing import Blake3IdentityStrategy
from clippycap.infra.scan.scanner import LibraryScanner
from clippycap.plugins_runtime.event_bus import InProcessEventBus

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOML = REPO_ROOT / "config" / "default.toml"


class _StubVideoProvider:
    media_type = "video"
    extensions = frozenset({"mp4"})
    identity_strategy_name = "blake3"
    player_kind = "video"

    def detect(self, path: Path) -> bool:
        return path.suffix.lstrip(".").lower() in self.extensions

    def extract_metadata(self, path: Path) -> dict[str, object]:
        return {"recorded_at": "2026-01-01T00:00:00+00:00"}

    def make_thumbnail(self, path: Path, out_path: Path, *, metadata: object) -> bool:
        return False

    def display_title(self, path: Path, metadata: object) -> str:
        return path.stem


def _wait(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for condition")


def _new_db(tmp_path: Path) -> SqliteDatabase:
    db = SqliteDatabase(tmp_path / "library.sqlite")
    db.initialise()
    return db


def test_thread_job_queue_runs_and_records_state() -> None:
    queue = ThreadJobQueue()
    box: list[int] = []
    job_id = queue.submit("count", lambda report: (report.update(1, 1, "done"), box.append(7)))
    _wait(lambda: (h := queue.get(job_id)) is not None and h.state == "done")
    handle = queue.get(job_id)
    assert handle is not None and handle.state == "done" and handle.scanned == 1
    assert box == [7]
    bad_id = queue.submit("boom", lambda report: (_ for _ in ()).throw(RuntimeError("x")))
    _wait(lambda: (h := queue.get(bad_id)) is not None and h.state == "error")
    assert queue.get(bad_id).error is not None  # type: ignore[union-attr]
    assert len(queue.list_all()) == 2
    queue.shutdown()


def test_scan_service_runs_a_scan_job(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    library.mkdir()
    (library / "a.mp4").write_text("AAAA")
    (library / "b.mp4").write_text("BBBB")
    db = _new_db(tmp_path)
    cfg = load_config(
        default_path=DEFAULT_TOML, data_dir_override=tmp_path / "data", install_dir_override=tmp_path / "inst",
        env={"CLIPPYCAP__SCAN__SKIP_MODIFIED_WITHIN_SECONDS": "0"}, write_local_on_first_run=False,
    )
    bus = InProcessEventBus()
    events: list[Event] = []
    bus.subscribe_all(events.append)
    scanner = LibraryScanner(db, [_StubVideoProvider()], {"blake3": Blake3IdentityStrategy()}, bus, cfg)
    jobs = ThreadJobQueue()
    sources = SourceService(db, bus)
    source = sources.create(str(library))
    assert source.id is not None
    scans = ScanService(db, scanner, jobs, bus)
    job_id = scans.scan_source(source.id)
    _wait(lambda: (h := jobs.get(job_id)) is not None and h.state in ("done", "error"))
    assert jobs.get(job_id).state == "done"  # type: ignore[union-attr]
    assert any(isinstance(e, ScanStarted) for e in events)
    completed = [e for e in events if isinstance(e, ScanCompleted)]
    assert len(completed) == 1 and completed[0].added == 2
    with db.transaction() as uow:
        _, total = uow.assets.search(filter=AssetFilter(), sort_key="added_desc", offset=0, limit=10)
        assert total == 2
    jobs.shutdown()


def test_reference_services(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    bus = InProcessEventBus()
    with db.transaction() as uow:
        a = uow.assets.add(Asset(identity_hash="b3:a", media_type="video", title="A", size_bytes=1))
        b = uow.assets.add(Asset(identity_hash="b3:b", media_type="video", title="B", size_bytes=1))
        a_id, b_id = a.id, b.id
    assert a_id is not None and b_id is not None
    rtypes = ReferenceTypeService(db)
    rt = rtypes.create(name="better version of", color="#56c271", reverse_name="worse version of")
    assert rt.id is not None
    assert [t.name for t in rtypes.list_all()] == ["better version of"]
    refs = ReferenceService(db, bus)
    ref = refs.create(from_asset_id=a_id, to_asset_id=b_id, type_id=rt.id, label="cmp",
                      from_timestamp_ms=1000, to_timestamp_ms=2000)
    listing_a = refs.for_asset(a_id)
    assert len(listing_a.outgoing) == 1 and listing_a.outgoing[0].type_name == "better version of"
    assert listing_a.outgoing[0].other_asset_title == "B"
    listing_b = refs.for_asset(b_id)
    assert len(listing_b.incoming) == 1 and listing_b.incoming[0].type_name == "worse version of"
    rtypes.update(rt.id, name="renamed", color="#000000", reverse_name=None, sort_order=3)
    rtypes.delete(rt.id)
    assert refs.for_asset(a_id).outgoing[0].type_name is None  # type linked via ON DELETE SET NULL
    assert ref.id is not None
    refs.delete(ref.id)
    assert refs.for_asset(a_id).outgoing == []
    with pytest.raises(NotFoundError):
        refs.create(from_asset_id=a_id, to_asset_id=9999)


def test_source_and_saved_view_services(tmp_path: Path) -> None:
    db = _new_db(tmp_path)
    bus = InProcessEventBus()
    sources = SourceService(db, bus)
    with pytest.raises(InvalidInputError):
        sources.create(str(tmp_path / "does-not-exist"))
    src = sources.create(str(tmp_path), recursive=False, media_types=["video"])
    assert src.id is not None and src.recursive is False
    src2 = sources.update(src.id, recursive=True, enabled=False, media_types=["video", "image"])
    assert src2.recursive is True and src2.enabled is False and src2.media_types == ["video", "image"]
    sources.delete(src.id)
    assert sources.list_all() == []
    with pytest.raises(NotFoundError):
        sources.delete(src.id)
    views = SavedViewService(db)
    v1 = views.create(name="untagged", filter_json='{"untagged_only": true}', sort_key="added_desc")
    v2 = views.create(name="best", filter_json="{}", sort_key="added_desc")
    assert v1.id is not None and v2.id is not None
    views.reorder([v2.id, v1.id])
    assert [v.name for v in views.list_all()] == ["best", "untagged"]
    views.update(v1.id, name="untagged clips", filter_json="{}", sort_key="title_asc", sort_order=5)
    views.delete(v2.id)
    assert [v.name for v in views.list_all()] == ["untagged clips"]
