"""Select winning links from scored candidates -- the global, *relative* layer (LINKERS.md §6) -- **pure**.

Input: scored :class:`Candidate` edges, the :class:`~clippycap.app.linking.types.ResolveSpec`, and
the manual pin/exclude overrides. Output: the chosen links plus the clip ids flagged "needs you"
(ambiguous). Strategies: ``keep_all`` / ``best_per_clip`` / ``best_per_file`` / ``quota`` are greedy
over a deterministic tiebreak ordering (cascade falls out for free); ``best_overall`` is the optimal
max-total-score assignment under the per-side caps, via min-cost flow that only takes beneficial
augmenting paths. Pins are forced links; excludes are removed; ``stable`` keeps existing links sticky.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from clippycap.app.linking.types import ResolveSpec

_SCORE_SCALE = 1_000_000   # score (0..1 float) -> integer cost magnitude for the flow solver
_MIN_FOR_AMBIGUITY = 2     # need at least two candidates to be "ambiguous"


@dataclass(frozen=True, slots=True)
class Candidate:
    clip_id: int
    file_path: str
    score: float
    delta: float = 0.0           # closeness of the primary time condition (smaller = closer)
    mtime: float = 0.0           # tiebreak: newest/oldest
    size: int = 0                # tiebreak: smallest/largest
    name: str = ""               # tiebreak: name
    group: Any = None            # one_per_group value (None = ungrouped)


@dataclass(slots=True)
class ResolvedLink:
    clip_id: int
    file_path: str
    score: float
    origin: str = "auto"         # auto | manual (a pin)


@dataclass(slots=True)
class ResolveOutput:
    links: list[ResolvedLink] = field(default_factory=list)
    ambiguous_clip_ids: list[int] = field(default_factory=list)


def _sort_key(c: Candidate, spec: ResolveSpec, existing: set[tuple[int, str]]) -> tuple[Any, ...]:
    key: list[Any] = []
    if spec.stable:
        key.append(0 if (c.clip_id, c.file_path) in existing else 1)
    for tb in spec.tiebreak:
        key.append({
            "score": -c.score, "nearest_time": c.delta, "newest": -c.mtime, "oldest": c.mtime,
            "smallest": c.size, "largest": -c.size, "name": c.name.casefold(),
        }.get(tb, -c.score))
    key.extend((-c.score, c.clip_id, c.file_path))   # always score-aware, then fully deterministic
    return tuple(key)


def _apply_relative_floor(cands: list[Candidate], spec: ResolveSpec) -> list[Candidate]:
    """Drop candidates below the absolute floor and, per clip, below (best - relative_threshold)."""
    kept = [c for c in cands if c.score >= spec.absolute_floor]
    if spec.relative_threshold is None:
        return kept
    best: dict[int, float] = {}
    for c in kept:
        best[c.clip_id] = max(best.get(c.clip_id, 0.0), c.score)
    return [c for c in kept if c.score >= best[c.clip_id] - spec.relative_threshold]


def _flag_ambiguous(cands: list[Candidate], spec: ResolveSpec, pinned_clips: set[int]) -> set[int]:
    """A clip whose two best candidates are within ``ambiguity_margin`` (and that the user hasn't
    pinned) is flagged for manual arbitration rather than auto-picked."""
    if spec.ambiguity_margin <= 0:
        return set()
    by_clip: dict[int, list[float]] = {}
    for c in cands:
        by_clip.setdefault(c.clip_id, []).append(c.score)
    flagged: set[int] = set()
    for clip_id, scores in by_clip.items():
        if clip_id in pinned_clips:
            continue
        top = sorted(scores, reverse=True)
        if len(top) >= _MIN_FOR_AMBIGUITY and (top[0] - top[1]) <= spec.ambiguity_margin:
            flagged.add(clip_id)
    return flagged


def _greedy(
    cands: list[Candidate], *, per_clip_max: int | None, per_file_max: int | None, limit: int | None,
    clip_used: dict[int, int], file_used: dict[str, int],
) -> list[Candidate]:
    chosen: list[Candidate] = []
    for c in cands:
        if limit is not None and len(chosen) >= limit:
            break
        if per_clip_max is not None and clip_used.get(c.clip_id, 0) >= per_clip_max:
            continue
        if per_file_max is not None and file_used.get(c.file_path, 0) >= per_file_max:
            continue
        chosen.append(c)
        clip_used[c.clip_id] = clip_used.get(c.clip_id, 0) + 1
        file_used[c.file_path] = file_used.get(c.file_path, 0) + 1
    return chosen


def _one_per_group(cands: list[Candidate]) -> list[Candidate]:
    seen: set[Any] = set()
    out: list[Candidate] = []
    for c in cands:                          # cands already in best-first order
        if c.group is None:
            out.append(c)
            continue
        if c.group in seen:
            continue
        seen.add(c.group)
        out.append(c)
    return out


def resolve(
    candidates: Iterable[Candidate], spec: ResolveSpec, *,
    pins: Iterable[tuple[int, str]] = (), excludes: Iterable[tuple[int, str]] = (),
    existing: Iterable[tuple[int, str]] = (),
) -> ResolveOutput:
    exclude_set = set(excludes)
    pin_set = set(pins)
    existing_set = set(existing)
    cands = [c for c in candidates if (c.clip_id, c.file_path) not in exclude_set
             and (c.clip_id, c.file_path) not in pin_set]
    cands = _apply_relative_floor(cands, spec)

    pinned_clips = {clip for clip, _ in pin_set}
    ambiguous = _flag_ambiguous(cands, spec, pinned_clips)
    cands = [c for c in cands if c.clip_id not in ambiguous]
    cands.sort(key=lambda c: _sort_key(c, spec, existing_set))

    # forced links from pins consume capacity before auto-assignment.
    clip_used: dict[int, int] = {}
    file_used: dict[str, int] = {}
    forced = [ResolvedLink(clip_id=cid, file_path=p, score=1.0, origin="manual") for cid, p in pin_set]
    for link in forced:
        clip_used[link.clip_id] = clip_used.get(link.clip_id, 0) + 1
        file_used[link.file_path] = file_used.get(link.file_path, 0) + 1

    chosen = _select(cands, spec, clip_used=clip_used, file_used=file_used, existing=existing_set)
    if spec.one_per_group is not None:
        chosen = _one_per_group(chosen)

    links = forced + [ResolvedLink(clip_id=c.clip_id, file_path=c.file_path, score=c.score) for c in chosen]
    return ResolveOutput(links=links, ambiguous_clip_ids=sorted(ambiguous))


def _select(
    cands: list[Candidate], spec: ResolveSpec, *,
    clip_used: dict[int, int], file_used: dict[str, int], existing: set[tuple[int, str]],
) -> list[Candidate]:
    strategy = spec.strategy
    if strategy == "best_overall":
        return _best_overall(
            cands, per_clip_max=spec.per_clip_max or 1, per_file_max=spec.per_file_max or 1,
            clip_used=clip_used, file_used=file_used,
        )
    if strategy == "best_per_clip":
        return _greedy(cands, per_clip_max=spec.per_clip_max or 1, per_file_max=spec.per_file_max,
                       limit=None, clip_used=clip_used, file_used=file_used)
    if strategy == "best_per_file":
        return _greedy(cands, per_clip_max=spec.per_clip_max, per_file_max=spec.per_file_max or 1,
                       limit=None, clip_used=clip_used, file_used=file_used)
    if strategy == "quota":
        return _greedy(cands, per_clip_max=spec.per_clip_max, per_file_max=spec.per_file_max,
                       limit=spec.quota, clip_used=clip_used, file_used=file_used)
    # keep_all
    return _greedy(cands, per_clip_max=spec.per_clip_max, per_file_max=spec.per_file_max,
                   limit=None, clip_used=clip_used, file_used=file_used)


# --------------------------------------------------------------------------- best-overall (MCMF)


class _MinCostFlow:
    """Min-cost flow taking only *beneficial* (negative total-cost) augmenting paths, so the result
    maximises total score under the node capacities (an optimal b-matching). SPFA finds each path."""

    def __init__(self, n: int) -> None:
        self._to: list[int] = []
        self._cap: list[int] = []
        self._cost: list[int] = []
        self._graph: list[list[int]] = [[] for _ in range(n)]

    def _push(self, u: int, v: int, cap: int, cost: int) -> None:
        self._graph[u].append(len(self._to))
        self._to.append(v)
        self._cap.append(cap)
        self._cost.append(cost)

    def add_edge(self, u: int, v: int, cap: int, cost: int) -> None:
        self._push(u, v, cap, cost)       # forward
        self._push(v, u, 0, -cost)        # residual

    def run(self, s: int, t: int) -> None:
        n = len(self._graph)
        while True:
            dist = [0] * n
            in_q = [False] * n
            prev_edge = [-1] * n
            reachable = [False] * n
            dist[s] = 0
            reachable[s] = True
            queue = [s]
            in_q[s] = True
            while queue:
                u = queue.pop()
                in_q[u] = False
                for eid in self._graph[u]:
                    if self._cap[eid] <= 0:
                        continue
                    v = self._to[eid]
                    nd = dist[u] + self._cost[eid]
                    if not reachable[v] or nd < dist[v]:
                        dist[v] = nd
                        reachable[v] = True
                        prev_edge[v] = eid
                        if not in_q[v]:
                            queue.append(v)
                            in_q[v] = True
            if not reachable[t] or dist[t] >= 0:    # no path, or no further *beneficial* one
                return
            v = t                                   # augment by 1 along the found path
            while v != s:
                eid = prev_edge[v]
                self._cap[eid] -= 1
                self._cap[eid ^ 1] += 1
                v = self._to[eid ^ 1]

    def next_edge_index(self) -> int:
        return len(self._to)

    def flow_on(self, edge_index: int) -> bool:
        return self._cap[edge_index] == 0   # a cap-1 forward edge with no residual carried flow


def _best_overall(
    cands: list[Candidate], *, per_clip_max: int, per_file_max: int,
    clip_used: dict[int, int], file_used: dict[str, int],
) -> list[Candidate]:
    if not cands:
        return []
    clip_ids = sorted({c.clip_id for c in cands})
    files = sorted({c.file_path for c in cands})
    clip_node = {cid: i + 2 for i, cid in enumerate(clip_ids)}
    file_node = {p: i + 2 + len(clip_ids) for i, p in enumerate(files)}
    n = 2 + len(clip_ids) + len(files)
    s, t = 0, 1
    mcmf = _MinCostFlow(n)
    for cid in clip_ids:
        remaining = per_clip_max - clip_used.get(cid, 0)
        if remaining > 0:
            mcmf.add_edge(s, clip_node[cid], remaining, 0)
    for p in files:
        remaining = per_file_max - file_used.get(p, 0)
        if remaining > 0:
            mcmf.add_edge(file_node[p], t, remaining, 0)
    edge_of: dict[tuple[int, str], int] = {}
    for c in cands:
        edge_of[(c.clip_id, c.file_path)] = mcmf.next_edge_index()
        mcmf.add_edge(clip_node[c.clip_id], file_node[c.file_path], 1, -round(c.score * _SCORE_SCALE))
    mcmf.run(s, t)
    return [c for c in cands if mcmf.flow_on(edge_of[(c.clip_id, c.file_path)])]
