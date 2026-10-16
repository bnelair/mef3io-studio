"""Cooperative cancellation shared by the UI thread, a worker and mef3io.

The app has one stop flag per task (:class:`CancelToken`). mef3io 1.3 has its
own (``mef3io.CancelToken``, checked inside the library between units of
work: a segment, a 1 MB copy chunk); :meth:`CancelToken.for_library` hands
out one that is linked to ours, so Stop reaches inside a long mef3io call.

A stop is never an error. Inside the app it is :class:`JobCancelled`;
mef3io raises ``mef3io.Cancelled`` (deliberately not a ``MefError``).
:func:`is_stop` recognises both, and :func:`stopped_by_library` turns the
library's into ours at the boundary, so the rest of the app sees one kind.

Plain Python, no Qt. mef3io is imported only when a library token is asked
for, or an exception has to be classified.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator


class JobCancelled(Exception):
    """Raised inside a job when the user cancelled it. Not an error: the job
    layer reports it as "Stopped", never as a failure."""


class CancelToken:
    """A thread-safe flag. The UI calls :meth:`cancel`; the job polls
    :attr:`cancelled` (or calls :meth:`raise_if_cancelled`) between steps,
    and mef3io polls the linked :meth:`for_library` token inside its calls.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], object]] = []
        self._library = None

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            callbacks, self._callbacks = self._callbacks, []
        for fn in callbacks:
            with contextlib.suppress(Exception):
                fn()

    def on_cancel(self, fn: Callable[[], object]) -> None:
        """Call ``fn()`` when this token is cancelled (at once if it already
        is). ``fn`` runs on the thread that cancels, so it must be quick."""
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(fn)
                return
        fn()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise JobCancelled()

    def for_library(self):
        """A ``mef3io.CancelToken`` that is cancelled together with this one.
        One per token, shared by every call it is passed to (mef3io allows
        that, and a token is one-shot like ours)."""
        import mef3io

        with self._lock:
            library = self._library
            created = library is None
            if created:
                library = self._library = mef3io.CancelToken()
        if created:
            self.on_cancel(library.cancel)
        return library


def is_stop(exc: BaseException) -> bool:
    """True when ``exc`` means "stopped on request", not "went wrong"."""
    if isinstance(exc, JobCancelled):
        return True
    try:
        import mef3io
    except Exception:  # pragma: no cover - the app always bundles mef3io
        return False
    cancelled = getattr(mef3io, "Cancelled", None)
    return isinstance(cancelled, type) and isinstance(exc, cancelled)


@contextlib.contextmanager
def stopped_by_library() -> Iterator[None]:
    """Turn ``mef3io.Cancelled`` raised inside the block into
    :class:`JobCancelled`, so the rest of the app has one kind of stop."""
    try:
        yield
    except JobCancelled:
        raise
    except Exception as exc:
        if is_stop(exc):
            raise JobCancelled() from None
        raise
