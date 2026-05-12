"""Downloading a static ffmpeg / ffprobe build on demand.

We fetch BtbN's *static* win64 GPL build -- a single ``ffmpeg.exe`` + ``ffprobe.exe`` with no extra
DLLs -- from the stable "latest release" redirect GitHub maintains, and place just those two
executables into a target directory (typically ``<data_dir>/bin``). The download is streamed to a
temp file inside the target directory and the binaries are written via a ``.part`` file then renamed,
so a half-finished download never leaves a broken ``ffmpeg.exe`` behind. Windows only.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

_log = logging.getLogger(__name__)

# GitHub keeps this URL pointed at the newest release's asset (a 302 to objects.githubusercontent.com,
# which the standard-library opener follows). The "gpl" (not "gpl-shared") build is statically linked.
FFMPEG_DOWNLOAD_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
)
_WANTED: tuple[str, ...] = ("ffmpeg.exe", "ffprobe.exe")
_CHUNK = 1 << 18  # 256 KiB

#: ``(bytes_downloaded, bytes_total)`` -- ``bytes_total`` is ``0`` when the server doesn't send a length.
ProgressCb = Callable[[int, int], None]


class FfmpegInstallError(Exception):
    """The on-demand ffmpeg download / extraction failed (network, bad archive, unsupported OS, ...)."""


def download_ffmpeg(
    target_dir: Path, *, progress: ProgressCb | None = None, url: str = FFMPEG_DOWNLOAD_URL
) -> tuple[Path, Path]:
    """Download a static win64 ffmpeg build and place ``ffmpeg.exe`` + ``ffprobe.exe`` in
    ``target_dir`` (created if needed). Returns ``(ffmpeg_path, ffprobe_path)``.

    :raises FfmpegInstallError: on any failure -- not running on Windows, no network, the archive is
        corrupt, or it doesn't contain the expected executables. The target directory is left as
        untouched as possible (the temp download file is always removed).
    """
    if sys.platform != "win32":
        raise FfmpegInstallError(
            "Automatic ffmpeg install is only available on Windows. On Linux/macOS, install ffmpeg "
            "with your package manager (e.g. 'apt install ffmpeg' or 'brew install ffmpeg') and "
            "reopen the app, or point it at an existing build in Settings."
        )
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FfmpegInstallError(f"Cannot create {target_dir}: {exc}") from exc
    tmp_zip = target_dir / f".ffmpeg-download-{os.getpid()}.zip.part"
    try:
        _stream_to_file(url, tmp_zip, progress)
        ffmpeg_path, ffprobe_path = _extract_binaries(tmp_zip, target_dir)
    except FfmpegInstallError:
        raise
    except (OSError, zipfile.BadZipFile, urllib.error.URLError) as exc:
        raise FfmpegInstallError(f"Could not install ffmpeg: {exc}") from exc
    finally:
        tmp_zip.unlink(missing_ok=True)
    _log.info("ffmpeg installed: %s, %s", ffmpeg_path, ffprobe_path)
    return ffmpeg_path, ffprobe_path


def _stream_to_file(url: str, dest: Path, progress: ProgressCb | None) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Clippycap"})
    with urllib.request.urlopen(request, timeout=60) as response:
        try:
            total = int(response.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        done = 0
        with dest.open("wb") as out:
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    if not dest.exists() or dest.stat().st_size == 0:
        raise FfmpegInstallError("The downloaded ffmpeg archive was empty.")


def _extract_binaries(zip_path: Path, target_dir: Path) -> tuple[Path, Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    extracted: dict[str, Path] = {}
    with zipfile.ZipFile(zip_path) as archive:
        # the build is a single top-level folder containing bin/ffmpeg.exe etc.; match on base name.
        members: dict[str, zipfile.ZipInfo] = {}
        for info in archive.infolist():
            if info.is_dir():
                continue
            base = info.filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
            members.setdefault(base, info)
        for name in _WANTED:
            member = members.get(name)
            if member is None:
                raise FfmpegInstallError(f"{name} was not found inside the downloaded ffmpeg archive.")
            part = target_dir / f".{name}.part"
            with archive.open(member) as src, part.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            final = target_dir / name
            part.replace(final)
            extracted[name] = final
    return extracted["ffmpeg.exe"], extracted["ffprobe.exe"]
