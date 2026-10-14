"""Background jobs: every long operation runs off the UI thread, with
throttled progress, cancellation and plain-language errors.

Nothing is imported eagerly: helper processes import
``mef3io_studio.workers.isolated`` and must not pull in Qt.
"""

__all__ = ["JobContext", "JobHandle", "JobManager"]


def __getattr__(name):
    if name in __all__:
        from . import jobs

        return getattr(jobs, name)
    raise AttributeError(name)
