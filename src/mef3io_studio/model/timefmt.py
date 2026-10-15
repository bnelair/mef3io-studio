"""How times, durations, sizes and rates are written on screen.

Recording times arrive as microseconds since 1970 in UTC (what mef3io
returns). They are shown in the **site time zone** the user chose, computed
per instant from the IANA database (``zoneinfo``), so a daylight-saving
change is handled correctly on both sides of it. Until a zone is chosen,
times are shown in UTC and labelled as UTC. The file's own fixed offset
(``GMT_offset``) is never used: it is one number, and meflib ignores the
daylight-saving fields (docs/design/explorer.md, "How times are shown").

Plain Python, no Qt.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

#: Shown and stored when no site time zone has been chosen.
UTC_NAME = "UTC"

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def zone_names() -> list[str]:
    """Every IANA time-zone name this installation knows, sorted, plus UTC."""
    try:
        names = available_timezones()
    except Exception:  # pragma: no cover - a broken tz database
        names = set()
    names.discard("localtime")
    return [UTC_NAME, *sorted(n for n in names if n != UTC_NAME)]


def resolve_zone(name: str | None) -> tzinfo:
    """The zone called ``name``; UTC when it is empty or unknown."""
    if not name or name == UTC_NAME:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def zone_label(zone: tzinfo) -> str:
    """ "UTC" or the IANA name, e.g. "Europe/Prague"."""
    if zone is UTC:
        return UTC_NAME
    return getattr(zone, "key", None) or str(zone)


def to_datetime(us: int, zone: tzinfo = UTC) -> datetime:
    """Exact (integer arithmetic): microseconds since 1970, in ``zone``."""
    return (_EPOCH + timedelta(microseconds=int(us))).astimezone(zone)


def format_time(us: int | None, zone: tzinfo = UTC, seconds_decimals: int = 0) -> str:
    """ "2000-01-01 01:00:00 +01:00" in a site zone, "2000-01-01 00:00:00 UTC"
    in UTC. The offset is always shown, so the reader never has to guess."""
    if us is None:
        return ""
    try:
        moment = to_datetime(us, zone)
    except (OverflowError, ValueError, OSError):
        return "not a valid time"
    text = moment.strftime("%Y-%m-%d %H:%M:%S")
    if seconds_decimals > 0:
        fraction = f"{moment.microsecond / 1e6:.{seconds_decimals}f}"[1:]
        text += fraction
    if zone is UTC:
        return f"{text} UTC"
    offset = moment.strftime("%z")  # +0100
    return f"{text} {offset[:3]}:{offset[3:]}"


def format_span(seconds: float | None) -> str:
    """A length of time in everyday units: "45 s", "12 min 05 s",
    "3 h 02 min", "2 days 4 h"."""
    if seconds is None or math.isnan(seconds):
        return ""
    if seconds < 0:
        seconds = 0.0
    if seconds < 10 and seconds != int(seconds):
        return f"{seconds:.2f} s".replace(".00 s", " s")
    total = int(round(seconds))
    days, rest = divmod(total, 86_400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} day{'s' if days != 1 else ''} {hours} h"
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


def format_size(n_bytes: int | None) -> str:
    """Decimal units, as file managers show them: "41.4 MB"."""
    if n_bytes is None:
        return ""
    value = float(n_bytes)
    for unit in ("bytes", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            if unit == "bytes":
                return f"{int(value)} bytes"
            return f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} PB"  # pragma: no cover - unreachable


def format_rate(hz: float) -> str:
    """ "256 Hz", "0.5 Hz", "2.5 kHz"."""
    if hz >= 1000 and hz % 100 == 0:
        return f"{hz / 1000:g} kHz"
    return f"{hz:g} Hz"


def format_rates(rates: list[float] | tuple[float, ...]) -> str:
    """One rate, a short list, or a range when there are many."""
    unique = sorted(set(rates))
    if not unique:
        return ""
    if len(unique) <= 3:
        return ", ".join(format_rate(r) for r in unique)
    return f"{format_rate(unique[0])} to {format_rate(unique[-1])} ({len(unique)} different)"


def format_count(n: int, one: str, many: str | None = None) -> str:
    """ "1 channel", "3 channels"."""
    return f"{n} {one if n == 1 else (many or one + 's')}"
