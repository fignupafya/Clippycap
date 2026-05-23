"""The library reconciler: a cheap, hashing-free re-sync of the path index with the filesystem.

A full scan content-hashes every file -- thorough, but far too slow to run constantly. The
reconciler is its lightweight companion: it walks the source folders reading only file *names*,
*sizes* and *mtimes* (no content reads, no BLAKE3, no ffmpeg) and reconciles that against the path
index. It detects files renamed, moved, or deleted *outside* the app since the last scan -- and,
crucially, recognises a renamed file by its unchanged ``(size, mtime)`` (looked up against the hash
cache), so the file keeps its asset and therefore its tags, notes and references.

Because it never touches file contents it is fast enough to run silently in the background every
time the desktop window regains focus, keeping the library in sync with the filesystem without the
user ever noticing. Brand-new files are deliberately left for a real scan (they need hashing).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from clippycap.core.entities import AssetPath, Source
from clippycap.core.events import AssetMissing, AssetUpdated, EventBus
from clippycap.core.ports import Database, UnitOfWork
from clippycap.infra.config import ConfigHolder
from clippycap.infra.scan.walker import walk_files

_log = logging.getLogger(__name__)

# (size in bytes, mtime in nanoseconds) -- a file's content "signature" for the purpose of spotting
# a rename. A move/rename changes neither, so an exact match is a near-certain same-file identity.
_Signature = tuple[int, int]


@dataclass
class ReconcileResult:
    """What one reconcile pass changed. ``changed`` drives whether the UI needs to refresh."""

    renamed: int = 0       # files found at a new path (renamed / moved) -> re-pointed, asset kept
    vanished: int = 0      # tracked files no longer on disk -> their path marked absent
    restored: int = 0      # previously-absent files found again at their known path
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.renamed or self.vanished or self.restored)


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "\\") or path.startswith(root + "/")


class LibraryReconciler:
    def __init__(self, database: Database, event_bus: EventBus, config_holder: ConfigHolder) -> None:
        self._db = database
        self._bus = event_bus
        self._config = config_holder        # read [scan] live, so a settings change takes effect at once

    def reconcile(self) -> ReconcileResult:
        """Walk the sources, diff against the path index, and apply renames / removals / restores."""
        result = ReconcileResult()
        with self._db.transaction() as uow:
            sources = [s for s in uow.sources.list_all() if s.enabled]
        if not sources:
            return result

        disk, roots = self._disk_snapshot(sources)     # filesystem walk -- outside any transaction
        if not roots:
            return result                              # every source is currently unavailable

        touched: set[int] = set()
        became_missing: list[int] = []
        with self._db.transaction() as uow:
            db_paths = uow.assets.all_paths()
            known = {p.path for p in db_paths}
            # disk files the index doesn't know yet -> candidate rename destinations, by signature.
            new_by_sig: dict[_Signature, str] = {}
            for path, signature in disk.items():
                if path not in known:
                    new_by_sig.setdefault(signature, path)
            claimed: set[str] = set()

            for ap in db_paths:
                if ap.id is None or not any(_is_under(ap.path, r) for r in roots):
                    continue                           # a path under an offline source -> leave it alone
                on_disk = ap.path in disk
                if ap.present and not on_disk:
                    dest = self._rename_destination(uow, ap.path, new_by_sig, claimed)
                    if dest is not None:
                        new_path, identity_hash = dest
                        self._apply_rename(uow, ap, new_path, identity_hash, disk[new_path])
                        claimed.add(new_path)
                        result.renamed += 1
                    else:
                        uow.assets.set_path_present(ap.id, present=False)
                        result.vanished += 1
                    touched.add(ap.asset_id)
                elif not ap.present and on_disk:
                    uow.assets.set_path_present(ap.id, present=True)
                    result.restored += 1
                    touched.add(ap.asset_id)

            for asset_id in touched:
                missing = uow.assets.all_paths_absent(asset_id)
                uow.assets.set_missing(asset_id, missing)
                if missing:
                    became_missing.append(asset_id)

        for asset_id in touched:
            self._bus.publish(AssetUpdated(asset_id=asset_id))
        for asset_id in became_missing:
            self._bus.publish(AssetMissing(asset_id=asset_id))
        if result.changed:
            _log.info("reconcile: %d renamed, %d vanished, %d restored",
                      result.renamed, result.vanished, result.restored)
        return result

    # ------------------------------------------------------------------ internals

    def _disk_snapshot(
        self, sources: Sequence[Source]
    ) -> tuple[dict[str, _Signature], list[str]]:
        """Return ``({absolute path -> signature}, [accessible source roots])``.

        A source whose root directory is currently unreachable (an unplugged drive, say) is skipped
        entirely -- and its paths are left untouched by the caller -- so a transient outage never
        flags a whole library as missing."""
        scan = self._config.current.scan
        disk: dict[str, _Signature] = {}
        roots: list[str] = []
        for source in sources:
            root = Path(source.path).resolve()
            if not root.is_dir():
                continue
            roots.append(str(root))
            for file in walk_files(
                root, recursive=source.recursive, follow_symlinks=scan.follow_symlinks,
                include_hidden=scan.include_hidden_files, ignored_globs=scan.ignored_globs,
            ):
                try:
                    st = file.stat()
                except OSError:
                    continue
                disk[str(file)] = (st.st_size, st.st_mtime_ns)
        return disk, roots

    def _rename_destination(
        self, uow: UnitOfWork, gone_path: str,
        new_by_sig: dict[_Signature, str], claimed: set[str],
    ) -> tuple[str, str] | None:
        """If a gone file's exact (size, mtime) -- from the hash cache -- turns up at an as-yet
        unknown path on disk, return ``(that new path, the file's identity hash)``."""
        cached = uow.hash_cache.entry(gone_path)        # (size, mtime_ns, identity_hash) | None
        if cached is None:
            return None
        candidate = new_by_sig.get((cached[0], cached[1]))
        if candidate is None or candidate in claimed:
            return None
        return candidate, cached[2]

    def _apply_rename(
        self, uow: UnitOfWork, old: AssetPath, new_path: str,
        identity_hash: str, signature: _Signature,
    ) -> None:
        uow.assets.rename_path(old.path, new_path)
        uow.hash_cache.forget(old.path)
        uow.hash_cache.put(new_path, signature[0], signature[1], identity_hash)
        asset = uow.assets.get(old.asset_id)
        if asset is not None:
            asset.title = Path(new_path).stem           # the title IS the file name -- keep it in sync
            uow.assets.update(asset)
