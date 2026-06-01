"""The transform-step evaluator -- the no-code pipeline behind every computed field (LINKERS.md §9.3).

Each :class:`~clippycap.app.linking.types.Step` takes the field's current value (and optionally
another field's value, by name) and returns a new value. A dispatch table keeps every op a small,
independently-testable function. A step that cannot run raises :class:`StepError`, which the
extractor turns into a per-field error in the preview.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from clippycap.app.linking.types import Step
from clippycap.app.linking.values import CoerceError, keep_digits, keep_letters, parse_duration, to_epoch, to_number

_UNIT_SECONDS = {"second": 1.0, "minute": 60.0, "hour": 3600.0, "day": 86400.0}


class StepError(ValueError):
    """A transform step that cannot be applied to the current value."""


def _operand(step: Step, fields: Mapping[str, Any]) -> Any:
    """The step's second operand: another field's value (``field``) or a literal (``value``)."""
    if step.field is not None:
        return fields.get(step.field)
    return step.value


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _h_replace(value: Any, step: Step, _f: Mapping[str, Any]) -> str:
    return _text(value).replace(_text(step.value), _text(step.sep))


def _h_split_take(value: Any, step: Step, _f: Mapping[str, Any]) -> str:
    parts = _text(value).split(step.sep) if step.sep else _text(value).split()
    idx = step.index if step.index is not None else 0
    try:
        return parts[idx]
    except IndexError as exc:
        raise StepError(f"no part #{idx} when splitting {value!r} by {step.sep!r}") from exc


def _h_substr(value: Any, step: Step, _f: Mapping[str, Any]) -> str:
    return _text(value)[step.index : step.end]


def _h_regex_extract(value: Any, step: Step, _f: Mapping[str, Any]) -> str:
    if not step.fmt:
        raise StepError("regex_extract needs a pattern")
    try:
        m = re.search(step.fmt, _text(value))
    except re.error as exc:
        raise StepError(f"bad regex {step.fmt!r}: {exc}") from exc
    if m is None:
        raise StepError(f"pattern {step.fmt!r} did not match {value!r}")
    group = step.group if step.group is not None else (1 if m.re.groups else 0)
    return m.group(group)


def _h_concat(value: Any, step: Step, fields: Mapping[str, Any]) -> str:
    return _text(value) + _text(step.sep) + _text(_operand(step, fields))


def _num(value: Any) -> float:
    try:
        return to_number(value)
    except CoerceError as exc:
        raise StepError(str(exc)) from exc


def _h_add(value: Any, step: Step, fields: Mapping[str, Any]) -> float:
    return _num(value) + _num(_operand(step, fields))


def _h_sub(value: Any, step: Step, fields: Mapping[str, Any]) -> float:
    return _num(value) - _num(_operand(step, fields))


def _h_mul(value: Any, step: Step, fields: Mapping[str, Any]) -> float:
    return _num(value) * _num(_operand(step, fields))


def _h_div(value: Any, step: Step, fields: Mapping[str, Any]) -> float:
    divisor = _num(_operand(step, fields))
    if divisor == 0:
        raise StepError("division by zero")
    return _num(value) / divisor


def _h_round(value: Any, step: Step, _f: Mapping[str, Any]) -> float:
    return round(_num(value), step.index if step.index is not None else 0)


def _h_parse_date(value: Any, step: Step, _f: Mapping[str, Any]) -> float:
    try:
        return to_epoch(value, date_format=step.fmt)
    except CoerceError as exc:
        raise StepError(str(exc)) from exc


def _h_date_add(value: Any, step: Step, fields: Mapping[str, Any]) -> float:
    factor = _UNIT_SECONDS.get(step.unit or "second", 1.0)
    return _num(value) + _num(_operand(step, fields)) * factor


def _h_date_round(value: Any, step: Step, _f: Mapping[str, Any]) -> float:
    factor = _UNIT_SECONDS.get(step.unit or "minute", 60.0)
    return round(_num(value) / factor) * factor


def _h_date_part(value: Any, step: Step, _f: Mapping[str, Any]) -> float:
    epoch = _num(value)
    local_midnight = epoch - (epoch % 86400)        # epoch math; good enough for grouping/compare
    return local_midnight if (step.unit or "date") == "date" else epoch - local_midnight


def _h_parse_duration(value: Any, step: Step, _f: Mapping[str, Any]) -> float:
    try:
        return parse_duration(value)
    except CoerceError as exc:
        raise StepError(str(exc)) from exc


_HANDLERS: dict[str, Callable[[Any, Step, Mapping[str, Any]], Any]] = {
    "trim": lambda v, s, f: _text(v).strip(),
    "lower": lambda v, s, f: _text(v).lower(),
    "upper": lambda v, s, f: _text(v).upper(),
    "keep_digits": lambda v, s, f: keep_digits(_text(v)),
    "keep_letters": lambda v, s, f: keep_letters(_text(v)),
    "replace": _h_replace,
    "split_take": _h_split_take,
    "substr": _h_substr,
    "regex_extract": _h_regex_extract,
    "concat": _h_concat,
    "to_number": lambda v, s, f: _num(v),
    "add": _h_add,
    "sub": _h_sub,
    "mul": _h_mul,
    "div": _h_div,
    "round": _h_round,
    "abs": lambda v, s, f: abs(_num(v)),
    "parse_date": _h_parse_date,
    "date_add": _h_date_add,
    "date_round": _h_date_round,
    "date_part": _h_date_part,
    "to_epoch": _h_parse_date,
    "parse_duration": _h_parse_duration,
}


def apply_steps(value: Any, steps: list[Step], fields: Mapping[str, Any]) -> Any:
    """Run ``value`` through ``steps`` in order. ``fields`` provides earlier fields' values for steps
    that operate against another field. A ``None`` value short-circuits text/number ops to ``None``
    only where they'd otherwise raise; each handler decides. Raises :class:`StepError` on failure."""
    for step in steps:
        handler = _HANDLERS.get(step.op)
        if handler is None:                                     # pragma: no cover -- guarded by schema
            raise StepError(f"unknown step {step.op!r}")
        value = handler(value, step, fields)
    return value
