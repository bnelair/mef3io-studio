"""Progress bookkeeping: the latest-report slot and time-remaining estimates.

Kept free of Qt so the rules (only the newest report matters, when an ETA is
meaningful) are tested directly.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Progress:
    """One progress report. ``total`` is ``None`` when the amount of work is
    unknown (the UI then shows an indeterminate bar)."""

    done: float = 0
    total: float | None = None
    phase: str = ""

    @property
    def determinate(self) -> bool:
        return self.total is not None and self.total > 0

    @property
    def fraction(self) -> float | None:
        if not self.determinate:
            return None
        return min(1.0, max(0.0, self.done / self.total))


class ProgressSlot:
    """Holds a job's latest progress report, written by the worker thread and
    read by the UI thread on its own timer.

    This is how progress is throttled: the worker may report a million times a
    second (including alternating phases) and it only overwrites one slot;
    the UI picks up the newest report at most :data:`UI_UPDATES_PER_SECOND`
    times a second. Nothing is queued, so nothing can flood the event loop,
    and the latest report is never lost — :meth:`take` after the job ends
    always returns the final one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: Progress | None = None
        self._version = 0
        self._taken = 0

    def put(self, progress: Progress) -> None:
        with self._lock:
            self._latest = progress
            self._version += 1

    def take(self) -> Progress | None:
        """The newest report if it changed since the last take, else None."""
        with self._lock:
            if self._version == self._taken:
                return None
            self._taken = self._version
            return self._latest

    @property
    def latest(self) -> Progress | None:
        with self._lock:
            return self._latest


#: How often the UI pulls progress from running jobs.
UI_UPDATES_PER_SECOND = 10


class EtaEstimator:
    """Estimates time remaining from the average rate since the job started.

    Returns ``None`` (show no ETA) until it is meaningful: the work must be
    determinate, at least ``min_elapsed`` seconds in, and past ``min_fraction``
    of the way — earlier estimates jump around and mislead more than help.
    """

    def __init__(
        self,
        min_elapsed: float = 2.0,
        min_fraction: float = 0.02,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.min_elapsed = min_elapsed
        self.min_fraction = min_fraction
        self._clock = clock
        self._start = clock()

    def remaining(self, progress: Progress) -> float | None:
        fraction = progress.fraction
        if fraction is None or fraction < self.min_fraction or fraction >= 1.0:
            return None
        elapsed = self._clock() - self._start
        if elapsed < self.min_elapsed:
            return None
        return elapsed * (1.0 - fraction) / fraction


def format_duration(seconds: float | None) -> str:
    """Plain-language duration: "about 3 minutes", "less than a minute"."""
    if seconds is None or math.isnan(seconds):
        return ""
    if seconds < 60:
        return "less than a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours = seconds / 3600
    if hours < 10:
        return f"about {hours:.1f} hours"
    return f"about {round(hours)} hours"
