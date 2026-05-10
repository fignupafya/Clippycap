"""A small thread-pool job queue for background work (library scans, thumbnail batches).

One worker by default, so scans never overlap (they would collide on the write transaction anyway).
:meth:`ThreadJobQueue.get` / :meth:`ThreadJobQueue.list_all` return *snapshots* of the job state.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from clippycap.core.ports import JobHandle, ProgressReporter

_log = logging.getLogger(__name__)


class _Reporter:
    def __init__(self, handle: JobHandle, lock: threading.Lock) -> None:
        self._handle = handle
        self._lock = lock

    def update(self, scanned: int, total: int | None = None, message: str = "") -> None:
        with self._lock:
            self._handle.scanned = scanned
            if total is not None:
                self._handle.total = total
            if message:
                self._handle.message = message


class ThreadJobQueue:
    def __init__(self, *, max_workers: int = 1) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="clippycap-job")
        self._jobs: dict[str, JobHandle] = {}
        self._lock = threading.Lock()

    def submit(self, name: str, run: Callable[[ProgressReporter], None]) -> str:
        job_id = uuid.uuid4().hex
        handle = JobHandle(id=job_id, name=name)
        with self._lock:
            self._jobs[job_id] = handle
        reporter = _Reporter(handle, self._lock)

        def _runner() -> None:
            with self._lock:
                handle.state = "running"
            try:
                run(reporter)
            except Exception as exc:
                _log.exception("background job %r (%s) failed", name, job_id)
                with self._lock:
                    handle.state = "error"
                    handle.error = str(exc)
            else:
                with self._lock:
                    handle.state = "done"

        self._executor.submit(_runner)
        return job_id

    def get(self, job_id: str) -> JobHandle | None:
        with self._lock:
            handle = self._jobs.get(job_id)
            return replace(handle) if handle is not None else None

    def list_all(self) -> list[JobHandle]:
        with self._lock:
            return [replace(handle) for handle in self._jobs.values()]

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
