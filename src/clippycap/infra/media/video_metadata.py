"""Video metadata extraction.

:class:`FfprobeMetadataExtractor` runs ``ffprobe -show_format -show_streams`` and pulls out
``duration_ms``, ``width``, ``height``, ``fps``, ``codec`` and ``recorded_at`` (from the container's
``creation_time`` tag, if any). It reads the ffprobe path from a shared :class:`FfmpegToolsHolder`,
so when no ffprobe is available -- or until the user installs one -- :meth:`extract` returns ``{}``
and the frontend reports what it discovers from the ``<video>`` element instead.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from clippycap.infra.media.ffmpeg import FfmpegToolsHolder

_log = logging.getLogger(__name__)
_PROBE_TIMEOUT = 30


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _parse_fps(rate: object) -> float | None:
    if not isinstance(rate, str) or "/" not in rate:
        return None
    numerator, _, denominator = rate.partition("/")
    try:
        num, den = float(numerator), float(denominator)
    except ValueError:
        return None
    return round(num / den, 3) if den else None


def parse_ffprobe(data: dict[str, Any]) -> dict[str, Any]:
    """Pick the fields we keep out of ffprobe's ``-show_format -show_streams`` JSON."""
    out: dict[str, Any] = {}
    fmt = _as_dict(data.get("format"))
    duration = fmt.get("duration")
    try:
        if duration is not None:
            out["duration_ms"] = round(float(duration) * 1000)
    except (TypeError, ValueError):
        pass
    created = _as_dict(fmt.get("tags")).get("creation_time")
    if isinstance(created, str) and created:
        out["recorded_at"] = created
    video = next(
        (s for s in _as_list(data.get("streams")) if _as_dict(s).get("codec_type") == "video"), None
    )
    if video is not None:
        v = _as_dict(video)
        if (width := _as_int(v.get("width"))) is not None:
            out["width"] = width
        if (height := _as_int(v.get("height"))) is not None:
            out["height"] = height
        if codec := v.get("codec_name"):
            out["codec"] = str(codec)
        if fps := (_parse_fps(v.get("avg_frame_rate")) or _parse_fps(v.get("r_frame_rate"))):
            out["fps"] = fps
    return out


class FfprobeMetadataExtractor:
    def __init__(self, tools: FfmpegToolsHolder) -> None:
        self._tools = tools

    def extract(self, path: Path) -> dict[str, Any]:
        ffprobe = self._tools.current.ffprobe_path
        if ffprobe is None:                                  # no ffprobe (yet) -> the frontend fills in
            return {}
        try:
            completed = subprocess.run(
                [str(ffprobe), "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)],
                capture_output=True, text=True, timeout=_PROBE_TIMEOUT, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _log.warning("ffprobe failed on %s: %s", path, exc)
            return {}
        if completed.returncode != 0 or not completed.stdout:
            return {}
        try:
            data = json.loads(completed.stdout)
        except (ValueError, TypeError):
            return {}
        return parse_ffprobe(data) if isinstance(data, dict) else {}
