"""A recording's first and last time, cheaply, and ordering by it.

One shared helper over ``mef3io.time_range`` (only the first and last part's
small details file of each channel is read; never a block table or signal),
used by Join or trim (:mod:`.join`) and by Rename (:mod:`.rename_ops`).

``verified`` is mef3io's ``offset_known``: False when the times cannot be
shown as absolute (a password-protected recording without its full-access
password, or an older way of hiding the date). Such a recording is NOT put
into the absolute order: :func:`order_by_time` returns it separately, so a
guessed time can never place it between two real ones.

Plain Python, no Qt. The mef3io call may run in a helper process.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .cancel import CancelToken, is_stop


@dataclass(frozen=True)
class RecordingTime:
    """First and last time of a recording (microseconds since 1970, UTC).
    ``start``/``end`` are None when no channel holds samples. The end is the
    one the details state (see ``join.read_layouts`` for the exact end)."""

    start: int | None = field(default=None, repr=False)
    end: int | None = field(default=None, repr=False)
    verified: bool = False
    channels: int = 0


def read_time(location: str, password: str = "", cancel: CancelToken | None = None):
    """The recording's :class:`RecordingTime`. A wrong password raises
    mef3io's ``PasswordError``; a stop raises ``mef3io.Cancelled``."""
    import mef3io

    kwargs = {"cancel": cancel.for_library()} if cancel is not None else {}
    found = mef3io.time_range(location, password, **kwargs)
    return RecordingTime(
        None if found.start_uutc is None else int(found.start_uutc),
        None if found.end_uutc is None else int(found.end_uutc),
        bool(found.offset_known),
        int(found.channels),
    )


def read_times(
    requests: Iterable[tuple[str, str, str]],
    cancel: CancelToken | None = None,
    send: Callable[[tuple[int, int]], object] | None = None,
    reader: Callable[..., RecordingTime] = read_time,
) -> dict[str, RecordingTime | Exception]:
    """``{key: RecordingTime}`` for ``(key, location, password)`` requests.
    A recording that cannot be read maps to its exception (the rest goes
    on). ``send`` gets ``(done, total)`` after each."""
    items = list(requests)
    out: dict[str, RecordingTime | Exception] = {}
    for n, (key, location, password) in enumerate(items):
        if cancel is not None:
            cancel.raise_if_cancelled()
        try:
            out[key] = reader(location, password, cancel)
        except Exception as exc:  # noqa: BLE001 - reported per recording
            if is_stop(exc):
                raise
            out[key] = exc
        if send is not None:
            send((n + 1, len(items)))
    return out


def order_by_time(times: dict[str, RecordingTime | None]) -> tuple[list[str], list[str]]:
    """``(ordered, unverified)`` keys. ``ordered`` holds the recordings with a
    verified start, by start (then end, then key, so ties are stable);
    ``unverified`` those whose times are not confirmed or unknown, in the
    order given, kept out of the absolute order."""
    ordered, unverified = [], []
    for key, t in times.items():
        if t is not None and t.verified and t.start is not None:
            ordered.append(key)
        else:
            unverified.append(key)
    ordered.sort(key=lambda k: (times[k].start, times[k].end or 0, k))
    return ordered, unverified
