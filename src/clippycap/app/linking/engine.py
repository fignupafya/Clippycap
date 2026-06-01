"""Orchestrate extract -> join -> resolve over a set of clips and files -- **pure**.

Given the items (each already turned into an :class:`ExtractContext` by the caller's I/O), this scores
every plausible pair and resolves the winners, then reports the four preview buckets (matched /
unmatched / needs-you / unused) plus per-item parse errors and the "why" for each link. The runner
uses the same result to write attachments; the preview uses it to show the user what the rule does.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from clippycap.app.linking.extract import ExtractContext, extract_side
from clippycap.app.linking.predicates import evaluate_match
from clippycap.app.linking.resolve import Candidate, ResolvedLink, resolve
from clippycap.app.linking.types import LinkerDefinition, Ref


@dataclass(slots=True)
class ClipItem:
    asset_id: int
    ctx: ExtractContext


@dataclass(slots=True)
class FileItem:
    path: str
    ctx: ExtractContext


@dataclass(slots=True)
class LinkResult:
    clip_id: int
    file_path: str
    score: float
    origin: str
    reasons: list[str]


@dataclass(slots=True)
class AmbiguousChoice:
    file_path: str
    score: float
    reasons: list[str]


@dataclass(slots=True)
class EngineResult:
    links: list[LinkResult] = field(default_factory=list)
    ambiguous: dict[int, list[AmbiguousChoice]] = field(default_factory=dict)
    unmatched_clip_ids: list[int] = field(default_factory=list)
    unused_files: list[str] = field(default_factory=list)
    clip_errors: dict[int, dict[str, str]] = field(default_factory=dict)
    file_errors: dict[str, dict[str, str]] = field(default_factory=dict)
    candidate_count: int = 0


def _group_value(ref: Ref, clip_vals: dict[str, Any], file_vals: dict[str, Any]) -> Any:
    return (clip_vals if ref.side == "clip" else file_vals).get(ref.field)


def run_match(
    defn: LinkerDefinition, clips: Iterable[ClipItem], files: Iterable[FileItem], *,
    pins: Iterable[tuple[int, str]] = (), excludes: Iterable[tuple[int, str]] = (),
    existing: Iterable[tuple[int, str]] = (),
) -> EngineResult:
    clip_list, file_list = list(clips), list(files)
    result = EngineResult()

    clip_recs: dict[int, dict[str, Any]] = {}
    for item in clip_list:
        rec = extract_side(defn.clip, item.ctx)
        clip_recs[item.asset_id] = rec.values
        if rec.errors:
            result.clip_errors[item.asset_id] = rec.errors
    file_recs: dict[str, dict[str, Any]] = {}
    file_ctx: dict[str, ExtractContext] = {}
    for fitem in file_list:
        rec = extract_side(defn.file, fitem.ctx)
        file_recs[fitem.path] = rec.values
        file_ctx[fitem.path] = fitem.ctx
        if rec.errors:
            result.file_errors[fitem.path] = rec.errors

    candidates: list[Candidate] = []
    scores: dict[tuple[int, str], list[str]] = {}
    for clip in clip_list:
        cvals = clip_recs[clip.asset_id]
        for fitem in file_list:
            fvals = file_recs[fitem.path]
            cand_score = evaluate_match(defn.match, cvals, fvals)
            if cand_score is None:
                continue
            ctx = file_ctx[fitem.path]
            group = _group_value(defn.resolve.one_per_group, cvals, fvals) if defn.resolve.one_per_group else None
            candidates.append(Candidate(
                clip_id=clip.asset_id, file_path=fitem.path, score=cand_score.score, delta=cand_score.delta,
                mtime=ctx.mtime_epoch or 0.0, size=ctx.size or 0, name=ctx.name, group=group,
            ))
            scores[(clip.asset_id, fitem.path)] = cand_score.reasons
    result.candidate_count = len(candidates)

    out = resolve(candidates, defn.resolve, pins=pins, excludes=excludes, existing=existing)
    result.links = [
        LinkResult(clip_id=link.clip_id, file_path=link.file_path, score=link.score, origin=link.origin,
                   reasons=scores.get((link.clip_id, link.file_path), []))
        for link in out.links
    ]

    _fill_buckets(result, clip_list, file_list, candidates, out.links, set(out.ambiguous_clip_ids))
    return result


def _fill_buckets(
    result: EngineResult, clip_list: list[ClipItem], file_list: list[FileItem],
    candidates: list[Candidate], links: list[ResolvedLink], ambiguous_ids: set[int],
) -> None:
    linked_clips = {link.clip_id for link in links}
    linked_files = {link.file_path for link in links}
    result.unmatched_clip_ids = [
        c.asset_id for c in clip_list if c.asset_id not in linked_clips and c.asset_id not in ambiguous_ids
    ]
    result.unused_files = [f.path for f in file_list if f.path not in linked_files]

    by_clip: dict[int, list[Candidate]] = {}
    for cand in candidates:
        by_clip.setdefault(cand.clip_id, []).append(cand)
    for clip_id in sorted(ambiguous_ids):
        top = sorted(by_clip.get(clip_id, []), key=lambda c: c.score, reverse=True)[:5]
        result.ambiguous[clip_id] = [AmbiguousChoice(file_path=c.file_path, score=c.score, reasons=[]) for c in top]
