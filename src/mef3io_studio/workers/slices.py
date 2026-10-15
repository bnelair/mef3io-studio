"""Work for the window's own drawing, done in short slices on the GUI
thread (e.g. laying out the combined timeline after a selection change).

Why not a background thread: this work is pure Python, and while a Python
thread computes, the GUI thread waits for the interpreter lock every time
it crosses into Qt and back, which a paint does hundreds of times. Measured
with 400 recordings: stalls of half a second. Done in slices of a few
milliseconds between other events instead, the window never waits longer
than one slice.

Not a task in the Activity panel either: it is short, transient and
replaced on every click, so listing it would only add noise, and there is
nothing for the user to stop. Anything that reads or writes files goes
through :class:`~.jobs.JobManager`.

The work is a generator that ``yield``s every few milliseconds of work and
finally ``return``s its result. Only the newest request runs: submitting
another one drops the one in progress.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator

from PySide6.QtCore import QObject, QTimer, Signal

log = logging.getLogger(__name__)

#: How long one slice may run before the window gets its turn again.
SLICE_SECONDS = 0.008


class SlicedRunner(QObject):
    done = Signal(object)  # the generator's return value
    failed = Signal()

    def __init__(self, parent: QObject | None = None, slice_seconds: float = SLICE_SECONDS):
        super().__init__(parent)
        self.slice_seconds = slice_seconds
        self._steps: Generator | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(1)  # leave room for paint and input events
        self._timer.timeout.connect(self._slice)

    @property
    def busy(self) -> bool:
        return self._steps is not None

    def submit(self, steps: Generator) -> None:
        self.cancel()
        self._steps = steps
        self._timer.start()

    def cancel(self) -> None:
        self._timer.stop()
        if self._steps is not None:
            self._steps.close()
            self._steps = None

    def _slice(self) -> None:
        steps = self._steps
        if steps is None:
            self._timer.stop()
            return
        deadline = time.perf_counter() + self.slice_seconds
        try:
            while time.perf_counter() < deadline:
                next(steps)
        except StopIteration as finished:
            self._timer.stop()
            self._steps = None
            self.done.emit(finished.value)
        except Exception as exc:  # drawing work must never take the app down
            self._timer.stop()
            self._steps = None
            log.warning("a layout step failed with %s", type(exc).__name__)
            self.failed.emit()
