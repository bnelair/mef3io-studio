"""Catch mef3io's warnings instead of letting them reach the log.

mef3io's warning texts start with the full path of the recording, which
identifies people and must never be logged. Every call into mef3io that may
warn runs inside :func:`library_warnings_caught`.

``warnings.catch_warnings`` swaps process-wide state and is not thread-safe:
two threads entering and leaving it out of order can leave the other's
filters in place (or lose them). The blocks are therefore serialised with
one lock. In the app they normally run in a helper process, one at a time;
the lock keeps it correct when a source runs on threads (``isolate=False``).

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
