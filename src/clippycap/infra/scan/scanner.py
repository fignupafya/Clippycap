"""The library scanner: discovers media under a source and assigns/refreshes assets by content hash.

One scan == one database transaction. Per file: pick a media-type provider that recognises it; skip
zero-byte files and files modified within the configured window (they may still be being written);
look the content hash up in the cache or compute it; find or create the asset; record the path.
Afterwards: paths under this source not seen this scan are marked absent, and assets with no present
path become *missing* (never deleted); a re-found asset has its *missing* flag cleared. Per-asset
events (:class:`~clippycap.core.events.AssetAdded`, :class:`~clippycap.core.events.AssetMissing`) are
published; the surrounding ``ScanStarted`` / ``ScanCompleted`` events carry a scan id and are
published by the application layer.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clippycap.core.entities import Asset, Source
from clippycap.core.events import AssetAdded, AssetMissing, EventBus
from clippycap.core.ports import (
    Database,
    IdentityStrategy,
    MediaTypeProvider,
    ProgressReporter,
    UnitOfWork,
)
from clippycap.infra.config import Config
from clippycap.infra.scan.walker import walk_files


@dataclass
class ScanResult:
    source_id: int
    files_seen: int = 0
    added: int = 0
    updated: int = 0       # existing assets re-found
    missing: int = 0       # assets that became missing during this scan
    errors: list[str] = field(default_factory=list)


def _volume_id(path: Path) -> str | None:
    try:
        return str(path.stat().st_dev) or None
    except OSError:
        return None


class LibraryScanner:
    def __init__(
        self,
        database: Database,
        media_types: Sequence[MediaTypeProvider],
        identity_strategies: Mapping[str, IdentityStrategy],
        event_bus: EventBus,
        config: Config,
    ) -> None:
        self._db = database
        self._media_types = media_types
        self._strategies = identity_strategies
        self._bus = event_bus
        self._scan = config.scan
        self._dedup = config.identity.dedup_by_volume_file_id

    def _detect(self, path: Path) -> MediaTypeProvider | None:
        for provider in self._media_types:
            try:
                if provider.detect(path):
                    return provider
            except OSError:
                return None
        return None

    def _metadata(self, provider: MediaTypeProvider, file: Path) -> dict[str, Any]:
        try:
            return provider.extract_metadata(file)
        except Exception:
            return {}

    def _process_file(
        self, uow: UnitOfWork, file: Path, wanted: set[str], now: float, result: ScanResult
    ) -> str | None:
        provider = self._detect(file)
        if provider is None or (wanted and provider.media_type not in wanted):
            return None
        try:
            st = file.stat()
        except OSError as exc:
            result.errors.append(f"{file}: {exc}")
            return None
        if st.st_size == 0 or now - st.st_mtime < self._scan.skip_modified_within_seconds:
            return None
        result.files_seen += 1
        path_str = str(file)
        identity = uow.hash_cache.get(path_str, st.st_size, st.st_mtime_ns)
        if identity is None:
            strategy = self._strategies.get(provider.identity_strategy_name)
            if strategy is None:
                result.errors.append(
                    f"{file}: unknown identity strategy {provider.identity_strategy_name!r}"
                )
                return None
            try:
                identity = strategy.compute(file, st.st_size)
            except OSError as exc:
                result.errors.append(f"{file}: {exc}")
                return None
            uow.hash_cache.put(path_str, st.st_size, st.st_mtime_ns, identity)
        vol = _volume_id(file) if self._dedup else None
        existing = uow.assets.get_by_hash(identity)
        if existing is None:
            metadata = self._metadata(provider, file)
            asset = uow.assets.add(Asset(
                identity_hash=identity, media_type=provider.media_type,
                title=provider.display_title(file, metadata), size_bytes=st.st_size, metadata=metadata,
            ))
            assert asset.id is not None
            uow.assets.upsert_path(asset.id, path_str, vol)
            self._bus.publish(
                AssetAdded(asset_id=asset.id, identity_hash=identity, media_type=asset.media_type)
            )
            result.added += 1
        else:
            assert existing.id is not None
            uow.assets.upsert_path(existing.id, path_str, vol)
            uow.assets.touch_seen(existing.id)
            uow.assets.set_missing(existing.id, False)
            result.updated += 1
        return path_str

    def scan(self, source: Source, *, report: ProgressReporter | None = None) -> ScanResult:
        if source.id is None:
            raise ValueError("cannot scan an unsaved source")
        result = ScanResult(source_id=source.id)
        root = Path(source.path).resolve()
        wanted = set(source.media_types)            # empty => every media type
        seen_paths: list[str] = []
        now = time.time()
        with self._db.transaction() as uow:
            for file in walk_files(
                root,
                recursive=source.recursive,
                follow_symlinks=self._scan.follow_symlinks,
                include_hidden=self._scan.include_hidden_files,
                ignored_globs=self._scan.ignored_globs,
            ):
                path_str = self._process_file(uow, file, wanted, now, result)
                if path_str is not None:
                    seen_paths.append(path_str)
                    if report is not None:
                        report.update(result.files_seen, None, file.name)
            for asset_id in uow.assets.reconcile_paths_under(str(root), seen_paths):
                if uow.assets.all_paths_absent(asset_id):
                    uow.assets.set_missing(asset_id, True)
                    self._bus.publish(AssetMissing(asset_id=asset_id))
                    result.missing += 1
            uow.sources.touch_scanned(source.id)
        return result
