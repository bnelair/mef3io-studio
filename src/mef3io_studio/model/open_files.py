"""How many signal files the viewer's reader may keep open between reads.

mef3io 2.4 can keep up to ``open_files`` data files open (``Reader(open_files=N)``): on a
network drive every open is a round trip, and paging re-reads the same few files again and
again. The cost is descriptors held until ``release_files()`` / ``close()``, and on Windows a
folder cannot be renamed or removed while a file in it is held.

Only the viewer turns it on (the wizards and Explore read once and leave).

The number
    One read of a block touches the current segment file of every channel in it, so the files
    held while paging are about *channels x segments touched by the page and its look-ahead*.
    Almost always that is one segment per channel; a page that crosses a segment border touches
    two. So the want is ``2 x channels`` (capped, see below). The library says to keep it
    "well under a quarter of the process's descriptor limit", and the app holds descriptors of
    its own (Qt, logs, other windows, helper pipes), so the cap is an eighth of the limit, and
    never more than :data:`HARD_CAP`:

    * Linux: the soft limit (``RLIMIT_NOFILE``, mostly 1024) -> 128;
    * macOS: the soft limit of a GUI app is 256 -> 32;
    * Windows: the C runtime keeps 512 streams by default (``_setmaxstdio``; handles made with
      ``_open`` go to 8192 but a library that uses buffered streams meets the 512) -> 64.

    A recording with few channels simply gets a smaller number.

Letting go
    :func:`release` closes every kept file; it is called when the viewer closes, before the
    app writes into a recording (saving or removing notes) and before every job the app starts
    (``workers.jobs.JobManager.prologue``), on the job's own thread. A viewer that has not read
    for :data:`IDLE_RELEASE` seconds lets go too (``ViewController``), so a recording that is
    left open in a window does not stay locked on Windows.
"""

from __future__ import annotations

import sys

#: Never more files than this, whatever the limit says.
HARD_CAP = 128
#: The share of the process's descriptor limit the viewer may hold: one eighth.
LIMIT_DIVISOR = 8
#: Windows' C runtime stream limit (its default; a library may meet it).
WINDOWS_LIMIT = 512
#: A viewer that has not read for this long lets its files go (seconds).
IDLE_RELEASE = 8.0
#: Files wanted per channel: the current segment and the next one (a page across a border).
PER_CHANNEL = 2


def descriptor_limit(platform: str | None = None, soft_limit: int | None = None) -> int:
    """The number of files this process may have open, as far as the viewer cares.
    ``platform`` and ``soft_limit`` are for tests."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        return WINDOWS_LIMIT
    if soft_limit is not None:
        return soft_limit
    try:
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft == resource.RLIM_INFINITY or soft < 0:
            return 1024
        return int(soft)
    except Exception:  # noqa: BLE001 - unknown: the common default
        return 1024


def cap(platform: str | None = None, soft_limit: int | None = None) -> int:
    """The most files the viewer may keep open here."""
    return max(0, min(HARD_CAP, descriptor_limit(platform, soft_limit) // LIMIT_DIVISOR))


def release_all(windows) -> int:
    """Let go of the kept files of every viewer window in ``windows`` (anything with
    ``.data.release_files()``); returns how many were released. One that fails is skipped (the
    job that follows must not depend on it). Call it on a worker thread, never the GUI's."""
    done = 0
    for window in list(windows):
        try:
            window.data.release_files()
            done += 1
        except Exception:  # noqa: BLE001 - a closed or broken viewer holds nothing we can free
            continue
    return done


def budget(channels: int, platform: str | None = None, soft_limit: int | None = None) -> int:
    """``open_files`` for a viewer whose recordings hold ``channels`` channels in total: the
    files a page and its look-ahead touch (:data:`PER_CHANNEL` per channel), within :func:`cap`.
    0 (keep nothing open) when there is no channel or the limit leaves no room."""
    if channels <= 0:
        return 0
    return min(PER_CHANNEL * int(channels), cap(platform, soft_limit))
