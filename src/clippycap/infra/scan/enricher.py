"""The metadata enricher -- the *enrichment* phase of a scan.

A scan's discovery phase records each asset fast, with only the cheap metadata (a video's
``recorded_at``) and ``metadata_pending = True``. This pass then reads the full set -- duration,
resolution, codec -- with the media type's real extractor (ffprobe, for video) and clears the
flag, committing every ``[scan].commit_batch_size`` assets so the durations fill into the library
grid progressively while the user is already browsing.

It is also the self-healing path: it always processes *every* pending asset, not just one scan's,
so an interrupted scan -- or assets discovered while ffmpeg was missing -- are finished off the
next time enrichment runs (after any scan, at startup, or once ffmpeg becomes available).

``AssetUpdated`` events are published after the batch that produced them commits.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from clippycap.core.events import AssetUpdated, Event, EventBus
from clippycap.core.ports import Database, MediaTypeProvider, ProgressReporter, UnitOfWork
from clippycap.infra.config import Config

_log = logging.getLogger(__name__)


@dataclass
class EnrichResult:
    enriched: int = 0          # assets whose metadata was extracted this pass
    skipped: int = 0           # pending assets left for later (no reachable file / no extractor)
    errors: list[str] = field(default_factory=list)


class MetadataEnricher:
    def __init__(
        self,
        database: Database,
        media_types: Mapping[str, MediaTypeProvider],
        event_bus: EventBus,
        config: Config,
    ) -> None:
        self._db = database
        self._media_types = media_types
        self._bus = event_bus
        self._batch_size = config.scan.commit_batch_size

    def run(self, *, report: ProgressReporter | None = None) -> EnrichResult:
        """Extract the full metadata of every asset still flagged ``metadata_pending``."""
        result = EnrichResult()
        with self._db.transaction() as uow:
            pending = uow.assets.pending_metadata_ids()
        if not pending:
            return result
        if not any(p.metadata_extraction_available for p in self._media_types.values()):
            # No extractor is available (e.g. ffprobe is not installed yet) -- leave the assets
            # pending; a later run (after a scan, or once ffmpeg is installed) finishes them.
            result.skipped = len(pending)
            return result
        total = len(pending)
        if report is not None:
            report.update(0, total, "Reading clip details…")
        for start in range(0, total, self._batch_size):
            batch = pending[start:start + self._batch_size]
            events: list[Event] = []
            with self._db.transaction() as uow:
                for asset_id in batch:
                    self._enrich_one(uow, asset_id, result, events)
            for event in events:                    # publish only once the batch has committed
                self._bus.publish(event)
            if report is not None:
                report.update(result.enriched + result.skipped, total, "Reading clip details…")
        if result.errors:
            _log.warning("metadata enrichment reported %d errors", len(result.errors))
        return result

    def _enrich_one(
        self, uow: UnitOfWork, asset_id: int, result: EnrichResult, events: list[Event]
    ) -> None:
        asset = uow.assets.get(asset_id)
        if asset is None or not asset.metadata_pending:
            return                                  # deleted, or already enriched by an earlier run
        provider = self._media_types.get(asset.media_type)
        if provider is None or not provider.metadata_extraction_available:
            result.skipped += 1
            return                                  # leave it pending for a future run
        path = self._present_path(uow, asset_id)
        if path is None:
            result.skipped += 1
            return                                  # the file is currently unreachable on disk
        try:
            metadata = provider.extract_metadata(path)
        except Exception as exc:        # one unreadable file must not abort the pass
            result.errors.append(f"{path}: {exc}")
            metadata = {}
        # Merge over the discovery metadata so a failed / empty extraction never drops recorded_at.
        asset.metadata = {**asset.metadata, **metadata}
        asset.metadata_pending = False              # extraction was attempted with the tool available
        uow.assets.update(asset)
        events.append(AssetUpdated(asset_id=asset_id))
        result.enriched += 1

    @staticmethod
    def _present_path(uow: UnitOfWork, asset_id: int) -> Path | None:
        for entry in uow.assets.get_paths(asset_id):
            candidate = Path(entry.path)
            if entry.present and candidate.is_file():
                return candidate
        return None
