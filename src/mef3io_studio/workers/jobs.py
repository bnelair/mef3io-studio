"""The background job layer.

Usage::

    def work(ctx, items):
        ctx.report(0, None, "Looking for recordings")     # amount unknown
        for i, item in enumerate(items):
            ctx.check_cancelled()
            ...
            ctx.report(i + 1, len(items), "Checking")
        return result

    handle = jobs.submit("Checking 12 recordings", work, items)
    handle.succeeded.connect(...)

``work`` runs on a ``QThreadPool`` thread and must not touch widgets.

* **Progress** goes into a one-slot mailbox (:class:`ProgressSlot`); the UI
  thread collects the newest report from every running job on one timer, at
  most 10 times a second. A job may report as often as it likes, phases
  included, without ever flooding the event loop.
* **Outcome:** exactly one of ``succeeded`` / ``failed`` / ``cancelled``,
  then ``finished``, always on the UI thread — even if the job raised
  ``SystemExit`` or ``KeyboardInterrupt``. Errors arrive translated into a
  plain-language :class:`~mef3io_studio.model.errors.UserMessage`.
* **Cleanup:** ``ctx.add_cleanup(fn)`` registers a function that runs (on the
  worker thread) if the job is cancelled or fails — e.g. removing a
  half-written copy.
* **Helper process:** ``ctx.run_isolated(fn, ...)`` runs a call that holds the
  GIL (see :mod:`.isolated`) in a helper process, cancellable.
* **Results while working:** ``ctx.publish(item)`` hands an item to the UI
  before the job ends (``JobHandle.published`` delivers a list of them, on
  the same throttled timer as progress, and always before the outcome).

The log gets the job's id, outcome and, on failure, the exception type and
traceback (scrubbed by the privacy filter) — never the job's arguments, which
may be paths or names.
"""

from __future__ import annotations

import logging
import traceback
import warnings
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

from ..model.cancel import CancelToken, is_stop
from ..model.errors import UserMessage, describe_error
from ..model.jobs import JobRecord, JobStatus
from ..model.progress import (
    UI_UPDATES_PER_SECOND,
    EtaEstimator,
    Mailbox,
    Progress,
    ProgressSlot,
)
from .isolated import run_isolated

log = logging.getLogger(__name__)


