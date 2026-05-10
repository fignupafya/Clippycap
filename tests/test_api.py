"""Smoke tests for the HTTP API (FastAPI TestClient -- no real server)."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from clippycap.api.app import create_app
from clippycap.app.bootstrap import build_application

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOML = REPO_ROOT / "config" / "default.toml"


def _build(tmp_path: Path, **extra_env: str) -> TestClient:
    env = {"CLIPPYCAP__MEDIA__FFMPEG__ENABLED": "false", **extra_env}
    app = build_application(
        default_toml_path=DEFAULT_TOML, data_dir_override=tmp_path / "data",
        install_dir_override=tmp_path / "inst", env=env,
    )
    return TestClient(create_app(app))


def test_health_and_empty_library(tmp_path: Path) -> None:
    client = _build(tmp_path)
    health = client.get("/api/health").json()
    assert health["media_types"] == ["video"] and health["plugins"] == []
    assert client.get("/api/assets").json() == {"items": [], "total": 0, "offset": 0, "limit": 100}
    assert client.get("/api/tags").json() == []
    assert [rt["name"] for rt in client.get("/api/reference-types").json()] == [
        "better version of", "same mistake as", "see also", "continues from", "excerpt of",
    ]


def test_tag_crud_and_errors(tmp_path: Path) -> None:
    client = _build(tmp_path)
    created = client.post("/api/tags", json={"name": "kill", "color": "#56c271", "sort_order": 0})
    assert created.status_code == 201
    tag_id = created.json()["id"]
    assert [t["name"] for t in client.get("/api/tags").json()] == ["kill"]
    updated = client.put(f"/api/tags/{tag_id}", json={
        "name": "kill!", "color": "#00ff00", "icon": "boom", "image_ref": None,
        "description": "a clean kill", "sort_order": 1,
    })
    assert updated.status_code == 200 and updated.json()["name"] == "kill!"
    assert client.post("/api/tags", json={"name": "kill!", "color": "#000000"}).status_code == 409
    assert client.get("/api/assets/9999").status_code == 404
    assert client.post("/api/tags", json={"name": "", "color": "#000000"}).status_code == 422  # validation
    assert client.delete(f"/api/tags/{tag_id}").status_code == 204
    assert client.get("/api/tags").json() == []


def test_scan_and_stream_via_api(tmp_path: Path) -> None:
    client = _build(tmp_path, CLIPPYCAP__SCAN__SKIP_MODIFIED_WITHIN_SECONDS="0")
    library = tmp_path / "lib"
    library.mkdir()
    (library / "a.mp4").write_text("AAAA")
    (library / "b.mp4").write_text("BBBB")
    src = client.post("/api/sources", json={"path": str(library), "recursive": True, "media_types": ["video"]})
    assert src.status_code == 201
    started = client.post(f"/api/sources/{src.json()['id']}/scan")
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if client.get(f"/api/jobs/{job_id}").json()["state"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert client.get(f"/api/jobs/{job_id}").json()["state"] == "done"
    listing = client.get("/api/assets").json()
    assert listing["total"] == 2
    asset_id = listing["items"][0]["id"]
    detail = client.get(f"/api/assets/{asset_id}").json()
    assert "timestamped_notes" in detail and detail["thumbnail_url"] == f"/thumbnails/{asset_id}"
    full = client.get(f"/media/{asset_id}/stream")
    assert full.status_code == 200 and len(full.content) == 4 and full.content in (b"AAAA", b"BBBB")
    ranged = client.get(f"/media/{asset_id}/stream", headers={"Range": "bytes=1-2"})
    assert ranged.status_code == 206 and ranged.headers["content-range"] == "bytes 1-2/4"
    assert ranged.content == full.content[1:3]


def test_config_endpoint(tmp_path: Path) -> None:
    config = _build(tmp_path).get("/api/config").json()
    assert config["app"]["name"] == "Clippycap" and config["identity"]["strategy"] == "blake3"
    assert config["media"]["video"]["extensions"][0] == "mp4"


def test_thumbnail_unavailable_then_client_upload(tmp_path: Path) -> None:
    client = _build(tmp_path, CLIPPYCAP__SCAN__SKIP_MODIFIED_WITHIN_SECONDS="0")
    library = tmp_path / "lib"
    library.mkdir()
    (library / "clip.mp4").write_text("VIDEO-BYTES")
    src = client.post("/api/sources", json={"path": str(library), "recursive": True, "media_types": ["video"]})
    job_id = client.post(f"/api/sources/{src.json()['id']}/scan").json()["job_id"]
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and client.get(f"/api/jobs/{job_id}").json()["state"] not in ("done", "error"):
        time.sleep(0.02)
    asset_id = client.get("/api/assets").json()["items"][0]["id"]

    # ffmpeg is disabled in the test build -> a missing thumbnail is 503 (the client should make one), not 404
    missing = client.get(f"/thumbnails/{asset_id}")
    assert missing.status_code == 503
    assert missing.json()["reason"] == "ffmpeg_unavailable"
    assert missing.json()["stream_url"] == f"/media/{asset_id}/stream"

    # the frontend captures a frame and uploads it; afterwards GET serves it back
    up = client.put(
        f"/thumbnails/{asset_id}", content=b"\xff\xd8\xff-fake-jpeg", headers={"content-type": "image/jpeg"}
    )
    assert up.status_code == 204
    got = client.get(f"/thumbnails/{asset_id}")
    assert got.status_code == 200 and got.content == b"\xff\xd8\xff-fake-jpeg"

    assert client.get("/thumbnails/9999").status_code == 404
    assert client.put("/thumbnails/9999", content=b"x", headers={"content-type": "image/jpeg"}).status_code == 404
    assert client.put(f"/thumbnails/{asset_id}", content=b"", headers={"content-type": "image/jpeg"}).status_code == 400
    assert client.get("/api/health").json()["ffmpeg"] is False
