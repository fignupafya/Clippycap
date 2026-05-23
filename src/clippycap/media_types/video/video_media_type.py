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
from datetime import datetime
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


_RECORDED_AT_FORMAT = "%Y-%m-%dT%H:%M:%S"   # canonical: naive local wall-clock, second precision
# Plausible recording-year window: anything outside it is a container "unset creation_time" sentinel
# (QuickTime's 1904 epoch, Unix's 1970) rather than a real capture time.
_RECORDED_YEAR_MIN = 2000
_RECORDED_YEAR_MAX = 2100


def normalize_recorded_at(raw: object) -> str | None:
    """Canonicalise any recording-time string to naive **local** ``YYYY-MM-DDTHH:MM:SS``.

    ``recorded_at`` reaches the app in three shapes -- a naive local time parsed from an OBS-style
    file name, the container's ``creation_time`` tag (UTC), or the file's mtime -- and the library
    sorts on it as a plain string, so all three MUST share one exact format or the ordering breaks.
    Anything timezone-aware is converted to the machine's local wall-clock; sub-second precision and
    the offset are dropped. Returns ``None`` for an empty / unparseable / obviously-bogus value (the
    1904 QuickTime and 1970 Unix "unset" sentinels are rejected so they don't poison the sort)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text)        # handles 'Z', offsets, fractional, ' '/'T' sep
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y%m%d %H%M%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is not None:                    # UTC / offset -> the machine's local wall clock
        try:
            parsed = parsed.astimezone().replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            return None                              # pre-epoch sentinel the OS can't convert -> reject
    if not (_RECORDED_YEAR_MIN <= parsed.year <= _RECORDED_YEAR_MAX):
        return None                                  # a 1904 / 1970 "unset creation_time" sentinel
    return parsed.strftime(_RECORDED_AT_FORMAT)


def _recorded_at_from_name(name: str, patterns: Sequence[re.Pattern[str]]) -> str | None:
    for pattern in patterns:
        match = pattern.search(name)
        if match is None:
            continue
        g = match.groupdict()
        try:
            return datetime(
                int(g["Y"]), int(g["m"]), int(g["d"]), int(g["H"]), int(g["M"]), int(g["S"])
            ).strftime(_RECORDED_AT_FORMAT)
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

    @property
    def metadata_extraction_available(self) -> bool:
        """Whether :meth:`extract_metadata` can read the full set right now -- i.e. ffprobe is
        located. When ``False`` the metadata enrichment pass leaves clips pending until it is."""
        return self._extractor.available

    def _recorded_at(self, path: Path, container_value: object) -> str | None:
        """The recording time as the canonical naive-local string: prefer the recording software's
        local wall-clock from the file name, then the container's creation_time tag (UTC), then the
        file's mtime -- every source funnelled through one format so the library sort compares like
        with like. ``container_value`` is ``None`` when the container has not been probed."""
        recorded = _recorded_at_from_name(path.name, self._patterns)
        if recorded is None:
            recorded = normalize_recorded_at(container_value)
        if recorded is None:
            with contextlib.suppress(OSError):
                recorded = datetime.fromtimestamp(path.stat().st_mtime).strftime(_RECORDED_AT_FORMAT)
        return recorded

    def quick_metadata(self, path: Path) -> dict[str, Any]:
        """Metadata obtainable without spawning ffprobe: just ``recorded_at`` (from the file name,
        falling back to the mtime). A scan's discovery phase uses this so the clips appear -- and
        the library sorts correctly -- at once; :meth:`extract_metadata` fills in the rest later."""
        recorded = self._recorded_at(path, None)
        return {"recorded_at": recorded} if recorded is not None else {}

    def extract_metadata(self, path: Path) -> dict[str, Any]:
        meta: dict[str, Any] = dict(self._extractor.extract(path))
        recorded = self._recorded_at(path, meta.get("recorded_at"))     # meta's value = ffprobe creation_time
        if recorded is not None:
            meta["recorded_at"] = recorded
        else:
            meta.pop("recorded_at", None)        # drop an unsalvageable container value, don't sort on junk
        return meta

    def make_thumbnail(self, path: Path, out_path: Path, *, metadata: Mapping[str, Any]) -> bool:
        return self._thumbnailer.make(path, out_path, metadata=metadata)

    def display_title(self, path: Path, metadata: Mapping[str, Any]) -> str:
        # The title is the file's own name -- what the user named it. The recording timestamp is
        # kept in metadata (`recorded_at`) and shown separately by the UI, not used as the title.
        del metadata
        return path.stem
