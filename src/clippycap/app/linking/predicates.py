"""Score a single (clip, file) pair against the match conditions -- **pure** (LINKERS.md §5).

Each condition yields *passed?*, a 0..1 sub-score (closeness within a tolerance, fuzzy ratio, or 1.0
for a binary pass) and a *delta* (how close, for the "nearest" tiebreak). ``combine`` folds them:
``all`` => every condition passes, score = weighted mean; ``any`` => at least one, score = best.
A pair scoring below ``min_score`` (or failing ``combine``) is not a candidate.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from clippycap.app.linking.types import Condition, MatchSpec, Ref
from clippycap.app.linking.values import CoerceError, similarity, to_number

_EPS = 1e-9


@dataclass(slots=True)
class CandidateScore:
    score: float
    delta: float                 # closeness of the primary time/interval condition (smaller = closer)
    reasons: list[str]


@dataclass(slots=True)
class _Eval:
    passed: bool
    sub: float
    delta: float | None
    reason: str


def _resolve(ref: Ref | None, clip: Mapping[str, Any], file: Mapping[str, Any]) -> Any:
    if ref is None:
        return None
    return (clip if ref.side == "clip" else file).get(ref.field)


def _num(value: Any) -> float | None:
    try:
        return to_number(value)
    except CoerceError:
        return None


def _norm(value: Any, *, ci: bool) -> str:
    text = "" if value is None else str(value)
    return text.casefold() if ci else text


def _eq(left: Any, right: Any, *, ci: bool) -> bool:
    if isinstance(left, int) and isinstance(right, int) and not isinstance(left, bool) and not isinstance(right, bool):
        return left == right                          # exact -- no float precision loss for big keys
    ln, rn = _num(left), _num(right)
    if ln is not None and rn is not None:
        return abs(ln - rn) < _EPS
    return _norm(left, ci=ci) == _norm(right, ci=ci)


def _evaluate(cond: Condition, clip: Mapping[str, Any], file: Mapping[str, Any]) -> _Eval:  # noqa: PLR0911
    op = cond.op
    left = _resolve(cond.left, clip, file)
    right = _resolve(cond.right, clip, file)
    lf, rf = cond.left.field, cond.right.field if cond.right else "?"

    if op == "equals":
        ok = _eq(left, right, ci=cond.case_insensitive)
        return _Eval(ok, 1.0 if ok else 0.0, None, f"{lf} {'=' if ok else '≠'} {rf}")
    if op == "not_equals":
        ok = not _eq(left, right, ci=cond.case_insensitive)
        return _Eval(ok, 1.0 if ok else 0.0, None, f"{lf} {'≠' if ok else '='} {rf}")
    if op == "within":
        ln, rn = _num(left), _num(right)
        if ln is None or rn is None:
            return _Eval(False, 0.0, None, f"{lf}/{rf} not both numbers")
        d = abs((rn - ln) - cond.offset)
        tol = cond.tolerance if cond.tolerance is not None else 0.0
        ok = d <= tol
        sub = max(0.0, 1.0 - d / tol) if tol > 0 else (1.0 if d == 0 else 0.0)
        return _Eval(ok, sub, d, f"{lf} within {tol:g} of {rf} (off by {d:g})")
    if op in {"interval_contains", "interval_overlap"}:
        return _interval(op, cond, left, right, clip, file)
    if op in {"lt", "lte", "gt", "gte"}:
        return _ordered(op, ln=_num(left), rn=_num(right), lf=lf, rf=rf)
    if op in {"contains", "startswith", "endswith", "regex"}:
        return _string(op, _norm(left, ci=cond.case_insensitive), _norm(right, ci=cond.case_insensitive), lf, rf)
    if op == "fuzzy":
        s = similarity(str(left or ""), str(right or ""))
        ok = s >= cond.threshold
        return _Eval(ok, s, 1.0 - s, f"{lf} ≈ {rf} ({s:.0%})")
    return _Eval(False, 0.0, None, f"unknown op {op}")        # pragma: no cover


def _interval(
    op: str, cond: Condition, left: Any, right: Any, clip: Mapping[str, Any], file: Mapping[str, Any]
) -> _Eval:
    start, end = _num(_resolve(cond.start, clip, file)), _num(_resolve(cond.end, clip, file))
    if start is None or end is None:
        return _Eval(False, 0.0, None, "interval bounds missing")
    lo, hi = min(start, end) - cond.slack, max(start, end) + cond.slack
    if op == "interval_contains":
        point = _num(left)
        if point is None:
            return _Eval(False, 0.0, None, f"{cond.left.field} not a number")
        ok = lo <= point <= hi
        delta = abs(point - start)
        return _Eval(ok, 1.0 if ok else 0.0, delta, f"{cond.left.field} {'in' if ok else 'outside'} interval")
    rn = _num(right)
    a_lo, a_hi = _num(left), rn
    if a_lo is None or a_hi is None:
        return _Eval(False, 0.0, None, "interval operand missing")
    ok = max(a_lo, lo) <= min(a_hi, hi)
    return _Eval(ok, 1.0 if ok else 0.0, None, f"intervals {'overlap' if ok else 'disjoint'}")


def _ordered(op: str, *, ln: float | None, rn: float | None, lf: str, rf: str) -> _Eval:
    if ln is None or rn is None:
        return _Eval(False, 0.0, None, f"{lf}/{rf} not both numbers")
    ok = {"lt": ln < rn, "lte": ln <= rn, "gt": ln > rn, "gte": ln >= rn}[op]
    sym = {"lt": "<", "lte": "≤", "gt": ">", "gte": "≥"}[op]
    return _Eval(ok, 1.0 if ok else 0.0, None, f"{lf} {sym} {rf}")


def _string(op: str, left: str, right: str, lf: str, rf: str) -> _Eval:
    if op == "regex":
        try:
            ok = re.search(right, left) is not None
        except re.error:
            return _Eval(False, 0.0, None, f"bad pattern {rf}")
    else:
        ok = {"contains": right in left, "startswith": left.startswith(right),
              "endswith": left.endswith(right)}[op]
    return _Eval(ok, 1.0 if ok else 0.0, None, f"{lf} {op} {rf}")


def evaluate_match(match: MatchSpec, clip: Mapping[str, Any], file: Mapping[str, Any]) -> CandidateScore | None:
    if not match.conditions:
        return None
    evals = [(c, _evaluate(c, clip, file)) for c in match.conditions]
    passed = [(c, e) for c, e in evals if e.passed]
    if match.combine == "all":
        if len(passed) != len(evals):
            return None
        total_w = sum(c.weight for c, _ in evals) or 1.0
        score = sum(c.weight * e.sub for c, e in evals) / total_w
    else:  # any
        if not passed:
            return None
        score = max(e.sub for _, e in passed)
    if score < match.min_score:
        return None
    deltas = [e.delta for _, e in (passed or evals) if e.delta is not None]
    delta = min(deltas) if deltas else 0.0
    reasons = [e.reason for _, e in (passed if match.combine == "any" else evals)]
    return CandidateScore(score=score, delta=delta, reasons=reasons)
