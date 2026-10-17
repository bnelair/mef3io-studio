"""Catch mef3io's warnings instead of letting them reach the log.

mef3io's warnings are about a recording that identifies people. Since
mef3io 1.4 their text says ``<session>`` and the path travels as data
(``MefWarning.path``), but the app still never lets them reach the log.
Every call into mef3io that may
warn runs inside :func:`library_warnings_caught`: ``inspect`` (Explorer and
summary), the legacy-password ``Reader`` open, ``Validator`` (check for
problems) and ``archive_session`` / ``extract_session`` (pack / unpack).
Deliberately left out: the self-test's ``mef3io.Writer`` (it writes a
synthetic recording in a temporary folder with no password, so nothing it
could warn about identifies anyone).

``warnings.catch_warnings`` swaps process-wide state and is not thread-safe:
two threads entering and leaving it out of order can leave the other's
filters in place (or lose them). EVERY ``catch_warnings`` in the app goes
through this module, under one lock (:func:`ignoring_warnings` for the job
layer's own). In the app the mef3io calls run in helper processes, so the
lock is never contended in the window's process; with a source that runs on
threads (``isolate=False``) a waiting block waits for the mef3io call.

Plain Python, no Qt.
"""

from __future__ import annotations

import contextlib
import threading
import warnings
from collections.abc import Iterator

_LOCK = threading.RLock()


@contextlib.contextmanager
def library_warnings_caught() -> Iterator[list]:
    """Record every warning raised inside the block (never shown, never
    logged); yields the list of caught ``warnings.WarningMessage``."""
    with _LOCK, warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield caught


@contextlib.contextmanager
def ignoring_warnings(category: type[Warning]) -> Iterator[None]:
    """Ignore warnings of ``category`` inside the block, under the same
    lock as :func:`library_warnings_caught`."""
    with _LOCK, warnings.catch_warnings():
        warnings.simplefilter("ignore", category)
        yield
