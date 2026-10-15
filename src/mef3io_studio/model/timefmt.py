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
        # "256 / 512 Hz"; units are written once per run of the same unit:
        # "512 Hz / 8 kHz".
        words = [format_rate(r).split(" ") for r in unique]
        out: list[str] = []
        for i, (number, unit) in enumerate(words):
            last = i == len(words) - 1 or words[i + 1][1] != unit
            out.append(f"{number} {unit}" if last else number)
        return " / ".join(out)
    return f"{format_rate(unique[0])} to {format_rate(unique[-1])} ({len(unique)} different)"


def format_count(n: int, one: str, many: str | None = None) -> str:
    """ "1 channel", "3 channels"."""
    return f"{n} {one if n == 1 else (many or one + 's')}"


#: Steps between the marks on a time axis, in microseconds.
_TICK_STEPS = [
    int(x * 1_000_000)
    for x in (
        0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
        1, 2, 5, 10, 15, 30,
        60, 120, 300, 600, 900, 1800,
        3600, 7200, 10800, 21600, 43200,
        86400, 2 * 86400, 7 * 86400, 14 * 86400, 28 * 86400, 91 * 86400,
        182 * 86400, 364 * 86400, 728 * 86400, 1820 * 86400, 3640 * 86400,
    )
]  # fmt: skip
_DAY = 86_400_000_000


def tick_step(span_us: int, max_ticks: int) -> int:
    """The smallest step that puts at most ``max_ticks`` marks on the axis."""
    max_ticks = max(1, max_ticks)
    for step in _TICK_STEPS:
        if span_us / step <= max_ticks:
            return step
    return _TICK_STEPS[-1] * max(1, int(span_us / (_TICK_STEPS[-1] * max_ticks)) + 1)


def axis_ticks(v0: int, v1: int, step: int, zone: tzinfo = UTC) -> list[int]:
    """Marks every ``step`` between ``v0`` and ``v1``, on round wall-clock
    times in ``zone`` (whole hours, local midnight, ...). Computed per mark
    from the time-zone database, so a daylight-saving change is honoured."""
    if v1 <= v0 or step <= 0:
        return []
    try:
        local = to_datetime(v0, zone)
    except (OverflowError, ValueError, OSError):
        return []
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    wall = local.replace(tzinfo=None)
    if step >= _DAY:
        days = step // _DAY
        ordinal = midnight.toordinal()
        start = datetime.fromordinal(ordinal - ordinal % days)
        delta = timedelta(days=days)
    else:
        since = (wall - midnight) // timedelta(microseconds=1)
        start = midnight + timedelta(microseconds=since - since % step)
        delta = timedelta(microseconds=step)
    out: list[int] = []
    current = start
    for _ in range(1000):
        try:
            # The repeated hour when clocks go back is two instants: both
            # get a mark (fold=1 is the second one).
            moments = sorted(
                {
                    (current.replace(tzinfo=zone, fold=f) - _EPOCH) // timedelta(microseconds=1)
                    for f in (0, 1)
                }
            )
        except (OverflowError, ValueError):
            break
        if moments[0] > v1:
            break
        for us in moments:
            if v0 <= us <= v1 and (not out or us > out[-1]):
                out.append(us)
        current = current + delta
    return out


def tick_label(us: int, step: int, zone: tzinfo = UTC, with_date: bool = False) -> str:
    """A short label for a mark: the date for steps of a day or more, else
    the time of day (with the date where ``with_date``)."""
    try:
        moment = to_datetime(us, zone)
    except (OverflowError, ValueError, OSError):
        return ""
    if step >= _DAY:
        return moment.strftime("%Y-%m-%d")
    if step >= 60_000_000:
        text = moment.strftime("%H:%M")
    elif step >= 1_000_000:
        text = moment.strftime("%H:%M:%S")
    else:
        text = moment.strftime("%H:%M:%S.") + f"{moment.microsecond // 1000:03d}"
    return f"{moment.strftime('%Y-%m-%d')} {text}" if with_date else text
