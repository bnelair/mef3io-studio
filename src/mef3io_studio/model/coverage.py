"""Where several recordings have data, on one shared time line.

Feeds the Explorer's combined timeline (one row per recording) and the
table export. Worked out from each part's start, end and sample count only
(what the recording's details already hold): no signal and no block table
is read, and nothing here opens a file.

For each recording:

* **coverage steps**: stretches of time with how many of its channels have
  data there (a channel counts once, however many of its parts overlap), and
  how many of those channels' parts have gaps inside them (a part whose
  samples cover less time than it spans; where exactly the gaps are is only
  in the block table, so the whole part is marked);
* **stretches with data** and the **gaps** between them, with the same
  rounding rule as the Overview tab (``RecordingDetails.gaps``).

Across recordings (only those whose times are verified; unverified times
are kept out of the absolute order, docs/design/explorer.md):

* the first and last time, the total span, and the time with data;
* the **gaps between recordings** (stretches where none of them has data,
  between one recording's last data and the next one's first);
* **overlaps**: time covered by two or more recordings, per pair, because
  two recordings of the same time may be copies of each other.

Times are integers, microseconds since 1970, UTC, as everywhere in
``model``. Plain Python and numpy, no Qt. Runs on a background thread.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, field

import numpy as np

from .recording import RecordingDetails

#: Why a recording has (or has no) coverage to draw.
STATUS_READY = "ready"
STATUS_READING = "reading"
STATUS_LOCKED = "locked"
STATUS_WRONG_PASSWORD = "wrong password"
STATUS_UNREADABLE = "unreadable"

#: Two recordings that share at least this much of the shorter one's data
#: time are flagged as possible copies of each other.
COPY_SHARE = 0.9

_EMPTY = np.zeros(0, dtype=np.int64)


class Stopped(Exception):
    """The caller asked to stop (a newer layout replaces this one)."""


@dataclass(frozen=True)
class RecordingInput:
    """One recording to lay out: what the Explorer already knows about it."""

    key: str
    #: The name the recordings list shows (never patient details).
    name: str
    details: RecordingDetails | None
    status: str = STATUS_READY


@dataclass(frozen=True, eq=False)
class RecordingCoverage:
    """Where one recording has data. Depends on that recording only, so it
    can be kept and reused while the recording does not change."""

    key: str
    name: str
    status: str
    #: False: the times may not be the real ones (see ``times_verified``).
    verified: bool = True
    #: meflib's deprecated date masking is in use (identifiable).
    uses_time_offset: bool = False
    #: The time offset cannot be read (it is behind the full-access password).
    offset_unknown: bool = False
    channel_count: int = 0
    part_count: int = 0
    parts_with_gaps_inside: int = 0
    first: int | None = None
    last: int | None = None
    #: Coverage steps, sorted, not overlapping: [starts[i], ends[i]) has
    #: data in ``counts[i]`` channels, ``inside[i]`` of them in a part with
    #: gaps inside. Only stretches with data are listed.
    starts: np.ndarray = field(default_factory=lambda: _EMPTY)
    ends: np.ndarray = field(default_factory=lambda: _EMPTY)
    counts: np.ndarray = field(default_factory=lambda: _EMPTY)
    inside: np.ndarray = field(default_factory=lambda: _EMPTY)
    #: Stretches with data (any channel), and the gaps between them.
    runs: tuple[tuple[int, int], ...] = ()
    gaps: tuple[tuple[int, int], ...] = ()

    @property
    def has_data(self) -> bool:
        return bool(self.runs)

    @property
    def data_seconds(self) -> float:
        return sum(e - s for s, e in self.runs) / 1e6

    @property
    def gap_seconds(self) -> float:
        return sum(e - s for s, e in self.gaps) / 1e6

    @property
    def span_seconds(self) -> float | None:
        if self.first is None or self.last is None:
            return None
        return (self.last - self.first) / 1e6

    def run_at(self, t: int) -> tuple[int, int] | None:
        """The stretch with data that contains ``t``, if any."""
        i = _bisect_right([s for s, _ in self.runs], t) - 1
        if i >= 0 and self.runs[i][0] <= t <= self.runs[i][1]:
            return self.runs[i]
        return None

    def gap_at(self, t: int) -> tuple[int, int] | None:
        for start, end in self.gaps:
            if start < t < end:
                return start, end
        return None

    def step_at(self, t: int) -> tuple[int, int] | None:
        """``(channels with data, of them in parts with gaps inside)`` at
        ``t``, or None where no channel has data."""
        if not len(self.starts):
            return None
        i = int(np.searchsorted(self.starts, t, side="right")) - 1
        if i >= 0 and t < self.ends[i]:
            return int(self.counts[i]), int(self.inside[i])
        return None


@dataclass(frozen=True)
class Overlap:
    """Two recordings with data at the same time."""

    first: str  # key
    second: str  # key
    microseconds: int
    #: The shared time is at least COPY_SHARE of the shorter one's data.
    may_be_copies: bool = False

    @property
    def seconds(self) -> float:
        return self.microseconds / 1e6


@dataclass(frozen=True)
class CombinedLayout:
    """Every recording's coverage, in display order, plus the figures
    across them."""

    #: Verified recordings with data by first time, then unverified ones by
    #: first time, then the ones with nothing to draw by name.
    rows: tuple[RecordingCoverage, ...] = ()
    #: Across the verified recordings with data only.
    first: int | None = None
    last: int | None = None
    data_microseconds: int = 0
    gaps_between: tuple[tuple[int, int], ...] = ()
    overlaps: tuple[Overlap, ...] = ()
    #: Time covered by two or more recordings (merged).
    overlap_spans: tuple[tuple[int, int], ...] = ()
    #: Per recording key: the time it shares with another recording.
    overlap_spans_of: dict = field(default_factory=dict)
    #: Per recording key: the keys of the recordings it overlaps.
    overlapping: dict = field(default_factory=dict)

    @property
    def counted(self) -> list[RecordingCoverage]:
        return [r for r in self.rows if r.verified and r.has_data]

    @property
    def unverified(self) -> list[RecordingCoverage]:
        return [r for r in self.rows if r.has_data and not r.verified]

    @property
    def without_data(self) -> list[RecordingCoverage]:
        return [r for r in self.rows if not r.has_data]

    @property
    def span_seconds(self) -> float | None:
        if self.first is None or self.last is None:
            return None
        return (self.last - self.first) / 1e6

    @property
    def data_seconds(self) -> float:
        return self.data_microseconds / 1e6

    @property
    def gaps_between_seconds(self) -> float:
        return sum(e - s for s, e in self.gaps_between) / 1e6

    @property
    def overlap_seconds(self) -> float:
        return sum(e - s for s, e in self.overlap_spans) / 1e6

    def row(self, key: str) -> RecordingCoverage | None:
        for r in self.rows:
            if r.key == key:
                return r
        return None


def _bisect_right(values: Sequence[int], x: int) -> int:
    lo, hi = 0, len(values)
    while lo < hi:
        mid = (lo + hi) // 2
        if x < values[mid]:
            hi = mid
        else:
            lo = mid + 1
    return lo


def merge_intervals(starts, ends, tolerance: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """The union of intervals, joining ones closer than ``tolerance``.
    Zero-length intervals are kept (as points) unless something covers them."""
    starts = np.asarray(starts, dtype=np.int64)
    ends = np.maximum(np.asarray(ends, dtype=np.int64), starts)
    if not len(starts):
        return _EMPTY, _EMPTY
    order = np.argsort(starts, kind="stable")
    s, e = starts[order], ends[order]
    reach = np.maximum.accumulate(e)
    new = np.ones(len(s), dtype=bool)
    new[1:] = s[1:] > reach[:-1] + tolerance
    first = np.flatnonzero(new)
    last = np.append(first[1:] - 1, len(s) - 1)
    return s[first], reach[last]


def _tolerance(rates) -> int:
    """1.5 sample periods of the slowest channel: below that it is rounding,
    not a gap (the Overview tab's rule, ``RecordingDetails.gaps``)."""
    rates = [r for r in rates if r > 0]
    return int(1.5e6 / min(rates)) if rates else 0


def _count_steps(intervals: list[tuple[np.ndarray, np.ndarray]], grid: np.ndarray) -> np.ndarray:
    """How many of the interval sets cover each [grid[i], grid[i+1])."""
    delta = np.zeros(len(grid), dtype=np.int64)
    for s, e in intervals:
        keep = e > s
        if keep.any():
            np.add.at(delta, np.searchsorted(grid, s[keep]), 1)
            np.add.at(delta, np.searchsorted(grid, e[keep]), -1)
    return np.cumsum(delta)[:-1]


def _at_least(sets, n: int) -> tuple[np.ndarray, np.ndarray]:
    """The (merged) time covered by at least ``n`` of the interval sets
    (each set's intervals must not overlap each other)."""
    nonempty = [(s, e) for s, e in sets if len(s)]
    if len(nonempty) < n:
        return _EMPTY, _EMPTY
    grid = np.unique(np.concatenate([np.concatenate(pair) for pair in nonempty]))
    counts = _count_steps(nonempty, grid)
    keep = counts >= n
    return merge_intervals(grid[:-1][keep], grid[1:][keep])


def drive(steps: Generator, should_stop=None, pause=None):
    """Run a ``*_steps`` generator to the end and return its value. At each
    pause point: raise :class:`Stopped` when ``should_stop()`` is true, and
    call ``pause()`` (a worker thread passes a short sleep, so the window's
    thread gets its turn)."""
    try:
        while True:
            next(steps)
            if should_stop is not None and should_stop():
                steps.close()
                raise Stopped
            if pause is not None:
                pause()
    except StopIteration as done:
        return done.value


def recording_coverage(item: RecordingInput) -> RecordingCoverage:
    """Where one recording has data (see the module docstring)."""
    return drive(recording_coverage_steps(item))


def recording_coverage_steps(item: RecordingInput) -> Generator[None, None, RecordingCoverage]:
    """:func:`recording_coverage`, pausing (``yield``) after each channel,
    so a caller can spread the work over many short slices."""
    details = item.details
    if details is None or item.status != STATUS_READY:
        status = item.status if item.status != STATUS_READY else STATUS_READING
        return RecordingCoverage(item.key, item.name, status)
    verified = details.times_verified
    channel_sets: list[tuple[np.ndarray, np.ndarray]] = []
    inside_sets: list[tuple[np.ndarray, np.ndarray]] = []
    all_starts: list[np.ndarray] = []
    all_ends: list[np.ndarray] = []
    part_count = gappy = 0
    for channel in details.channels:
        parts = channel.parts
        if not parts:
            continue
        part_count += len(parts)
        fs = channel.sampling_frequency
        s = np.fromiter((p.start for p in parts), dtype=np.int64, count=len(parts))
        e = np.fromiter((p.end for p in parts), dtype=np.int64, count=len(parts))
        e = np.maximum(e, s)  # a part that ends before it starts is a point
        all_starts.append(s)
        all_ends.append(e)
        tol = _tolerance([fs])
        channel_sets.append(merge_intervals(s, e, tol))
        has_gaps = np.fromiter(
            (p.missing_seconds(fs) > 0 for p in parts), dtype=bool, count=len(parts)
        )
        if has_gaps.any():
            gappy += int(has_gaps.sum())
            inside_sets.append(merge_intervals(s[has_gaps], e[has_gaps], tol))
        yield
    if not channel_sets:
        return RecordingCoverage(
            item.key,
            item.name,
            STATUS_READY,
            verified=verified,
            uses_time_offset=details.uses_time_offset,
            offset_unknown=details.recording_time_offset is None,
            channel_count=len(details.channels),
        )
    # Stretches with data and the gaps between them (the Overview's rule).
    rs, re_ = merge_intervals(
        np.concatenate(all_starts),
        np.concatenate(all_ends),
        _tolerance([c.sampling_frequency for c in details.channels]),
    )
    runs = tuple(zip(rs.tolist(), re_.tolist(), strict=True))
    gaps = tuple((a[1], b[0]) for a, b in zip(runs, runs[1:], strict=False))
    # How many channels have data at each moment.
    grid = np.unique(np.concatenate([np.concatenate(pair) for pair in channel_sets + inside_sets]))
    counts = _count_steps(channel_sets, grid)
    inside = _count_steps(inside_sets, grid) if inside_sets else np.zeros_like(counts)
    starts, ends = grid[:-1], grid[1:]
    # Keep stretches with data, and join neighbours that read the same.
    keep = counts > 0
    starts, ends, counts, inside = starts[keep], ends[keep], counts[keep], inside[keep]
    if len(starts) > 1:
        same = (starts[1:] == ends[:-1]) & (counts[1:] == counts[:-1]) & (inside[1:] == inside[:-1])
        begin = np.ones(len(starts), dtype=bool)
        begin[1:] = ~same
        first = np.flatnonzero(begin)
        last = np.append(first[1:] - 1, len(starts) - 1)
        starts, ends, counts, inside = starts[first], ends[last], counts[first], inside[first]
    return RecordingCoverage(
        item.key,
        item.name,
        STATUS_READY,
        verified=verified,
        uses_time_offset=details.uses_time_offset,
        offset_unknown=details.recording_time_offset is None,
        channel_count=len(details.channels),
        part_count=part_count,
        parts_with_gaps_inside=gappy,
        first=runs[0][0],
        last=max(e for _, e in runs),
        starts=starts,
        ends=ends,
        counts=counts,
        inside=inside,
        runs=runs,
        gaps=gaps,
    )


def _order(row: RecordingCoverage):
    if row.has_data:
        return (0 if row.verified else 1, row.first, row.name.casefold(), row.key)
    return (2, 0, row.name.casefold(), row.key)


def combine(
    items: Sequence[RecordingInput],
    cache: dict | None = None,
    should_stop: Callable[[], bool] | None = None,
    pause: Callable[[], None] | None = None,
) -> tuple[CombinedLayout, dict]:
    """Lay out ``items`` on one time line.

    ``cache`` maps a recording key to ``(details, RecordingCoverage)`` from
    an earlier call: a recording whose details object is the same is not
    worked out again. Returns the layout and the cache to keep. Raises
    :class:`Stopped` when ``should_stop()`` turns true."""
    return drive(combine_steps(items, cache), should_stop, pause)


def combine_steps(
    items: Sequence[RecordingInput], cache: dict | None = None
) -> Generator[None, None, tuple[CombinedLayout, dict]]:
    """:func:`combine`, pausing (``yield``) every few milliseconds of work."""
    cache = cache or {}
    new_cache: dict = {}
    rows = []
    for item in items:
        cached = cache.get(item.key)
        if (
            cached is not None
            and cached[0] is item.details
            and cached[1].status == item.status
            and cached[1].name == item.name
        ):
            coverage = cached[1]
        else:
            coverage = yield from recording_coverage_steps(item)
        new_cache[item.key] = (item.details, coverage)
        rows.append(coverage)
        yield
    rows.sort(key=_order)
    layout = yield from across_steps(rows)
    return layout, new_cache


def across(rows: Sequence[RecordingCoverage]) -> CombinedLayout:
    """The figures across recordings (verified ones with data only)."""
    return drive(across_steps(rows))


def across_steps(
    rows: Sequence[RecordingCoverage],
) -> Generator[None, None, CombinedLayout]:
    """:func:`across`, pausing (``yield``) as it goes."""
    counted = [r for r in rows if r.verified and r.has_data]
    if not counted:
        return CombinedLayout(rows=tuple(rows))
    first = min(r.first for r in counted)
    last = max(r.last for r in counted)
    # Gaps between recordings: where none of them is recording at all.
    ext_s, ext_e = merge_intervals([r.first for r in counted], [r.last for r in counted])
    gaps_between = tuple(
        (int(a), int(b)) for a, b in zip(ext_e[:-1].tolist(), ext_s[1:].tolist(), strict=True)
    )
    # Vectorised over every recording's stretches with data (a point has no
    # time to share): the time with data, and the time two or more share.
    sets = []
    for n, row in enumerate(counted):
        arr = np.array(row.runs, dtype=np.int64).reshape(-1, 2)
        arr = arr[arr[:, 1] > arr[:, 0]]
        sets.append((arr[:, 0], arr[:, 1]))
        if n % 32 == 31:
            yield
    lengths = [int((e - s).sum()) for s, e in sets]
    all_s = np.concatenate([s for s, _ in sets])
    all_e = np.concatenate([e for _, e in sets])
    us, ue = merge_intervals(all_s, all_e)
    with_data = int((ue - us).sum())
    yield
    shared_s, shared_e = _at_least(sets, 2)
    yield
    shared = list(zip(shared_s.tolist(), shared_e.tolist(), strict=True))
    shared_of: dict[str, tuple] = {}
    overlaps = []
    overlapping: dict[str, list[str]] = {}
    if shared:
        # Only recordings whose first-to-last extents meet can share time.
        order = sorted(range(len(counted)), key=lambda i: counted[i].first)
        for n, i in enumerate(order):
            yield
            a = counted[i]
            mine = _at_least([sets[i], (shared_s, shared_e)], 2)
            if len(mine[0]):
                shared_of[a.key] = tuple(zip(mine[0].tolist(), mine[1].tolist(), strict=True))
            else:
                continue
            for j in order[n + 1 :]:
                b = counted[j]
                if b.first >= a.last:
                    break
                yield
                # shared = |a| + |b| - |a or b|
                u_s, u_e = merge_intervals(
                    np.concatenate((sets[i][0], sets[j][0])),
                    np.concatenate((sets[i][1], sets[j][1])),
                )
                us_shared = lengths[i] + lengths[j] - int((u_e - u_s).sum())
                if us_shared <= 0:
                    continue
                shorter = min(lengths[i], lengths[j])
                copies = shorter > 0 and us_shared >= COPY_SHARE * shorter
                first_key, second_key = (a.key, b.key)
                overlaps.append(Overlap(first_key, second_key, us_shared, copies))
                overlapping.setdefault(a.key, []).append(b.key)
                overlapping.setdefault(b.key, []).append(a.key)
    overlaps.sort(key=lambda o: -o.microseconds)
    return CombinedLayout(
        rows=tuple(rows),
        first=first,
        last=last,
        data_microseconds=with_data,
        gaps_between=gaps_between,
        overlaps=tuple(overlaps),
        overlap_spans=tuple(shared),
        overlap_spans_of=shared_of,
        overlapping={k: tuple(v) for k, v in overlapping.items()},
    )


# --------------------------------------------------------------------------
# Drawing aid: what to draw at a given zoom, already merged per pixel.
# --------------------------------------------------------------------------

#: Pixel classes.
PIXEL_NONE, PIXEL_SOME, PIXEL_ALL = 0, 1, 2


def _touched(starts, ends, v0: int, scale: float, width: int) -> np.ndarray:
    """Which of ``width`` pixel columns the intervals touch (a point or a
    very short stretch still touches one column, so it stays visible)."""
    out = np.zeros(width + 1, dtype=np.int64)
    if not len(starts):
        return out[:width] > 0
    x0 = (np.asarray(starts, dtype=np.int64) - v0) * scale
    x1 = (np.asarray(ends, dtype=np.int64) - v0) * scale
    visible = (x1 >= 0) & (x0 < width)
    if not visible.any():
        return out[:width] > 0
    i0 = np.clip(np.floor(x0[visible]), 0, width - 1).astype(np.int64)
    i1 = np.clip(np.ceil(x1[visible]) - 1, 0, width - 1).astype(np.int64)
    i1 = np.maximum(i1, i0)
    np.add.at(out, i0, 1)
    np.add.at(out, i1 + 1, -1)
    return np.cumsum(out)[:width] > 0


def pixel_runs(
    row: RecordingCoverage, v0: int, v1: int, width: int
) -> list[tuple[int, int, int, bool]]:
    """What to draw for ``row`` when ``[v0, v1]`` spans ``width`` pixels:
    ``(x0, x1, class, gaps_inside)`` runs of pixel columns, x1 exclusive.

    Everything narrower than a pixel is merged into its column, so the
    amount drawn depends on the width, not on how many parts there are. A
    column with any data from every channel is ``PIXEL_ALL``; one with data
    from some channels only is ``PIXEL_SOME``."""
    width = int(width)
    if width <= 0 or v1 <= v0 or not row.runs:
        return []
    scale = width / (v1 - v0)
    starts, ends = row.starts, row.ends
    # Only the steps that can be on screen.
    lo = int(np.searchsorted(ends, v0, side="left"))
    hi = int(np.searchsorted(starts, v1, side="right"))
    s, e = starts[lo:hi], ends[lo:hi]
    c, ins = row.counts[lo:hi], row.inside[lo:hi]
    runs = np.array(row.runs, dtype=np.int64).reshape(-1, 2)
    any_ = _touched(runs[:, 0], runs[:, 1], v0, scale, width)
    full = _touched(s[c >= row.channel_count], e[c >= row.channel_count], v0, scale, width)
    gappy = _touched(s[ins > 0], e[ins > 0], v0, scale, width)
    cls = np.where(full, PIXEL_ALL, np.where(any_, PIXEL_SOME, PIXEL_NONE))
    key = cls * 2 + gappy.astype(np.int64)
    change = np.flatnonzero(np.diff(key)) + 1
    bounds = np.concatenate(([0], change, [width]))
    out = []
    for a, b in zip(bounds[:-1].tolist(), bounds[1:].tolist(), strict=True):
        k = int(key[a])
        if k // 2 != PIXEL_NONE:
            out.append((a, b, k // 2, bool(k % 2)))
    return out


def span_runs(spans, v0: int, v1: int, width: int) -> list[tuple[int, int]]:
    """Pixel runs ``(x0, x1)`` touched by ``spans`` (see :func:`pixel_runs`)."""
    width = int(width)
    if width <= 0 or v1 <= v0 or not spans:
        return []
    arr = np.array(spans, dtype=np.int64).reshape(-1, 2)
    hit = _touched(arr[:, 0], arr[:, 1], v0, width / (v1 - v0), width)
    change = np.flatnonzero(np.diff(hit.astype(np.int8))) + 1
    bounds = np.concatenate(([0], change, [width]))
    return [
        (a, b) for a, b in zip(bounds[:-1].tolist(), bounds[1:].tolist(), strict=True) if hit[a]
    ]
