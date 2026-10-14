"""Cooperative cancellation shared by the UI thread and a worker."""

from __future__ import annotations

import threading


class JobCancelled(Exception):
    """Raised inside a job when the user cancelled it. Not an error: the job
    layer reports it as "Cancelled", never as a failure."""


class CancelToken:
    """A thread-safe flag. The UI calls :meth:`cancel`; the job polls
    :attr:`cancelled` (or calls :meth:`raise_if_cancelled`) between steps.

    mef3io long operations will accept this kind of token once mef3io#69
    lands; until then jobs check it between their own steps.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise JobCancelled()
