"""The scan use case: run a library scan as a background job, bracketed by ScanStarted/ScanCompleted."""

from __future__ import annotations

import logging
import uuid

from clippycap.core.entities import Source
from clippycap.core.errors import NotFoundError
from clippycap.core.events import EventBus, ScanCompleted, ScanStarted
from clippycap.core.ports import Database, JobQueue, ProgressReporter
from clippycap.infra.scan.scanner import LibraryScanner

_log = logging.getLogger(__name__)


class ScanService:
    def __init__(
        self, database: Database, scanner: LibraryScanner, jobs: JobQueue, event_bus: EventBus
    ) -> None:
        self._db = database
        self._scanner = scanner
        self._jobs = jobs
        self._bus = event_bus

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
        return self._submit([source], name=f"scan: {source.path}")

    def scan_all(self) -> str:
        sources = self._enabled_sources()
        return self._submit(sources, name=f"scan: all enabled sources ({len(sources)})")

    def _submit(self, sources: list[Source], *, name: str) -> str:
        scan_id = uuid.uuid4().hex

        def _run(report: ProgressReporter) -> None:
            self._bus.publish(ScanStarted(scan_id=scan_id))
            added = updated = missing = 0
            for source in sources:
                result = self._scanner.scan(source, report=report)
                added += result.added
                updated += result.updated
                missing += result.missing
                if result.errors:
                    _log.warning("scan of %s reported %d errors", source.path, len(result.errors))
            self._bus.publish(
                ScanCompleted(scan_id=scan_id, added=added, updated=updated, missing=missing)
            )

        return self._jobs.submit(name, _run)
