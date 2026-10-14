"""Background jobs: every long operation runs on a worker thread, never on the
UI thread, with throttled progress, cancellation and plain-language errors."""

from .jobs import JobContext, JobHandle, JobManager

__all__ = ["JobContext", "JobHandle", "JobManager"]
