"""What the activity panel shows about each background job. Plain data."""

from __future__ import annotations

import enum
import itertools
from dataclasses import dataclass, field

from .progress import Progress

_ids = itertools.count(1)


class JobStatus(enum.Enum):
    QUEUED = "Waiting"
    RUNNING = "Running"
    SUCCEEDED = "Done"
    FAILED = "Failed"
    CANCELLED = "Stopped"

    @property
    def finished(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


@dataclass
class JobRecord:
    """The UI-facing state of one job. ``title`` and ``result_text`` are shown
    to the user and are plain language; they must not carry patient details
    (titles are written by the app, e.g. "Checking 12 recordings")."""

    title: str
    id: int = field(default_factory=lambda: next(_ids))
    status: JobStatus = JobStatus.QUEUED
    progress: Progress = field(default_factory=Progress)
    eta_seconds: float | None = None
    result_text: str = ""
    cancel_requested: bool = False
