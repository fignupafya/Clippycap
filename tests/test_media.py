"""Tests for ffmpeg/ffprobe resolution and the ffprobe-JSON parser (no real subprocess required)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from clippycap.infra.config import Config, load_config
from clippycap.infra.media.ffmpeg import resolve_ffmpeg_tools
from clippycap.infra.media.video_metadata import NoOpMetadataExtractor, parse_ffprobe
from clippycap.infra.media.video_thumbnail import UnavailableThumbnailer

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOML = REPO_ROOT / "config" / "default.toml"


def _cfg(tmp_path: Path, **env: str) -> Config:
    return load_config(
        default_path=DEFAULT_TOML, data_dir_override=tmp_path / "data",
        install_dir_override=tmp_path / "install", env=env, write_local_on_first_run=False,
    )


def test_resolve_returns_none_when_disabled(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, CLIPPYCAP__MEDIA__FFMPEG__ENABLED="false")
    assert resolve_ffmpeg_tools(cfg, tmp_path / "install") == (None, None)


def test_resolve_missing_absolute_path_is_none(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        CLIPPYCAP__MEDIA__FFMPEG__FFMPEG_PATH=str(tmp_path / "nope" / "ffmpeg"),
        CLIPPYCAP__MEDIA__FFMPEG__FFPROBE_PATH=str(tmp_path / "nope" / "ffprobe"),
    )
    assert resolve_ffmpeg_tools(cfg, tmp_path / "install") == (None, None)


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed"
)
def test_resolve_auto_finds_path_install(tmp_path: Path) -> None:
    ffmpeg, ffprobe = resolve_ffmpeg_tools(_cfg(tmp_path), tmp_path / "install")
    assert ffmpeg is not None and ffprobe is not None


def test_parse_ffprobe_extracts_fields() -> None:
    data = {
        "format": {"duration": "58.42", "tags": {"creation_time": "2026-05-09T19:36:44.000000Z"}},
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
             "avg_frame_rate": "60/1", "r_frame_rate": "60/1"},
        ],
    }
    assert parse_ffprobe(data) == {
        "duration_ms": 58420, "recorded_at": "2026-05-09T19:36:44.000000Z",
        "width": 1920, "height": 1080, "codec": "h264", "fps": 60.0,
    }


def test_parse_ffprobe_handles_missing_bits() -> None:
    assert parse_ffprobe({}) == {}
    assert parse_ffprobe({"format": {}, "streams": []}) == {}
    assert parse_ffprobe({"streams": [{"codec_type": "video"}]}) == {}


def test_no_op_extractor_and_unavailable_thumbnailer(tmp_path: Path) -> None:
    assert NoOpMetadataExtractor().extract(tmp_path / "x.mp4") == {}
    thumb = UnavailableThumbnailer()
    assert thumb.available is False
    assert thumb.make(tmp_path / "x.mp4", tmp_path / "x.webp", metadata={}) is False
