"""Tests for the on-demand ffmpeg downloader (download via a local file:// URL; extract a synthetic zip)."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from clippycap.infra.media.ffmpeg_install import FfmpegInstallError, _extract_binaries, download_ffmpeg

_windows_only = pytest.mark.skipif(sys.platform != "win32", reason="automatic ffmpeg install is Windows-only")


def _make_archive(path: Path, names: list[str]) -> None:
    """Write a zip mimicking BtbN's layout (a single top-level folder containing bin/<exe>)."""
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(f"ffmpeg-master-latest-win64-gpl/bin/{name}", f"#!fake {name}\n".encode())


def test_extract_binaries_picks_the_two_exes(tmp_path: Path) -> None:
    archive = tmp_path / "ffmpeg.zip"
    _make_archive(archive, ["ffmpeg.exe", "ffprobe.exe"])
    out = tmp_path / "bin"
    out.mkdir()
    ffmpeg_path, ffprobe_path = _extract_binaries(archive, out)
    assert ffmpeg_path == out / "ffmpeg.exe" and ffmpeg_path.read_bytes() == b"#!fake ffmpeg.exe\n"
    assert ffprobe_path == out / "ffprobe.exe" and ffprobe_path.is_file()
    assert sorted(p.name for p in out.iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]   # nothing else extracted


def test_extract_binaries_complains_when_a_binary_is_missing(tmp_path: Path) -> None:
    archive = tmp_path / "ffmpeg.zip"
    _make_archive(archive, ["ffmpeg.exe"])              # no ffprobe.exe
    with pytest.raises(FfmpegInstallError, match=r"ffprobe\.exe"):
        _extract_binaries(archive, tmp_path / "bin")


@_windows_only
def test_download_ffmpeg_end_to_end_via_file_url(tmp_path: Path) -> None:
    archive = tmp_path / "src" / "ffmpeg.zip"
    archive.parent.mkdir()
    _make_archive(archive, ["ffmpeg.exe", "ffprobe.exe"])
    seen: list[tuple[int, int]] = []
    target = tmp_path / "data" / "bin"
    ffmpeg_path, ffprobe_path = download_ffmpeg(target, progress=lambda d, t: seen.append((d, t)), url=archive.as_uri())
    assert ffmpeg_path == target / "ffmpeg.exe" and ffmpeg_path.is_file()
    assert ffprobe_path == target / "ffprobe.exe" and ffprobe_path.is_file()
    assert not any(p.suffix == ".part" for p in target.iterdir())      # temp files cleaned up
    assert seen and seen[-1][0] > 0                                    # progress was reported


@_windows_only
def test_download_ffmpeg_reports_a_clear_error_on_a_bad_url(tmp_path: Path) -> None:
    missing = (tmp_path / "does-not-exist.zip").as_uri()
    with pytest.raises(FfmpegInstallError):
        download_ffmpeg(tmp_path / "bin", url=missing)
