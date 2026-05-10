"""Video thumbnail generation.

:class:`FfmpegThumbnailer` grabs one frame with ffmpeg and scales it. :class:`UnavailableThumbnailer`
does nothing (``available is False``) -- used when ffmpeg is disabled; the frontend then captures a
frame client-side and uploads it instead.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)
_THUMBNAIL_TIMEOUT = 60

# A thumbnail may be stored as any of these (ffmpeg writes one; the client may PUT a JPEG/PNG).
THUMBNAIL_EXTENSIONS: tuple[str, ...] = (".webp", ".jpg", ".png")


def purge_asset_thumbnails(directory: Path, asset_id: int) -> None:
    """Delete every thumbnail file for ``asset_id`` so at most one survives -- called before
    (re)generating one and when an asset is deleted, so stale variants don't accumulate."""
    for ext in THUMBNAIL_EXTENSIONS:
        (directory / f"{asset_id}{ext}").unlink(missing_ok=True)


class FfmpegThumbnailer:
    available = True

    def __init__(self, ffmpeg_path: Path, *, width: int, at_fraction: float, output_format: str) -> None:
        self._ffmpeg = ffmpeg_path
        self._width = width
        self._at_fraction = at_fraction
        self._format = output_format

    def _seek_candidates(self, metadata: Mapping[str, Any]) -> list[float]:
        duration_ms = metadata.get("duration_ms")
        if isinstance(duration_ms, int | float) and duration_ms > 0:
            duration = float(duration_ms) / 1000.0
            # the configured fraction, but clamped so we never seek past the end (a too-short clip
            # would otherwise yield no frame); then frame zero as a last resort.
            return [min(duration * self._at_fraction, max(0.0, duration - 0.2)), 0.0]
        return [1.0, 0.0]   # unknown duration: an early frame, then frame zero

    def make(self, path: Path, out_path: Path, *, metadata: Mapping[str, Any]) -> bool:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        for seek in self._seek_candidates(metadata):
            try:
                completed = subprocess.run(
                    [str(self._ffmpeg), "-y", "-loglevel", "error", "-ss", f"{seek:.3f}", "-i", str(path),
                     "-frames:v", "1", "-an", "-vf", f"scale={self._width}:-1", str(out_path)],
                    capture_output=True, timeout=_THUMBNAIL_TIMEOUT, check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                _log.warning("ffmpeg thumbnail failed on %s: %s", path, exc)
                return False
            if completed.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
                return True
        return False


class UnavailableThumbnailer:
    available = False

    def make(self, path: Path, out_path: Path, *, metadata: Mapping[str, Any]) -> bool:
        _ = (path, out_path, metadata)
        return False
