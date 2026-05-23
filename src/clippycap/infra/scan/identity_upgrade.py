"""One-time identity-format upgrade.

An asset's content identity is produced by an :class:`IdentityStrategy` and stored
algorithm-prefixed (``b3:`` for the full BLAKE3 hash, ``b3c:`` for the composite one). Changing the
configured strategy -- e.g. to the faster composite hash -- would otherwise leave the existing
library on the old format, which breaks move/duplicate detection: a moved file gets re-hashed in
the *new* format and no longer matches its asset.

:class:`IdentityUpgrader` re-identifies every asset whose stored hash was not produced by its media
type's current strategy -- it re-hashes the file and updates the asset and the hash cache to match.
It is idempotent and, once the whole library is on the current format, a no-op after one cheap
query, so it is safe to run at every startup. It runs as a background job submitted *before* any
scan, so a scan never sees a half-upgraded library.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

from clippycap.core.errors import ConflictError
from clippycap.core.ports import Database, IdentityStrategy, MediaTypeProvider, UnitOfWork

_log = logging.getLogger(__name__)


class IdentityUpgrader:
    def __init__(
        self,
        database: Database,
        media_types: Sequence[MediaTypeProvider],
        identity_strategies: Mapping[str, IdentityStrategy],
    ) -> None:
        self._db = database
        self._media_types = media_types
        self._strategies = identity_strategies

    def run(self) -> int:
        """Re-identify every asset not on its media type's current identity format. Returns the
        number re-identified; 0 (after one cheap query per media type) once the library is uniform."""
        upgraded = 0
        for provider in self._media_types:
            strategy = self._strategies.get(provider.identity_strategy_name)
            if strategy is None:
                continue
            with self._db.transaction() as uow:
                legacy_ids = uow.assets.asset_ids_with_foreign_identity(
                    provider.media_type, strategy.identity_prefix
                )
            for asset_id in legacy_ids:
                if self._upgrade_one(asset_id, strategy):
                    upgraded += 1
        if upgraded:
            _log.info("re-identified %d asset(s) onto the current identity format", upgraded)
        return upgraded

    def _upgrade_one(self, asset_id: int, strategy: IdentityStrategy) -> bool:
        """Re-hash one asset's file with ``strategy`` and update the asset + hash cache. Each asset
        is its own transaction so an interruption keeps the assets already done. Returns whether it
        was actually re-identified."""
        with self._db.transaction() as uow:
            asset = uow.assets.get(asset_id)
            if asset is None or asset.identity_hash.startswith(strategy.identity_prefix):
                return False                        # deleted, or already upgraded by another path
            path = self._readable_path(uow, asset_id)
            if path is None:
                return False                        # file unreachable now -> retried on a later run
            try:
                new_hash = strategy.compute(path, path.stat().st_size)
            except OSError as exc:
                _log.warning("identity upgrade: cannot read %s: %s", path, exc)
                return False
            asset.identity_hash = new_hash
            try:
                uow.assets.update(asset)
            except ConflictError:
                # Another asset already holds new_hash -- only reachable via a (practically
                # impossible) composite-hash collision. Leave this asset on its old format; the
                # library stays internally consistent and the app keeps working.
                _log.warning("identity upgrade: %s collides with an existing asset; left as-is", path)
                return False
            self._reindex_cache(uow, asset_id, new_hash)
        return True

    @staticmethod
    def _readable_path(uow: UnitOfWork, asset_id: int) -> Path | None:
        for entry in uow.assets.get_paths(asset_id):
            candidate = Path(entry.path)
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _reindex_cache(uow: UnitOfWork, asset_id: int, new_hash: str) -> None:
        """Point every hash-cache row for the asset's paths at the new identity, so the next scan
        finds a consistent (cached hash == asset hash) and does not treat the file as new content."""
        for entry in uow.assets.get_paths(asset_id):
            cached = uow.hash_cache.entry(entry.path)
            if cached is not None:
                uow.hash_cache.put(entry.path, cached[0], cached[1], new_hash)
