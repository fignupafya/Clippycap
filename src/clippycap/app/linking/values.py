"""Pure value coercion shared by the extract / steps / predicate stages.

Datetimes and durations are normalised to **epoch seconds** / **seconds** (float) so that every
numeric and time predicate compares in one space -- no timezone or DST pitfalls (LINKERS.md §11). A
coercion that cannot succeed raises :class:`CoerceError`; the extractor turns that into a per-field
error surfaced in the preview, never a crash.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any

from clippycap.app.linking.types import FieldType


class CoerceError(ValueError):
    """A value that cannot be coerced to the requested type/shape."""


# Date formats tried, in order, when a datetime field has no explicit format (auto-detect). ISO is
# handled separately by ``datetime.fromisoformat`` first.
_DATE_FORMATS = (
    "%Y.%m.%d - %H.%M.%S",   # NVIDIA ShadowPlay
    "%Y-%m-%d %H-%M-%S",     # OBS default
    "%Y-%m-%d_%H-%M-%S",
    "%Y%m%d_%H%M%S",
    "%Y%m%d-%H%M%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)
_DUR_HMS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")     # [H:]M:S
_DUR_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([hms])", re.IGNORECASE)       # 1h2m3s


def keep_digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def keep_letters(text: str) -> str:
    return "".join(ch for ch in text if ch.isalpha())


def similarity(a: str, b: str) -> float:
    """A 0..1 string closeness (case-folded) -- ``SequenceMatcher`` ratio, pure stdlib."""
    return SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


def to_number(value: Any) -> float:
    """Coerce to ``float``. Accepts numbers and numeric strings (leading zeros / spaces tolerated)."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", ".")
        try:
            return float(text)
        except ValueError as exc:
            raise CoerceError(f"{value!r} is not a number") from exc
    raise CoerceError(f"{value!r} is not a number")


def to_epoch(value: Any, *, date_format: str | None = None, tz: str = "local") -> float:
    """Coerce a datetime to **epoch seconds**. A number is taken as already-epoch; a string is parsed
    with ``date_format`` (else auto-detected) and read in ``tz`` (local | utc) before converting."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        raise CoerceError(f"{value!r} is not a date")
    text = value.strip()
    parsed: datetime | None = None
    if date_format:
        try:
            parsed = datetime.strptime(text, date_format)
        except ValueError as exc:
            raise CoerceError(f"{text!r} does not match date format {date_format!r}") from exc
    else:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            for fmt in _DATE_FORMATS:
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
    if parsed is None:
        raise CoerceError(f"could not read a date from {text!r}")
    if parsed.tzinfo is None and tz == "utc":
        parsed = parsed.replace(tzinfo=UTC)
    # naive + local => .timestamp() uses the machine zone; aware => respected.
    return parsed.timestamp()


def parse_duration(value: Any) -> float:
    """Coerce to **seconds**. Accepts numbers, ``H:M:S`` / ``M:S``, and ``1h2m3s`` forms."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        raise CoerceError(f"{value!r} is not a duration")
    text = value.strip()
    m = _DUR_HMS_RE.match(text)
    if m:
        hours = int(m.group(1)) if m.group(1) else 0
        return hours * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    units = _DUR_UNIT_RE.findall(text)
    if units:
        factor = {"h": 3600.0, "m": 60.0, "s": 1.0}
        return sum(float(num) * factor[unit.lower()] for num, unit in units)
    try:
        return float(text)
    except ValueError as exc:
        raise CoerceError(f"{value!r} is not a duration") from exc


def _to_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


def cast_value(value: Any, field_type: FieldType, *, date_format: str | None = None, tz: str = "local") -> Any:
    """Coerce a raw value to the field's declared type (datetime/duration -> float seconds)."""
    if value is None:
        return None
    casters: dict[FieldType, Any] = {
        "string": lambda v: v if isinstance(v, str) else str(v),
        "int": lambda v: round(to_number(v)),
        "float": to_number,
        "bool": _to_bool,
        "datetime": lambda v: to_epoch(v, date_format=date_format, tz=tz),
        "duration": parse_duration,
    }
    return casters[field_type](value)
