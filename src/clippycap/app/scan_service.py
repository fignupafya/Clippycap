"""The scan use cases: run library maintenance as background jobs.

* ``scan_all`` / ``scan_source`` -- a full scan: a *discovery* phase (find + identify files, so the
  clips stream into the library) followed by an *enrichment* phase (read each clip's duration /
  resolution). Bracketed by ``ScanStarted`` / ``ScanCompleted``.
* ``enrich_pending``          -- the enrichment phase alone, for clips a previous scan recorded but
  did not finish (an interrupted scan, or clips found before ffmpeg was available).
* ``upgrade_identity_format`` -- re-identify a library left on a superseded identity format.
* ``reconcile``               -- a fast, hashing-free re-sync of the path index.
"""

from __future__ import annotations

import logging
import uuid

from clippycap.core.entities import Source
from clippycap.core.errors import NotFoundError
from clippycap.core.events import EventBus, ScanCompleted, ScanStarted
from clippycap.core.ports import Database, JobQueue, ProgressReporter
from clippycap.infra.scan.enricher import MetadataEnricher
from clippycap.infra.scan.identity_upgrade import IdentityUpgrader
from clippycap.infra.scan.reconciler import LibraryReconciler, ReconcileResult
from clippycap.infra.scan.scanner import LibraryScanner

_log = logging.getLogger(__name__)


class ScanService:
    def __init__(
        self,
        database: Database,
        scanner: LibraryScanner,
        reconciler: LibraryReconciler,
        enricher: MetadataEnricher,
        identity_upgrader: IdentityUpgrader,
        jobs: JobQueue,
        event_bus: EventBus,
    ) -> None:
        self._db = database
        self._scanner = scanner
        self._reconciler = reconciler
        self._enricher = enricher
        self._identity_upgrader = identity_upgrader
        self._jobs = jobs
        self._bus = event_bus

    def reconcile(self) -> ReconcileResult:
        """A fast, hashing-free re-sync of the path index: pick up files renamed / moved / deleted
        outside the app. Cheap enough to run synchronously (the HTTP layer calls it off the event
        loop); see :class:`~clippycap.infra.scan.reconciler.LibraryReconciler`."""
        return self._reconciler.reconcile()

    def _source(self, source_id: int) -> Source:
        with self._db.transaction() as uow:
            source = uow.sources.get(source_id)
        if source is None:
            raise NotFoundError(f"no source with id {source_id!r}")
        return source

    def _enabled_sources(self) -> list[Source]:
        with self._db.transaction() as uow:
            return [s for s in uow.sources.list_all() if s.enabled]

    def scan_source(self, source_id: int) -> str:
        source = self._source(source_id)
        return self._submit_scan([source], name=f"scan: {source.path}")

    def scan_all(self) -> str:
        sources = self._enabled_sources()
        return self._submit_scan(sources, name=f"scan: all enabled sources ({len(sources)})")

    def enrich_pending(self) -> str:
        """Run only the enrichment phase as a background job -- for clips a scan recorded but did
        not finish. A no-op when nothing is pending; the returned job id is pollable like a scan's."""
        def _run(report: ProgressReporter) -> None:
            self._enricher.run(report=report)

        return self._jobs.submit("enrich pending clips", _run)

    def upgrade_identity_format(self) -> str:
        """Re-identify, as a background job, any library left on a superseded identity format. Runs
        before any scan would, so a scan never sees a half-upgraded library; a no-op once uniform."""
        def _run(report: ProgressReporter) -> None:
            del report                                   # the upgrade reports no per-item progress
            self._identity_upgrader.run()

        return self._jobs.submit("upgrade identity format", _run)

    def _submit_scan(self, sources: list[Source], *, name: str) -> str:
        scan_id = uuid.uuid4().hex

        def _run(report: ProgressReporter) -> None:
            self._bus.publish(ScanStarted(scan_id=scan_id))
            added = updated = missing = 0
            for source in sources:                       # phase 1: discovery -- clips stream in
                result = self._scanner.scan(source, report=report)
                added += result.added
                updated += result.updated
                missing += result.missing
                if result.errors:
                    _log.warning("scan of %s reported %d errors", source.path, len(result.errors))
            self._enricher.run(report=report)            # phase 2: read duration / resolution
            self._bus.publish(
                ScanCompleted(scan_id=scan_id, added=added, updated=updated, missing=missing)
            )

        return self._jobs.submit(name, _run)
