"""The built-in ``video`` media type.

Recognises common video extensions; parses ``recorded_at`` from OBS-style file names (falling back
to the container's ``creation_time`` tag, then the file's mtime); delegates metadata extraction and
thumbnailing to the injected (ffmpeg-backed or no-op) collaborators; hints the frontend to use the
analysis video player. It is wired into the media-type registry exactly like a third-party plugin.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clippycap.core.ports import MetadataExtractor, Thumbnailer

_FILENAME_TOKENS: dict[str, str] = {
    "%Y": r"(?P<Y>\d{4})", "%m": r"(?P<m>\d{2})", "%d": r"(?P<d>\d{2})",
    "%H": r"(?P<H>\d{2})", "%M": r"(?P<M>\d{2})", "%S": r"(?P<S>\d{2})",
}


def _compile_filename_patterns(patterns: Sequence[str]) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        regex = re.escape(pattern)
        for token, group in _FILENAME_TOKENS.items():
            regex = regex.replace(token, group)
        try:
            compiled.append(re.compile(regex))
        except re.error:
            continue
    return compiled


def _recorded_at_from_name(name: str, patterns: Sequence[re.Pattern[str]]) -> str | None:
    for pattern in patterns:
        match = pattern.search(name)
        if match is None:
            continue
        g = match.groupdict()
        try:
            return datetime(
                int(g["Y"]), int(g["m"]), int(g["d"]), int(g["H"]), int(g["M"]), int(g["S"])
            ).isoformat()
        except (KeyError, ValueError):
            continue
    return None


class VideoMediaType:
    media_type = "video"
    player_kind = "video"

    def __init__(
        self,
        *,
        extensions: Sequence[str],
        recorded_at_patterns: Sequence[str],
        identity_strategy_name: str,
        metadata_extractor: MetadataExtractor,
        thumbnailer: Thumbnailer,
    ) -> None:
        self.extensions = frozenset(e.lower().lstrip(".") for e in extensions)
        self.identity_strategy_name = identity_strategy_name
        self._extractor = metadata_extractor
        self._thumbnailer = thumbnailer
        self._patterns = _compile_filename_patterns(recorded_at_patterns)

    def detect(self, path: Path) -> bool:
        return path.suffix.lower().lstrip(".") in self.extensions

    def extract_metadata(self, path: Path) -> dict[str, Any]:
        meta: dict[str, Any] = dict(self._extractor.extract(path))
        # the recording software writes the local wall-clock time into the file name; prefer that
        # over the container's creation_time (UTC, sometimes wrong or absent), then the file mtime.
        from_name = _recorded_at_from_name(path.name, self._patterns)
        if from_name is not None:
            meta["recorded_at"] = from_name
        elif not meta.get("recorded_at"):
            with contextlib.suppress(OSError):
                meta["recorded_at"] = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
        return meta

    def make_thumbnail(self, path: Path, out_path: Path, *, metadata: Mapping[str, Any]) -> bool:
        return self._thumbnailer.make(path, out_path, metadata=metadata)

    def display_title(self, path: Path, metadata: Mapping[str, Any]) -> str:
        recorded = metadata.get("recorded_at")
        if isinstance(recorded, str):
            try:
                return datetime.fromisoformat(recorded).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        return path.stem
