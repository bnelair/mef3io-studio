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
    return from_local(datetime(day.year, day.month, day.day), zone)


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


def _holds_data(runs: Sequence[tuple[int, int]], a: int, b: int) -> bool:
    """Any stretch with data inside ``[a, b)``. A stretch ``[s, e)`` ends
    where its last sample ends, so one that ends at ``a`` is not inside; a
    single sample (``s == e``) is."""
    return any(s < b and (e > a or (s == e and s >= a)) for s, e in runs)


def _data_inside(runs: Sequence[tuple[int, int]], a: int, b: int) -> int:
    return sum(max(0, min(e, b) - max(s, a)) for s, e in runs)


def plan_pieces(
    runs: Sequence[tuple[int, int]],
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
    runs = sorted(runs)
    if not runs:
        return PiecePlan(problem="This recording has no data to cut.")
    first, last = runs[0][0], max(e for _s, e in runs)
    cuts = cut_times(first, last, choice, zone)
    if isinstance(cuts, str):
        return PiecePlan(problem=cuts)
    edges = [first, *cuts, last]
    total_data = _data_inside(runs, first, last) or 1
    pieces: list[Piece] = []
    left_out = 0
    for i, (a, b) in enumerate(zip(edges, edges[1:], strict=False)):
        # The last piece owns the last sample too; the first starts a second
        # early for the same reason (nothing earlier exists).
        last_piece = i == len(edges) - 2
        stop = b + END_MARGIN_US if last_piece else b
        window = (a - END_MARGIN_US if i == 0 else a, stop)
        if not _holds_data(runs, a, stop):
            left_out += 1
            continue
        data = _data_inside(runs, a, stop)
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
