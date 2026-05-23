"""Locating ffmpeg / ffprobe -- and a tiny mutable holder so the located paths can change at runtime.

The configured path (``[media.ffmpeg].ffmpeg_path`` / ``ffprobe_path``) is one of:

* ``"auto"``      -- probe, in order: ``<data_dir>/bin`` (where an on-demand install / the installer
  put a downloaded build), the bundle dir's ``bin`` (``@bundled``), the exe's ``bin`` when frozen,
  common OS install locations, then ``PATH``;
* ``"@bundled"``  -- only the binary shipped next to the app (``<install_dir>/bin``);
* an absolute path -- exactly that file.

A candidate is accepted only if ``<exe> -version`` succeeds. :func:`resolve_ffmpeg_tools` returns a
:class:`FfmpegTools` with ``None`` paths when ffmpeg is disabled in the config or cannot be found.

Long-lived services (the thumbnailer, the metadata extractor, the video editor) keep a
:class:`FfmpegToolsHolder` rather than a bare path, so that after the user installs ffmpeg (or points
the app at an existing build) the next call uses it -- no restart.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from clippycap.infra.config import Config

_log = logging.getLogger(__name__)
_EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
_VERSION_PROBE_TIMEOUT = 15

# A windowed build (PyInstaller --windowed) has no console; without this, Windows allocates a fresh
# console *window* for every console child (ffmpeg / ffprobe) and it flashes on screen. Pass it as
# `creationflags=` to every ffmpeg/ffprobe subprocess call. 0 (a harmless no-op) off Windows.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass(frozen=True, slots=True)
class FfmpegTools:
    """The currently resolved ffmpeg/ffprobe locations (``None`` => not available)."""

    ffmpeg_path: Path | None
    ffprobe_path: Path | None

    @property
    def ffmpeg_available(self) -> bool:
        return self.ffmpeg_path is not None

    @property
    def ffprobe_available(self) -> bool:
        return self.ffprobe_path is not None


class FfmpegToolsHolder:
    """Mutable reference to the active :class:`FfmpegTools` (swapped by the ffmpeg service)."""

    __slots__ = ("current",)

    def __init__(self, tools: FfmpegTools) -> None:
        self.current: FfmpegTools = tools


# --------------------------------------------------------------------------- probing


def probe_ffmpeg_version(exe: Path) -> str | None:
    """The first line of ``<exe> -version`` (e.g. ``ffmpeg version n7.1 ...``), or ``None`` if the
    file does not exist or cannot be executed successfully."""
    if not exe.is_file():
        return None
    try:
        completed = subprocess.run(
            [str(exe), "-version"], capture_output=True, text=True,
            timeout=_VERSION_PROBE_TIMEOUT, check=False, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    lines = (completed.stdout or "").strip().splitlines()
    return lines[0].strip() if lines and lines[0].strip() else "(version unknown)"


def _is_runnable(exe: Path) -> bool:
    return probe_ffmpeg_version(exe) is not None


# --------------------------------------------------------------------------- candidate directories


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


def _candidate_dirs(config: Config, install_dir: Path) -> list[Path]:
    dirs: list[Path] = [
        Path(config.app.data_dir) / "bin",          # on-demand-installed / installer-placed build (wins)
        install_dir / "bin",                        # the @bundled location (= _MEIPASS/bin when frozen)
    ]
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent / "bin")   # next to a one-folder build's exe
    dirs.extend(_windows_dirs() if sys.platform == "win32" else _posix_dirs())
    # de-duplicate while preserving order (the lists above can overlap, e.g. data_dir == an OS dir)
    seen: set[Path] = set()
    unique: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def _resolve(name: str, configured: str, *, install_dir: Path, candidate_dirs: list[Path]) -> Path | None:
    if configured == "@bundled":
        candidate = install_dir / "bin" / f"{name}{_EXE_SUFFIX}"
        return candidate if _is_runnable(candidate) else None
    if configured != "auto":
        candidate = Path(configured)
        return candidate if _is_runnable(candidate) else None
    for directory in candidate_dirs:
        candidate = directory / f"{name}{_EXE_SUFFIX}"
        if _is_runnable(candidate):
            return candidate
    on_path = shutil.which(name)
    return Path(on_path) if on_path and _is_runnable(Path(on_path)) else None


def resolve_ffmpeg_tools(config: Config, install_dir: Path) -> FfmpegTools:
    """The currently locatable ``(ffmpeg, ffprobe)`` -- :class:`FfmpegTools` with ``None`` paths if
    ffmpeg is disabled in the config (``[media.ffmpeg].enabled = false``) or cannot be found."""
    ffmpeg_cfg = config.media.ffmpeg
    if not ffmpeg_cfg.enabled:
        return FfmpegTools(None, None)
    candidate_dirs = _candidate_dirs(config, install_dir)
    ffmpeg = _resolve("ffmpeg", ffmpeg_cfg.ffmpeg_path, install_dir=install_dir, candidate_dirs=candidate_dirs)
    ffprobe = _resolve("ffprobe", ffmpeg_cfg.ffprobe_path, install_dir=install_dir, candidate_dirs=candidate_dirs)
    if ffmpeg is None or ffprobe is None:
        _log.warning(
            "ffmpeg is enabled but was not fully located (ffmpeg=%s, ffprobe=%s); thumbnails and "
            "video metadata fall back to the browser. Install ffmpeg from the app's Settings, drop it "
            "in %s, or put it on PATH.",
            ffmpeg, ffprobe, Path(config.app.data_dir) / "bin",
        )
    return FfmpegTools(ffmpeg, ffprobe)
