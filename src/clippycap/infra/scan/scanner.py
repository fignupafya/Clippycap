"""The library scanner -- the *discovery* phase of a scan.

It walks a source folder and, per file: picks a media-type provider that recognises it; skips
zero-byte files and files modified within the configured window (they may still be being written);
looks the content identity up in the cache or computes it; finds or creates the asset; records the
path. A newly created asset is marked ``metadata_pending`` -- its duration / resolution are read
afterwards, off the critical path, by the :class:`~clippycap.infra.scan.enricher.MetadataEnricher`.

The scan commits every ``[scan].commit_batch_size`` files instead of once at the very end, so a
large first scan streams progressively into the library grid and the app stays usable throughout.
After every file is walked, paths under the source no longer on disk are marked absent and assets
with no present path become *missing* (never deleted); a re-found asset has its *missing* flag
cleared.

Per-asset events (:class:`AssetAdded`, :class:`AssetMissing`) are published *after* the batch that
produced them commits, so a subscriber never sees an asset a rollback then discarded. The
surrounding ``ScanStarted`` / ``ScanCompleted`` events are published by the application layer.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clippycap.core.entities import Asset, Source
from clippycap.core.events import AssetAdded, AssetMissing, Event, EventBus
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


def _chunks(items: list[Path], size: int) -> Iterator[list[Path]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


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

    @staticmethod
    def _quick_metadata(provider: MediaTypeProvider, file: Path) -> dict[str, Any]:
        """The cheap, no-subprocess metadata recorded at discovery (a video's recorded_at). The
        full set -- duration, resolution -- is read later by the enrichment phase."""
        try:
            return provider.quick_metadata(file)
        except Exception:        # a misbehaving provider must not abort the scan
            return {}

    def _process_file(
        self, uow: UnitOfWork, file: Path, wanted: set[str], now: float,
        result: ScanResult, seen_paths: list[str], events: list[Event],
    ) -> None:
        """Discover one file: identify it, find or create its asset, record its path. Appends the
        file's path to ``seen_paths`` and any domain event to ``events`` (published post-commit)."""
        provider = self._detect(file)
        if provider is None or (wanted and provider.media_type not in wanted):
            return
        try:
            st = file.stat()
        except OSError as exc:
            result.errors.append(f"{file}: {exc}")
            return
        if st.st_size == 0 or now - st.st_mtime < self._scan.skip_modified_within_seconds:
            return
        result.files_seen += 1
        path_str = str(file)
        seen_paths.append(path_str)             # the file is confirmed on disk -> it counts as "seen"
        identity = uow.hash_cache.get(path_str, st.st_size, st.st_mtime_ns)
        if identity is None:
            strategy = self._strategies.get(provider.identity_strategy_name)
            if strategy is None:
                result.errors.append(
                    f"{file}: unknown identity strategy {provider.identity_strategy_name!r}"
                )
                return
            try:
                identity = strategy.compute(file, st.st_size)
            except OSError as exc:
                result.errors.append(f"{file}: {exc}")
                return
            uow.hash_cache.put(path_str, st.st_size, st.st_mtime_ns, identity)
        vol = _volume_id(file) if self._dedup else None
        existing = uow.assets.get_by_hash(identity)
        if existing is None:
            # Discovery records the asset with only the cheap metadata (no ffprobe); the enrichment
            # phase later fills in duration / resolution and clears metadata_pending.
            asset = uow.assets.add(Asset(
                identity_hash=identity, media_type=provider.media_type,
                title=provider.display_title(file, {}), size_bytes=st.st_size,
                metadata=self._quick_metadata(provider, file), metadata_pending=True,
            ))
            assert asset.id is not None
            uow.assets.upsert_path(asset.id, path_str, vol)
            events.append(
                AssetAdded(asset_id=asset.id, identity_hash=identity, media_type=asset.media_type)
            )
            result.added += 1
        else:
            assert existing.id is not None
            uow.assets.upsert_path(existing.id, path_str, vol)
            uow.assets.touch_seen(existing.id)
            uow.assets.set_missing(existing.id, False)
            result.updated += 1

    def scan(self, source: Source, *, report: ProgressReporter | None = None) -> ScanResult:
        if source.id is None:
            raise ValueError("cannot scan an unsaved source")
        source_id = source.id
        result = ScanResult(source_id=source_id)
        root = Path(source.path).resolve()
        wanted = set(source.media_types)            # empty => every media type
        if report is not None:
            report.update(0, None, "Finding files…")
        # Materialise the walk: it lets the scan report a real total, and a fresh transaction is
        # opened per batch (so an interrupted scan keeps the batches that already committed).
        files = list(walk_files(
            root, recursive=source.recursive, follow_symlinks=self._scan.follow_symlinks,
            include_hidden=self._scan.include_hidden_files, ignored_globs=self._scan.ignored_globs,
        ))
        total = len(files)
        now = time.time()
        seen_paths: list[str] = []
        if report is not None:
            report.update(0, total, "Discovering files…")
        for batch in _chunks(files, self._scan.commit_batch_size):
            batch_events: list[Event] = []
            with self._db.transaction() as uow:
                for file in batch:
                    self._process_file(uow, file, wanted, now, result, seen_paths, batch_events)
            for event in batch_events:              # publish only once the batch has committed
                self._bus.publish(event)
            if report is not None:
                report.update(result.files_seen, total, "Discovering files…")
        self._finalise(root, source_id, seen_paths, result)
        return result

    def _finalise(
        self, root: Path, source_id: int, seen_paths: list[str], result: ScanResult
    ) -> None:
        """Mark paths under the source not seen this scan absent, flag assets left with no present
        path as missing, and stamp the source's last-scanned time -- in one final transaction."""
        missing_events: list[Event] = []
        with self._db.transaction() as uow:
            for asset_id in uow.assets.reconcile_paths_under(str(root), seen_paths):
                if uow.assets.all_paths_absent(asset_id):
                    uow.assets.set_missing(asset_id, True)
                    missing_events.append(AssetMissing(asset_id=asset_id))
                    result.missing += 1
            uow.sources.touch_scanned(source_id)
        for event in missing_events:
            self._bus.publish(event)
