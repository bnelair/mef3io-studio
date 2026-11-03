"""Cut into pieces: plan how ONE recording is cut, as plain data (#31, 2).

The person chooses where the cuts go: every N hours or days (optionally at
midnight in the site time zone, so "every 1 day" is one piece per calendar
day, and a day with a clock change is 23 or 25 hours long), or at times
typed one by one. This module turns that choice into the list of pieces to
show (start, end, length, about how big), leaves out pieces that hold no
data and says how many, names them from a pattern, and builds one
:class:`~.changes.ChangeRequest` per piece. Each request is then one item of
the ONE batch runner (``model.pipeline`` + ``workers.tasks.run_batch``), so
Stop, resume after a crash, the up-front checks and the verification of every
piece against its original are the batch's own; mef3io's cut
(``transform_session(time_range=, exact=True)``) is called only in
``model.changes``.

Where the data is comes from what the Explorer already holds for the
recording (``model.coverage``: the stretches with data); nothing is read from
the files here. Pieces are ``[start, end)``: a sample at a cut time belongs
to the piece that starts there, so the pieces together hold every sample of
the recording once.

Plain Python, no Qt. Nothing here touches a file or logs anything (names
and times identify).
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, tzinfo

from .anonymize import destination_for, estimate_bytes
from .changes import ChangeRequest
from .makenew import Source
from .timefmt import from_local, to_datetime

SECOND_US = 1_000_000
HOUR_US = 3600 * SECOND_US

#: How the cuts are chosen.
EVERY = "every"  # every N hours or days
AT = "at"  # at the times the person gives
MODES = (EVERY, AT)

HOURS = "hours"
DAYS = "days"
UNITS = (HOURS, DAYS)

#: More pieces than this is a typing mistake, not a plan.
MAX_PIECES = 2000
#: The last piece's window reaches this far past the last stretch with data,
#: so a writer that states the end one sample early never loses the last
#: sample (a window past the data keeps nothing more).
END_MARGIN_US = SECOND_US

#: What a name pattern may contain.
TOKEN_NAME = "<name>"
TOKEN_NUMBER = "<number>"
TOKEN_DATE = "<date>"
DEFAULT_PATTERN = "<name>_<number>"
DAY_PATTERN = "<name>_day<number>"


@dataclass(frozen=True)
class Choice:
    """Where to cut. ``every`` + ``unit``: every that many hours or days;
    ``aligned``: from midnight in the site time zone (the first piece is then
    the rest of the first day). ``at``: the cut times (microseconds since
    1970, UTC)."""

    mode: str = EVERY
    every: int = 1
    unit: str = DAYS
    aligned: bool = True
    at: tuple[int, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class Piece:
    """One piece of the plan. ``start``/``end`` are what is shown (the end
    is exclusive); ``window`` is what mef3io is given."""

    #: 1, 2, 3 ... among the pieces that are made.
    number: int
    start: int = field(repr=False)
    end: int = field(repr=False)
    window: tuple[int, int] = field(repr=False)
    #: Time inside it that has data, and about how many bytes it takes.
    data_us: int = field(default=0, repr=False)
    bytes: int = field(default=0, repr=False)

    @property
    def seconds(self) -> float:
        return max(0, self.end - self.start) / SECOND_US


@dataclass(frozen=True)
class PiecePlan:
    pieces: tuple[Piece, ...] = ()
    #: Pieces that would hold no data, so they are not made.
    left_out: int = 0
    #: "" or why there is nothing to make (plain words).
    problem: str = ""


# --------------------------------------------------------------------------
# Where the cuts go

TOO_MANY = f"That would make more than {MAX_PIECES} pieces. Choose a longer length."
NO_TIMES = "The times of this recording are not confirmed, so it cannot be cut by time."
NOT_POSITIVE = "Choose a length of at least 1."
NO_CUTS = "Give at least one time inside the recording to cut at."
ONE_PIECE = "That would give a single piece: the whole recording. Choose a shorter length."


def _midnight(day, zone: tzinfo) -> int:
    """The first instant of ``day`` in ``zone``. Where clocks jump over
    midnight (some zones change at 00:00) that is the first instant that
    exists, which ``from_local`` (fold 0) gives: the instant of the jump."""
    t = from_local(datetime(day.year, day.month, day.day), zone)
    # Never earlier than the day, whatever the zone database does.
    if to_datetime(t, zone).date() < day:
        t += HOUR_US
    return t


def cut_times(first: int, last: int, choice: Choice, zone: tzinfo = UTC) -> list[int] | str:
    """The cut times strictly inside ``(first, last)``, sorted, each once; or
    a plain-words problem. ``first``/``last``: the recording's first and last
    time."""
    if choice.mode == AT:
        cuts = sorted({int(t) for t in choice.at if first < int(t) < last})
        return cuts if cuts else NO_CUTS
    n = int(choice.every)
    if n < 1:
        return NOT_POSITIVE
    cuts: list[int] = []
    if choice.aligned and choice.unit == DAYS:
        day = to_datetime(first, zone).date()
        k = 1
        while True:
            t = _midnight(day + timedelta(days=n * k), zone)
            if t >= last:
                break
            if t > first:
                cuts.append(t)
            k += 1
            if k > MAX_PIECES:
                return TOO_MANY
    else:
        step = n * (24 * HOUR_US if choice.unit == DAYS else HOUR_US)
        if choice.aligned:
            anchor = _midnight(to_datetime(first, zone).date(), zone)
            k = (first - anchor) // step + 1
            t = anchor + k * step
        else:
            t = first + step
        while t < last:
            cuts.append(t)
            t += step
            if len(cuts) > MAX_PIECES:
                return TOO_MANY
    return cuts if cuts else ONE_PIECE


# --------------------------------------------------------------------------
# The pieces


class Stretches:
    """The stretches with data, sorted and not overlapping, with prefix sums
    so a piece is looked up in O(log n) instead of scanned (a multi-day
    recording has thousands of stretches and a plan has hundreds of pieces).
    A stretch ``[s, e]`` includes its last sample's time (a writer may state
    the end as that time), and ``s == e`` is a single sample."""

    def __init__(self, runs: Sequence[tuple[int, int]]):
        merged: list[list[int]] = []
        for s, e in sorted((int(s), max(int(s), int(e))) for s, e in runs):
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        self.starts = [s for s, _ in merged]
        self.ends = [e for _, e in merged]
        self.before = [0]
        for s, e in merged:
            self.before.append(self.before[-1] + (e - s))

    def __bool__(self) -> bool:
        return bool(self.starts)

    @property
    def first(self) -> int:
        return self.starts[0]

    @property
    def last(self) -> int:
        return self.ends[-1]

    def holds_data(self, a: int, b: int) -> bool:
        """Any stretch with data inside ``[a, b)``."""
        i = bisect_left(self.ends, a)  # the first stretch ending at or after a
        return i < len(self.starts) and self.starts[i] < b

    def _upto(self, x: int) -> int:
        i = bisect_right(self.starts, x) - 1
        if i < 0:
            return 0
        return self.before[i] + min(x, self.ends[i]) - self.starts[i]

    def data_inside(self, a: int, b: int) -> int:
        return max(0, self._upto(b) - self._upto(a))


def plan_pieces(
    runs: Sequence[tuple[int, int]] | Stretches,
    channel_bytes: int,
    choice: Choice,
    zone: tzinfo = UTC,
    *,
    verified: bool = True,
) -> PiecePlan:
    """The pieces for a recording whose data lies in ``runs`` (its stretches
    with data, ``[start, end]`` in microseconds) and whose signal takes
    ``channel_bytes`` in all. Pieces with no data are left out and counted.
    Pure: nothing is read."""
    if not verified:
        return PiecePlan(problem=NO_TIMES)
    stretches = runs if isinstance(runs, Stretches) else Stretches(runs)
    if not stretches:
        return PiecePlan(problem="This recording has no data to cut.")
    first, last = stretches.first, stretches.last
    cuts = cut_times(first, last, choice, zone)
    if isinstance(cuts, str):
        return PiecePlan(problem=cuts)
    edges = [first, *cuts, last]
    total_data = stretches.data_inside(first, last) or 1
    pieces: list[Piece] = []
    left_out = 0
    for i, (a, b) in enumerate(zip(edges, edges[1:], strict=False)):
        # The last piece owns the last sample too; the first starts a second
        # early for the same reason (nothing earlier exists).
        last_piece = i == len(edges) - 2
        stop = b + END_MARGIN_US if last_piece else b
        window = (a - END_MARGIN_US if i == 0 else a, stop)
        if not stretches.holds_data(a, stop):
            left_out += 1
            continue
        data = stretches.data_inside(a, stop)
        share = data / total_data
        pieces.append(
            Piece(
                len(pieces) + 1,
                a,
                b,
                window,
                data,
                estimate_bytes({"all": int(channel_bytes * share)}, ("all",)),
            )
        )
    if len(pieces) < 2 and not left_out:
        return PiecePlan(problem=ONE_PIECE)
    if not pieces:
        return PiecePlan(left_out=left_out, problem="No piece would hold any data.")
    return PiecePlan(tuple(pieces), left_out)


def describe_left_out(left_out: int) -> str:
    if not left_out:
        return ""
    if left_out == 1:
        return "1 piece would hold no data (it falls in a gap), so it is left out."
    return f"{left_out} pieces would hold no data (they fall in gaps), so they are left out."


# --------------------------------------------------------------------------
# Names


def pattern_problem(pattern: str) -> str:
    """ "" or why the pattern cannot be used."""
    if TOKEN_NUMBER not in pattern and TOKEN_DATE not in pattern:
        return f"Put {TOKEN_NUMBER} or {TOKEN_DATE} in the name so every piece gets its own."
    return ""


def piece_names(
    plan: PiecePlan, recording_name: str, pattern: str, zone: tzinfo = UTC
) -> list[str]:
    """The pieces' names. ``<name>``: the recording's name; ``<number>``:
    01, 02, ... (as wide as the count needs, at least two digits);
    ``<date>``: the date the piece starts, in the site time zone."""
    width = max(2, len(str(len(plan.pieces))))
    out = []
    for piece in plan.pieces:
        day = to_datetime(piece.start, zone).strftime("%Y-%m-%d")
        out.append(
            pattern.replace(TOKEN_NAME, recording_name)
            .replace(TOKEN_NUMBER, f"{piece.number:0{width}d}")
            .replace(TOKEN_DATE, day)
        )
    return out


# --------------------------------------------------------------------------
# For the batch


def piece_key(source: Source, piece: Piece) -> str:
    return f"{source.key}#{piece.number}"


def piece_sources(source: Source, plan: PiecePlan) -> list[Source]:
    """One :class:`~.makenew.Source` per piece, for the shared name checks."""
    return [replace(source, key=piece_key(source, p)) for p in plan.pieces]


def requests_for(
    source: Source,
    plan: PiecePlan,
    names: Sequence[str],
    folder: str,
    output_password: tuple[str, str] | None = None,
) -> list[ChangeRequest]:
    """One copy per piece: the whole recording's channels, the piece's time
    window, exact edges. ``source.password`` opens the original."""
    return [
        ChangeRequest(
            source.path,
            destination_for(folder, name),
            password=source.password,
            output_password=output_password,
            time_range=p.window,
            exact=True,
            key=piece_key(source, p),
        )
        for p, name in zip(plan.pieces, names, strict=True)
    ]


def estimates_for(source: Source, plan: PiecePlan) -> dict[str, int]:
    return {piece_key(source, p): p.bytes for p in plan.pieces}