class JobContext:
    """Handed to the job function on the worker thread."""

    def __init__(
        self,
        token: CancelToken,
        slot: ProgressSlot,
        job_id: int = 0,
        mailbox: Mailbox | None = None,
    ):
        self.cancel_token = token
        self.job_id = job_id
        self._slot = slot
        self._mailbox = mailbox if mailbox is not None else Mailbox()
        self._cleanups: list[Callable[[], Any]] = []

    @property
    def cancelled(self) -> bool:
        return self.cancel_token.cancelled

    def check_cancelled(self) -> None:
        """Raise :class:`JobCancelled` if the user asked to stop."""
        self.cancel_token.raise_if_cancelled()

    def report(self, done: float, total: float | None = None, phase: str = "") -> None:
        """Report progress. ``total=None`` means "amount of work unknown".
        Cheap: it only overwrites the latest report."""
        self._slot.put(Progress(done, total, phase))

    def publish(self, item: Any) -> None:
        """Hand ``item`` to the UI now, before the job ends (see
        ``JobHandle.published``)."""
        self._mailbox.put(item)

    def add_cleanup(self, fn: Callable[[], Any]) -> None:
        """Run ``fn()`` if the job is cancelled or fails (last added runs
        first). Use it to remove partial output."""
        self._cleanups.append(fn)

    def run_isolated(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_message: Callable[[Any], None] | None = None,
        stop_grace: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run ``fn`` in a helper process; Stop stops the helper. See
        :func:`.isolated.run_isolated` for ``on_message`` /
        ``stop_grace``."""
        return run_isolated(
            fn,
            *args,
            cancel=self.cancel_token,
            on_message=on_message,
            stop_grace=stop_grace,
            **kwargs,
        )

    def _run_cleanups(self) -> None:
        while self._cleanups:
            fn = self._cleanups.pop()
            try:
                fn()
            except Exception as exc:
                log.warning(
                    "job %d: a cleanup step failed with %s", self.job_id, type(exc).__name__
                )


class _Bridge(QObject):
    """Carries the start and the outcome from the worker thread to the UI
    thread. Lives in the UI thread, so its signals are delivered there."""

    started = Signal()
    done = Signal(str, object)  # outcome, payload


def _safe_describe(exc: BaseException) -> UserMessage:
    try:
        return describe_error(exc)
    except BaseException:  # pragma: no cover - describe_error already guards itself
        return UserMessage("Something went wrong", "The task stopped unexpectedly.", "")


class _Runnable(QRunnable):
    def __init__(
        self, fn, args, kwargs, token: CancelToken, slot: ProgressSlot, bridge, job_id, mailbox
    ):
        super().__init__()
        self.setAutoDelete(True)
        self._fn, self._args, self._kwargs = fn, args, kwargs
        self._token, self._slot, self._bridge, self._job_id = token, slot, bridge, job_id
        self._mailbox = mailbox

    def run(self) -> None:  # worker thread
        bridge = self._bridge
        outcome, payload = "failed", None
        ctx = JobContext(self._token, self._slot, self._job_id, self._mailbox)
        try:
            bridge.started.emit()
            self._token.raise_if_cancelled()
            payload = self._fn(ctx, *self._args, **self._kwargs)
            outcome = "succeeded"
        except BaseException as exc:  # noqa: BLE001 - incl. SystemExit/KeyboardInterrupt
            if is_stop(exc):  # ours, or mef3io.Cancelled: stopped on request, not a failure
                outcome, payload = "cancelled", None
            else:
                log.error(
                    "job %d failed with %s\n%s",
                    self._job_id,
                    type(exc).__name__,
                    "".join(traceback.format_tb(exc.__traceback__)),
                )
                outcome, payload = "failed", _safe_describe(exc)
        finally:
            if outcome != "succeeded":
                ctx._run_cleanups()
            # Exactly one outcome, always, and nothing touches the bridge after.
            self._bridge = None
            bridge.done.emit(outcome, payload)


class JobHandle(QObject):
    """The UI thread's view of one job."""

    progress = Signal(object)  # Progress
    published = Signal(list)  # items from ctx.publish, oldest first
    succeeded = Signal(object)  # the job function's return value
    failed = Signal(object)  # UserMessage
    cancelled = Signal()
    finished = Signal()
    changed = Signal()

    def __init__(self, record: JobRecord, token: CancelToken, parent: QObject | None = None):
        super().__init__(parent)
        self.record = record
        self.token = token
        self.slot = ProgressSlot()
        self.mailbox = Mailbox()
        self.result: Any = None
        self.error: UserMessage | None = None
        self._eta: EtaEstimator | None = None
        self._bridge = _Bridge(self)
        self._bridge.started.connect(self._on_started)
        self._bridge.done.connect(self._on_done)

    def cancel(self) -> None:
        if not self.record.status.finished and not self.record.cancel_requested:
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

    def pull_progress(self) -> None:
        """Called by the manager's timer on the UI thread."""
        self._deliver_published()
        progress = self.slot.take()
        if progress is None or self.record.status.finished:
            return
        self.record.progress = progress
        self.record.eta_seconds = self._eta.remaining(progress) if self._eta else None
        self.progress.emit(progress)
        self.changed.emit()

    def _deliver_published(self) -> None:
        items = self.mailbox.take_all()
        if items:
            self.published.emit(items)

    @Slot(str, object)
    def _on_done(self, outcome: str, payload: object) -> None:
        record = self.record
        if record.status.finished:  # pragma: no cover - the runnable emits once
            return
        # Whatever the outcome, items published before it are real results
        # (the recordings opened before Stop was pressed).
        self._deliver_published()
        if outcome == "succeeded":
            # Flush the final report so the last update is never lost.
            self.pull_progress()
            if record.progress.determinate and record.progress.done < record.progress.total:
                record.progress = Progress(
                    record.progress.total, record.progress.total, record.progress.phase
                )
                self.progress.emit(record.progress)
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
            record.result_text = "Stopped"
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
    """Owns the thread pool and every job submitted in this session.

    Teardown order is explicit (:meth:`dispose`): cancel, wait for the pool's
    threads, and only then delete the handles their runnables report to.
    """

    job_added = Signal(object)  # JobHandle
    job_changed = Signal(object)  # JobHandle
    jobs_removed = Signal()
    running_changed = Signal(int)  # number of unfinished jobs

    def __init__(self, parent: QObject | None = None, max_threads: int | None = None):
        super().__init__(parent)
        self.pool = QThreadPool(self)
        if max_threads:
            self.pool.setMaxThreadCount(max_threads)
        self.handles: list[JobHandle] = []
        self._timer = QTimer(self)
        self._timer.setInterval(1000 // UI_UPDATES_PER_SECOND)
        self._timer.timeout.connect(self._pull_progress)

    def submit(self, title: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> JobHandle:
        """Run ``fn(ctx, *args, **kwargs)`` in the background. ``title`` is
        shown to the user; it must not contain patient details."""
        record = JobRecord(title=title)
        token = CancelToken()
        handle = JobHandle(record, token, self)
        # A bound slot, not a lambda capturing the handle: a closure over the
        # handle, stored in its own C++ connection, is a cycle Python's GC
        # cannot see, and every finished job would stay in memory forever.
        handle.changed.connect(self._on_handle_changed)
        self.handles.append(handle)
        log.info("job %d queued", record.id)
        self.job_added.emit(handle)
        self.running_changed.emit(self.running_count())
        if not self._timer.isActive():
            self._timer.start()
        self.pool.start(
            _Runnable(
                fn, args, kwargs, token, handle.slot, handle._bridge, record.id, handle.mailbox
            )
        )
        return handle

    def _pull_progress(self) -> None:
        running = [h for h in self.handles if not h.is_finished]
        for handle in running:
            handle.pull_progress()
        if not running:
            self._timer.stop()

    @Slot()
    def _on_handle_changed(self) -> None:
        handle = self.sender()
        if isinstance(handle, JobHandle):
            self._on_changed(handle)

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
        self._release(removed)
        if removed:
            self.jobs_removed.emit()
        return removed

    @staticmethod
    def _release(handles: list[JobHandle]) -> None:
        """Make finished handles collectable.

        Every connection to a handle's signals is dropped (callers often
        connect closures that capture the handle — a cycle through C++ that
        Python's GC cannot see), the result is released, and the handle is
        detached from the manager so its C++ object goes with its last Python
        reference. (deleteLater on an object Python still wraps crashed
        PySide6 6.12 when the wrapper was later touched.)
        """
        signals = (
            "progress",
            "published",
            "succeeded",
            "failed",
            "cancelled",
            "finished",
            "changed",
        )
        for handle in handles:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                for name in signals:
                    try:
                        getattr(handle, name).disconnect()
                    except (RuntimeError, TypeError):
                        pass
            handle.result = None
            handle.setParent(None)

    def wait(self, timeout_ms: int = 5000) -> bool:
        """Block until every worker thread is idle (tests, self-test, exit)."""
        return self.pool.waitForDone(timeout_ms)

    def is_idle(self) -> bool:
        return self.running_count() == 0 and self.pool.activeThreadCount() == 0

    def dispose(self, timeout_ms: int = 5000) -> bool:
        """Cancel everything, wait for the threads, then delete the handles."""
        self.cancel_all()
        idle = self.wait(timeout_ms)
        self._timer.stop()
        if idle:
            self._release(self.handles)
            self.handles = []
        return idle

    # Backwards-compatible name used by tests and the self-test.
    shutdown = dispose
