"""The jump grammar (docs/design/viewer.md, "Navigation"), the clinical one.

Plain Python, no Qt. Times are microseconds since 1970 (UTC); wall-clock
words are read in the site time zone.

* ``HH:MM[:SS]``: the next occurrence of that time of day after the current
  page (``previous=True``: the previous one);
* ``day 2 08:00``: the 2nd calendar day of the recording (day 1 = the day
  it starts), at that time;
* ``YYYY-MM-DD HH:MM[:SS]``;
* ``+10m``, ``-90s``, ``+2h``, ``+1d``: relative to the current page;
* ``A``, ``B``...: the start of that recording.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta, tzinfo

from ..model.timefmt import from_local, to_datetime

US = 1_000_000
HELP_TEXT = (
    "You can type a time of day (14:30), a day and time (day 2 08:00), a date and time "
    "(2000-01-31 14:30), a move such as +10m, -90s or +2h, or a recording letter (B) "
    "to go to its start. Press Shift+Enter for the previous occurrence of a time of day."
)


class JumpError(ValueError):
    """The text could not be used. ``args[0]`` is a plain-language sentence."""


_TIME = r"(\d{1,2}):(\d{2})(?::(\d{2}(?:\.\d+)?))?"
_RE_TIME = re.compile(rf"^{_TIME}$")
_RE_DAY = re.compile(rf"^day\s+(\d+)\s+{_TIME}$", re.IGNORECASE)
_RE_DATE = re.compile(rf"^(\d{{4}})-(\d{{2}})-(\d{{2}})[ T]{_TIME}$")
_RE_REL = re.compile(r"^([+-])\s*(\d+(?:\.\d+)?)\s*([smhd])$", re.IGNORECASE)
_RE_LETTER = re.compile(r"^[A-Za-z]{1,3}$")
_UNIT = {"s": US, "m": 60 * US, "h": 3600 * US, "d": 86_400 * US}


def _clock(h: str, m: str, s: str | None) -> time:
    try:
        sec = float(s) if s else 0.0
        return time(int(h), int(m), int(sec), int(round((sec % 1) * 1e6)))
    except ValueError:
        raise JumpError(
            "That is not a time of day. Use hours 0 to 23 and minutes 0 to 59."
        ) from None


def _local_us(day: datetime, at: time, zone: tzinfo) -> int:
    return from_local(datetime.combine(day.date(), at), zone)


def parse(
    text: str,
    *,
    current_us: int,
    timeline: tuple[int, int],
    zone: tzinfo = UTC,
    letters: Mapping[str, int] | None = None,
    previous: bool = False,
) -> int:
    """The time (µs) the text points to; raises :class:`JumpError`.

    The result is not clamped to the recordings, except that a point outside
    the whole timeline is refused with an explanation."""
    s = text.strip()
    if not s:
        raise JumpError("Type a time to go to. " + HELP_TEXT)
    start, end = timeline
    target: int
    if (m := _RE_REL.match(s)) is not None:
        amount = float(m.group(2)) * _UNIT[m.group(3).lower()]
        target = int(current_us + (amount if m.group(1) == "+" else -amount))
    elif (m := _RE_TIME.match(s)) is not None:
        at = _clock(*m.groups())
        here = to_datetime(current_us, zone)
        day = here
        step = timedelta(days=-1 if previous else 1)
        # today's occurrence first, then onward day by day (a few days only
        # matter: a time of day recurs daily)
        found = None
        for _ in range(4):
            cand = _local_us(day, at, zone)
            if (cand < current_us) if previous else (cand > current_us):
                found = cand
                break
            day = day + step
        if found is None:
            raise JumpError("That time of day could not be found.")
        target = found
    elif (m := _RE_DAY.match(s)) is not None:
        number = int(m.group(1))
        if number < 1:
            raise JumpError("Days are counted from 1: day 1 is the day the recording starts.")
        first = to_datetime(start, zone)
        target = _local_us(first + timedelta(days=number - 1), _clock(*m.groups()[1:]), zone)
    elif (m := _RE_DATE.match(s)) is not None:
        try:
            day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            raise JumpError("That date does not exist.") from None
        target = _local_us(day, _clock(*m.groups()[3:]), zone)
    elif _RE_LETTER.match(s) and letters is not None and s.upper() in letters:
        return int(letters[s.upper()])
    elif _RE_LETTER.match(s):
        raise JumpError("There is no recording with that letter.")
    else:
        raise JumpError("That could not be understood. " + HELP_TEXT)
    if target < start or target > end:
        raise JumpError("That time is outside the recordings.")
    return target
