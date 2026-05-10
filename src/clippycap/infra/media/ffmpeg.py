"""Locating ffmpeg / ffprobe.

The configured path is one of: ``"auto"`` (probe the bundled binary, then common install
locations, then ``PATH``), ``"@bundled"`` (only the binary shipped next to the app), or an absolute
path. A candidate is accepted only if running ``<exe> -version`` succeeds. Returns ``(None, None)``
when ffmpeg is disabled in the config or cannot be found.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from clippycap.infra.config import Config

_log = logging.getLogger(__name__)
_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
_VERSION_PROBE_TIMEOUT = 15


def _windows_dirs() -> Iterator[Path]:
    env = os.environ
    yield Path("C:/ffmpeg/bin")
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        if base := env.get(var):
            yield Path(base) / "ffmpeg" / "bin"
    if choco := env.get("ChocolateyInstall"):
        yield Path(choco) / "bin"
    yield Path("C:/ProgramData/chocolatey/bin")
    if profile := env.get("USERPROFILE"):
        yield Path(profile) / "scoop" / "shims"
        yield Path(profile) / "scoop" / "apps" / "ffmpeg" / "current" / "bin"
    if local := env.get("LOCALAPPDATA"):
        yield Path(local) / "Microsoft" / "WinGet" / "Links"
        yield Path(local)


def _posix_dirs() -> Iterator[Path]:
    for directory in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/snap/bin", "/bin"):
        yield Path(directory)


def _candidate_dirs(install_dir: Path) -> Iterator[Path]:
    yield install_dir / "bin"          # the bundled binary
    yield from (_windows_dirs() if sys.platform == "win32" else _posix_dirs())


def _is_runnable(exe: Path) -> bool:
    if not exe.is_file():
        return False
    try:
        completed = subprocess.run(
            [str(exe), "-version"], capture_output=True, timeout=_VERSION_PROBE_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _resolve(name: str, configured: str, install_dir: Path) -> Path | None:
    if configured == "@bundled":
        candidate = install_dir / "bin" / f"{name}{_EXE_SUFFIX}"
        return candidate if _is_runnable(candidate) else None
    if configured != "auto":
        candidate = Path(configured)
        return candidate if _is_runnable(candidate) else None
    for directory in _candidate_dirs(install_dir):
        candidate = directory / f"{name}{_EXE_SUFFIX}"
        if _is_runnable(candidate):
            return candidate
    on_path = shutil.which(name)
    return Path(on_path) if on_path and _is_runnable(Path(on_path)) else None


def resolve_ffmpeg_tools(config: Config, install_dir: Path) -> tuple[Path | None, Path | None]:
    """``(ffmpeg, ffprobe)`` absolute paths, or ``(None, None)`` if disabled or not found."""
    ffmpeg_cfg = config.media.ffmpeg
    if not ffmpeg_cfg.enabled:
        return (None, None)
    ffmpeg = _resolve("ffmpeg", ffmpeg_cfg.ffmpeg_path, install_dir)
    ffprobe = _resolve("ffprobe", ffmpeg_cfg.ffprobe_path, install_dir)
    if ffmpeg is None or ffprobe is None:
        _log.warning(
            "ffmpeg is enabled but was not fully located (ffmpeg=%s, ffprobe=%s); thumbnails and "
            "video metadata will fall back to the browser. Install ffmpeg or set its path in settings.",
            ffmpeg, ffprobe,
        )
    return (ffmpeg, ffprobe)
