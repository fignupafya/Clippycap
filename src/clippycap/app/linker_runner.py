"""The :class:`LinkerRunner` -- the filesystem bridge that turns a linker rule into stored links.

It gathers the in-scope assets and walks the target directories (the only I/O), builds an
:class:`ExtractContext` for each, runs the **pure** engine, then syncs the result into
``asset_attachments``: upsert the winners, prune this linker's auto rows that no longer match (never
manual pins -- those are honoured forever), and flag a vanished file ``missing`` rather than deleting
it. Designed to run inside the background :class:`~clippycap.app.jobs.ThreadJobQueue` (LINKERS.md §10).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clippycap.app.linking.engine import ClipItem, EngineResult, FileItem, run_match
from clippycap.app.linking.extract import ExtractContext
from clippycap.app.linking.types import LinkerDefinition, MetadataSource, load_definition
from clippycap.core.entities import Attachment
from clippycap.core.ports import Database
from clippycap.core.query import AssetFilter
from clippycap.infra.scan.walker import walk_files

_log = logging.getLogger(__name__)
_GATHER_LIMIT = 1_000_000


@dataclass(slots=True)
class RunSummary:
    linker_id: int
    clips: int = 0
    files: int = 0
    written: int = 0
    removed: int = 0
    missing: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    unused: int = 0
    waiting_on_metadata: int = 0
    unavailable: bool = False     # a configured target directory was missing -> links left untouched


@dataclass(slots=True)
class _ClipRaw:
    asset_id: int
    metadata: dict[str, Any]
    path: str | None
    pending: bool


def _created_epoch(st: os.stat_result) -> float:
    birth = getattr(st, "st_birthtime", None)
    return float(birth) if birth is not None else float(st.st_ctime)


def _context_for_path(path: Path, folder_index: int | None, metadata: dict[str, Any]) -> ExtractContext:
    ctx = ExtractContext(
        name=path.name, stem=path.stem, ext=path.suffix.lower().lstrip("."),
        path=str(path), folder=path.parent.name, folder_index=folder_index, metadata=metadata,
    )
    try:
        st = path.stat()
        ctx.mtime_epoch = float(st.st_mtime)
        ctx.created_epoch = _created_epoch(st)
        ctx.size = int(st.st_size)
    except OSError:
        pass
    return ctx


def _scope_filter(defn: LinkerDefinition) -> AssetFilter:
    s = defn.source
    return AssetFilter(
        media_type=s.media_type, path_under=s.path_under,
        tags_all=list(s.tags_all), tags_any=list(s.tags_any), in_categories=list(s.in_categories),
    )


def _duration_ok(metadata: dict[str, Any], defn: LinkerDefinition) -> bool:
    lo, hi = defn.source.min_duration_ms, defn.source.max_duration_ms
    if lo is None and hi is None:
        return True
    dur = metadata.get("duration_ms")
    if not isinstance(dur, int | float):
        return False
    return (lo is None or dur >= lo) and (hi is None or dur <= hi)


def _clip_needs_metadata(defn: LinkerDefinition) -> bool:
    return any(isinstance(f.source, MetadataSource) for f in defn.clip.fields)


def _build_clip_items(defn: LinkerDefinition, clips_raw: list[_ClipRaw]) -> tuple[list[ClipItem], int]:
    """Turn raw clip rows into engine items, skipping clips still awaiting the metadata a condition
    needs (they re-run after enrichment). Returns the items and the waiting-on-metadata count."""
    needs_meta = _clip_needs_metadata(defn)
    items: list[ClipItem] = []
    waiting = 0
    for raw in clips_raw:
        if not _duration_ok(raw.metadata, defn):
            continue
        if raw.pending and needs_meta:
            waiting += 1
            continue
        ctx = (_context_for_path(Path(raw.path), None, raw.metadata) if raw.path
               else ExtractContext(metadata=raw.metadata))
        items.append(ClipItem(asset_id=raw.asset_id, ctx=ctx))
    return items, waiting


class LinkerRunner:
    def __init__(self, database: Database) -> None:
        self._db = database

    def run_linker(self, linker_id: int) -> RunSummary:
        defn, clips_raw, pins, excludes, existing = self._gather_inputs(linker_id)
        if defn is None:
            return RunSummary(linker_id=linker_id)

        summary = RunSummary(linker_id=linker_id)
        clip_items, summary.waiting_on_metadata = _build_clip_items(defn, clips_raw)
        file_items, available = self._gather_files(defn)
        summary.unavailable = not available
        summary.clips, summary.files = len(clip_items), len(file_items)

        result = run_match(defn, clip_items, file_items, pins=pins, excludes=excludes, existing=existing)
        # If a configured target folder is missing (unmounted drive, deleted folder), DON'T prune --
        # the files only "vanished" transiently; wiping the user's links would be data loss. We still
        # upsert any links found in the folders that ARE present.
        self._sync(linker_id, result, summary, prune=available)
        summary.ambiguous = len(result.ambiguous)
        summary.unmatched = len(result.unmatched_clip_ids)
        summary.unused = len(result.unused_files)
        _log.info("linker %d: %d clips x %d files -> %d links", linker_id, summary.clips, summary.files,
                  summary.written)
        return summary

    def preview(self, definition_json: str, *, limit_clips: int = 300, limit_files: int = 1000) -> EngineResult:
        """Run the rule against the (capped) library WITHOUT writing anything -- the test harness."""
        defn = load_definition(definition_json)
        with self._db.transaction() as uow:
            assets, _ = uow.assets.search(
                filter=_scope_filter(defn), sort_key="added_desc", offset=0, limit=limit_clips
            )
            clips_raw: list[_ClipRaw] = []
            for asset in assets:
                if asset.id is None:
                    continue
                present = [p for p in uow.assets.get_paths(asset.id) if p.present]
                clips_raw.append(_ClipRaw(
                    asset.id, dict(asset.metadata), present[0].path if present else None, asset.metadata_pending
                ))
        clip_items, _ = _build_clip_items(defn, clips_raw)
        file_items, _ = self._gather_files(defn)
        return run_match(defn, clip_items, file_items[:limit_files])

    def run_all_enabled(self) -> list[RunSummary]:
        with self._db.transaction() as uow:
            ids = [lk.id for lk in uow.linkers.list_enabled() if lk.id is not None]
        return [self.run_linker(lid) for lid in ids]

    # ---- gather (read I/O) ----------------------------------------------

    def _gather_inputs(
        self, linker_id: int,
    ) -> tuple[LinkerDefinition | None, list[_ClipRaw], list[tuple[int, str]], list[tuple[int, str]],
               list[tuple[int, str]]]:
        with self._db.transaction() as uow:
            linker = uow.linkers.get(linker_id)
            if linker is None:
                return None, [], [], [], []
            defn = load_definition(linker.definition_json)
            assets, _ = uow.assets.search(
                filter=_scope_filter(defn), sort_key="added_desc", offset=0, limit=_GATHER_LIMIT
            )
            clips_raw: list[_ClipRaw] = []
            for asset in assets:
                if asset.id is None:
                    continue
                present = [p for p in uow.assets.get_paths(asset.id) if p.present]
                path = present[0].path if present else None
                clips_raw.append(_ClipRaw(asset.id, dict(asset.metadata), path, asset.metadata_pending))
            pins = [(o.asset_id, o.path) for o in uow.attachment_overrides.list_for_linker(linker_id)
                    if o.decision == "pin"]
            excludes = [(o.asset_id, o.path) for o in uow.attachment_overrides.list_for_linker(linker_id)
                        if o.decision == "exclude"]
            existing = [(a.asset_id, a.path) for a in uow.attachments.list_for_linker(linker_id)]
        return defn, clips_raw, pins, excludes, existing

    def _gather_files(self, defn: LinkerDefinition) -> tuple[list[FileItem], bool]:
        """Walk the target directories. Returns the files AND whether every configured directory was
        actually present -- a missing one (unmounted drive / deleted folder) makes the run
        ``unavailable`` so the caller skips pruning rather than wiping links on a transient failure."""
        target = defn.target
        exts = {e.lower().lstrip(".") for e in target.extensions}
        available = all(Path(d).is_dir() for d in target.directories)
        items: list[FileItem] = []
        folder_order: dict[str, list[str]] = {}
        for directory in target.directories:
            root = Path(directory)
            for path in walk_files(
                root, recursive=target.recursive, follow_symlinks=False, include_hidden=False,
                ignored_globs=target.ignore_globs,
            ):
                if exts and path.suffix.lower().lstrip(".") not in exts:
                    continue
                folder_order.setdefault(str(path.parent), []).append(str(path))
        index_of: dict[str, int] = {}
        for paths in folder_order.values():
            for i, p in enumerate(sorted(paths, key=str.casefold)):
                index_of[p] = i
        for paths in folder_order.values():
            for p in paths:
                ctx = _context_for_path(Path(p), index_of.get(p), {})
                if not _size_ok(ctx.size, target.min_size, target.max_size):
                    continue
                items.append(FileItem(path=p, ctx=ctx))
        return items, available

    # ---- sync (write) ---------------------------------------------------

    def _sync(self, linker_id: int, result: EngineResult, summary: RunSummary, *, prune: bool) -> None:
        with self._db.transaction() as uow:
            keep: set[tuple[int, str]] = set()
            for link in result.links:
                path = Path(link.file_path)
                exists = path.exists()
                if not exists:
                    summary.missing += 1
                size = mtime_ns = None
                if exists:
                    try:
                        st = path.stat()
                        size, mtime_ns = int(st.st_size), st.st_mtime_ns
                    except OSError:
                        exists = False
                uow.attachments.upsert(Attachment(
                    asset_id=link.clip_id, linker_id=linker_id, path=link.file_path,
                    label=path.name, ext=path.suffix.lower().lstrip("."),
                    score=link.score, matched={"why": link.reasons},
                    status="linked" if exists else "missing", origin=link.origin,
                    size=size, mtime_ns=mtime_ns,
                ))
                keep.add((link.clip_id, link.file_path))
                summary.written += 1
            if prune:
                summary.removed = uow.attachments.prune_auto(linker_id, keep)
            uow.commit()


def _size_ok(size: int | None, lo: int | None, hi: int | None) -> bool:
    if lo is None and hi is None:
        return True
    if size is None:
        return False
    return (lo is None or size >= lo) and (hi is None or size <= hi)
