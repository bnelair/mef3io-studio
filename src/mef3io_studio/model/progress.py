"""Progress bookkeeping: throttling and time-remaining estimates.

Kept free of Qt so the rules (at most N updates a second, when an ETA is
meaningful) are tested directly.
"""

from __future__ import annotations

import math
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


class ProgressThrottle:
    """Lets through at most one update per ``min_interval`` seconds (default
    0.1 s, i.e. 10 updates a second), so a job reporting per block can never
    flood the UI event loop.

    Always let through: the first update, a phase change, and completion
    (``done >= total``) — those are the updates a user would notice missing.
    """

    def __init__(self, min_interval: float = 0.1, clock: Callable[[], float] = time.monotonic):
        self.min_interval = min_interval
        self._clock = clock
        self._last_time: float | None = None
        self._last_phase: str | None = None

    def should_emit(self, progress: Progress) -> bool:
        now = self._clock()
        finished = progress.determinate and progress.done >= progress.total
        if (
            self._last_time is None
            or progress.phase != self._last_phase
            or finished
            or now - self._last_time >= self.min_interval
        ):
            self._last_time = now
            self._last_phase = progress.phase
            return True
        return False


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
