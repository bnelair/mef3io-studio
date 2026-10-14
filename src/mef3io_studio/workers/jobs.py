"""The background job layer.

Usage::

    def work(ctx, location):
        ctx.report(0, None, "Opening")          # indeterminate
        for i, item in enumerate(items):
            ctx.check_cancelled()
            ...
            ctx.report(i + 1, len(items), "Checking")
        return result

    handle = jobs.submit("Checking 12 recordings", work, location)
    handle.succeeded.connect(...)

``work`` runs on a ``QThreadPool`` thread and must not touch widgets. Progress
reports are throttled to at most 10 a second before they cross to the UI
thread, so a job may call ``ctx.report`` as often as it likes. Every outcome
arrives on the UI thread as exactly one of ``succeeded`` / ``failed`` /
``cancelled``, followed by ``finished``. Errors arrive already translated into
a plain-language :class:`~mef3io_studio.model.errors.UserMessage`.

The log gets the job's id, outcome and, on failure, the exception type and
traceback (scrubbed by the privacy filter) — never the job's arguments, which
may be paths or names.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from ..model.cancel import CancelToken, JobCancelled
from ..model.errors import UserMessage, describe_error
from ..model.jobs import JobRecord, JobStatus
from ..model.progress import EtaEstimator, Progress, ProgressThrottle

log = logging.getLogger(__name__)


class JobContext:
    """Handed to the job function on the worker thread."""

    def __init__(self, token: CancelToken, emit: Callable[[Progress], None]):
        self.cancel_token = token
        self._emit = emit
        self._throttle = ProgressThrottle()
        self._latest: Progress | None = None

    @property
    def cancelled(self) -> bool:
        return self.cancel_token.cancelled

    def check_cancelled(self) -> None:
        """Raise :class:`JobCancelled` if the user asked to stop."""
        self.cancel_token.raise_if_cancelled()

    def report(self, done: float, total: float | None = None, phase: str = "") -> None:
        """Report progress. ``total=None`` means "amount of work unknown"."""
        progress = Progress(done, total, phase)
        self._latest = progress
        if self._throttle.should_emit(progress):
            self._emit(progress)


class _Bridge(QObject):
    """Carries results from the worker thread to the UI thread. Lives in the
    UI thread, so its signals are delivered there (queued)."""

    started = Signal()
    progress = Signal(object)
    done = Signal(str, object)  # outcome, payload


class _Runnable(QRunnable):
    def __init__(self, fn, args, kwargs, token: CancelToken, bridge: _Bridge, job_id: int):
        super().__init__()
        self.setAutoDelete(True)
        self._fn, self._args, self._kwargs = fn, args, kwargs
        self._token, self._bridge, self._job_id = token, bridge, job_id

    def run(self) -> None:  # worker thread
        bridge = self._bridge
        bridge.started.emit()
        ctx = JobContext(self._token, bridge.progress.emit)
        try:
            self._token.raise_if_cancelled()
            result = self._fn(ctx, *self._args, **self._kwargs)
        except JobCancelled:
            bridge.done.emit("cancelled", None)
        except Exception as exc:
            log.error(
                "job %d failed with %s\n%s",
                self._job_id,
                type(exc).__name__,
                "".join(traceback.format_tb(exc.__traceback__)),
            )
            bridge.done.emit("failed", describe_error(exc))
        else:
            if ctx._latest is not None and ctx._latest.determinate:
                bridge.progress.emit(
                    Progress(ctx._latest.total, ctx._latest.total, ctx._latest.phase)
                )
            bridge.done.emit("succeeded", result)


class JobHandle(QObject):
    """The UI thread's view of one job."""

    progress = Signal(object)  # Progress
    succeeded = Signal(object)  # the job function's return value
    failed = Signal(object)  # UserMessage
    cancelled = Signal()
    finished = Signal()
    changed = Signal()

    def __init__(self, record: JobRecord, token: CancelToken, parent: QObject | None = None):
        super().__init__(parent)
        self.record = record
        self.token = token
        self.result: Any = None
        self.error: UserMessage | None = None
        self._eta: EtaEstimator | None = None
        self._bridge = _Bridge(self)
        self._bridge.started.connect(self._on_started)
        self._bridge.progress.connect(self._on_progress)
        self._bridge.done.connect(self._on_done)

    def cancel(self) -> None:
        if not self.record.status.finished:
            self.record.cancel_requested = True
            self.token.cancel()
            self.changed.emit()

    @property
    def is_finished(self) -> bool:
        return self.record.status.finished

    @Slot()
    def _on_started(self) -> None:
        if self.record.status is JobStatus.QUEUED:
            self.record.status = JobStatus.RUNNING
            self._eta = EtaEstimator()
            self.changed.emit()

    @Slot(object)
    def _on_progress(self, progress: Progress) -> None:
        if self.record.status.finished:
            return
        self.record.progress = progress
        self.record.eta_seconds = self._eta.remaining(progress) if self._eta else None
        self.progress.emit(progress)
        self.changed.emit()

    @Slot(str, object)
    def _on_done(self, outcome: str, payload: object) -> None:
        record = self.record
        record.eta_seconds = None
        if outcome == "succeeded":
            record.status = JobStatus.SUCCEEDED
            self.result = payload
            if not record.result_text:
                record.result_text = "Finished"
            log.info("job %d finished", record.id)
            self.changed.emit()
            self.succeeded.emit(payload)
        elif outcome == "cancelled":
            record.status = JobStatus.CANCELLED
            record.result_text = "Cancelled"
            log.info("job %d cancelled", record.id)
            self.changed.emit()
            self.cancelled.emit()
        else:
            record.status = JobStatus.FAILED
            self.error = payload  # type: ignore[assignment]
            record.result_text = self.error.title if self.error else "Failed"
            self.changed.emit()
            self.failed.emit(payload)
        self.finished.emit()


class JobManager(QObject):
    """Owns the thread pool and every job submitted in this session."""

    job_added = Signal(object)  # JobHandle
    job_changed = Signal(object)  # JobHandle
    running_changed = Signal(int)  # number of unfinished jobs

    def __init__(self, parent: QObject | None = None, max_threads: int | None = None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        if max_threads:
            self.pool.setMaxThreadCount(max_threads)
        self.handles: list[JobHandle] = []

    def submit(self, title: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> JobHandle:
        """Run ``fn(ctx, *args, **kwargs)`` in the background. ``title`` is
        shown to the user; it must not contain patient details."""
        record = JobRecord(title=title)
        token = CancelToken()
        handle = JobHandle(record, token, self)
        handle.changed.connect(lambda h=handle: self._on_changed(h))
        self.handles.append(handle)
        log.info("job %d queued", record.id)
        self.job_added.emit(handle)
        self.running_changed.emit(self.running_count())
        self.pool.start(_Runnable(fn, args, kwargs, token, handle._bridge, record.id))
        return handle

    def _on_changed(self, handle: JobHandle) -> None:
        self.job_changed.emit(handle)
        self.running_changed.emit(self.running_count())

    def running_count(self) -> int:
        return sum(1 for h in self.handles if not h.is_finished)

    def cancel_all(self) -> None:
        for handle in self.handles:
            handle.cancel()

    def clear_finished(self) -> list[JobHandle]:
        removed = [h for h in self.handles if h.is_finished]
        self.handles = [h for h in self.handles if not h.is_finished]
        return removed

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """Cancel everything and wait for the workers to stop (app exit)."""
        self.cancel_all()
        return self.pool.waitForDone(timeout_ms)
