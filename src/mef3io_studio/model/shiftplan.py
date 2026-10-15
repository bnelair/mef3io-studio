"""How much each recording's times are shifted in an anonymized copy.

The planner of the anonymizer wizard (docs/design/anonymizer.md, step 2;
mef3io-studio#5). It decides one ``shift_us`` per recording; mef3io's
``anonymize_session`` (mef3io#70) applies it. Nothing here touches a file,
imports mef3io or logs anything: the input is plain data (each recording's
start and end, in microseconds since 1970, UTC, plus a few flags) and the
output is a :class:`ShiftPlan` of plain data, with warnings, blockers and
refusals as structured codes. Words for people belong to the UI.

The rules, exactly as the design states them:

* **Reference recording:** the earliest start by default (ties: the first one
  given), or any recording the caller names.
* **New date** for the reference (default 2000-01-01). **Time of day**: kept on
  the site's local clock (default), kept on the UTC clock, or set too.
* **D** is a whole number of days measured on the clock the mode keeps:
  keep-local, ``D = new date - the reference's local start date`` in the site
  time zone; keep-UTC, ``D = new date - the reference's UTC start date``.
* **Shift:** keep-local, ``D + off_ref``, where ``off_ref`` is the reference
  start's UTC offset in the site zone at that instant (daylight saving
  included); keep-UTC, ``D``. Either way the copy's UTC clock reads the kept
  time of day on the new date. Set-the-time: the reference copy starts at the
  chosen date and time on the copy's clock, which is UTC (every copy carries
  ``GMT_offset = 0``).
* **Keep the weekday:** only the date is moved, to the nearest one with the
  same weekday as the original (so ``D`` becomes a multiple of 7 in the keep
  modes). ``off_ref`` is never rounded.
* **Relative** mode: one shift for every recording. **Aligned** mode: every
  recording starts at the reference copy's start, so each gets its own shift.
* **Refused:** a shift smaller than the time span of the batch's originals
  plus one day (aligned: each recording's own shift), because the leak scan
  could not tell a leaked original from a shifted value; and any shifted time
  below :data:`MIN_SHIFTED_US` or past :data:`MAX_TIME_US`.
* **Warned:** recordings that overlap in an aligned copy; a recording that
  crosses a daylight-saving change in the site zone; in relative keep-local
  mode, a recording whose UTC offset at its start differs from the
  reference's (its copy shows a time of day that far off, and its date and
  weekday flip when that crosses midnight).

Recordings that cannot be planned are listed in :attr:`ShiftPlan.blocked`
with :class:`Blocker` codes and take no part in the plan (nor in the batch
span). In particular a recording whose times are not verified (a non-zero
recording-time offset, meflib's deprecated date masking, whose sign
convention the bytes cannot settle; or one that is unknown because the
full-access password is missing) is blocked: a wrong reading of its times
would put a wrong date in the copy and defeat the leak scan.

Privacy: original times and shifts are re-identifying (the shift is what the
optional key file stores), so they are left out of every ``repr`` here; a
stray log line of a plan shows only ids and codes. Ids are the caller's
opaque keys and must not be names or paths.

Plain Python, no Qt.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .recording import ACCESS_BASIC, RecordingDetails

#: One day in microseconds.
DAY_US = 86_400_000_000
_ONE_US = timedelta(microseconds=1)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Every shifted time must be at least this (1970-01-02 00:00 UTC). MEF stores
#: times negated as a marker, so a time must stay well above 0; one day of
#: margin also keeps every site-zone date after 1970-01-01.
MIN_SHIFTED_US = DAY_US
#: The last instant handled (9999-12-30 00:00 UTC), one day short of what
#: Python's dates reach, so a time stays convertible in every zone.
MAX_TIME_US = (datetime(9999, 12, 30, tzinfo=UTC) - _EPOCH) // _ONE_US

#: The default new date of the reference copy.
DEFAULT_NEW_DATE = date(2000, 1, 1)

#: Clock changes are looked for at this step, then pinned to the microsecond.
#: Two changes that cancel out within one step would be missed; no zone in the
#: IANA database changes its offset twice within six hours.
_SCAN_STEP_US = 6 * 3600 * 1_000_000


class Mode(enum.StrEnum):
    RELATIVE = "relative"  # one shift for every recording
    ALIGNED = "aligned"  # every recording starts at the same moment


class TimeOfDay(enum.StrEnum):
    KEEP_LOCAL = "keep_local"  # the site's wall-clock time of day is kept
    KEEP_UTC = "keep_utc"  # the UTC time of day is kept
    SET = "set"  # the time of day is chosen too


class Blocker(enum.StrEnum):
    """Why a recording cannot be planned."""

    #: Its times may not be the real ones (a non-zero recording-time offset,
    #: or one that could not be read).
    TIMES_UNVERIFIED = "times_unverified"
    #: Opened without the full-access password; anonymizing needs it.
    LOCKED = "locked"
    #: No usable start/end (missing, end before start, or out of range).
    TIMES_INVALID = "times_invalid"


class RefusalCode(enum.StrEnum):
    """Why the plan cannot run as it stands."""

    NOTHING_TO_PLAN = "nothing_to_plan"  # no recording can be planned
    REFERENCE_BLOCKED = "reference_blocked"  # the chosen reference is blocked
    UNKNOWN_ZONE = "unknown_zone"  # the site zone name is not in the database
    NEEDS_SITE_ZONE = "needs_site_zone"  # keep-local without a site zone
    BELOW_FLOOR = "below_floor"  # |shift| < batch span + one day
    TOO_EARLY = "too_early"  # a shifted time below MIN_SHIFTED_US
    TOO_LATE = "too_late"  # a shifted time past MAX_TIME_US (or no such date)


class WarningCode(enum.StrEnum):
    ALIGNED_OVERLAP = "aligned_overlap"
    DST_CROSSING = "dst_crossing"
    #: The UTC offset at the recording's start differs from the reference's
    #: (normally the other daylight-saving season; also a zone's own rule
    #: change between the two dates).
    OTHER_SEASON = "other_season"


# --------------------------------------------------------------------------
# Input


@dataclass(frozen=True)
class RecordingTimes:
    """One recording as the planner sees it."""

    #: The caller's stable, opaque key (never a name or a path).
    id: str
    start_us: int | None = field(repr=False)
    end_us: int | None = field(repr=False)
    #: False when the times may not be the real ones (Explorer:
    #: ``RecordingDetails.times_verified``).
    times_verified: bool = True
    #: True when the full-access password is missing.
    locked: bool = False


def recording_times(details: RecordingDetails, recording_id: str) -> RecordingTimes:
    """What the Explorer knows about a recording, as planner input."""
    return RecordingTimes(
        id=recording_id,
        start_us=details.start,
        end_us=details.end,
        times_verified=details.times_verified,
        locked=details.access == ACCESS_BASIC,
    )


@dataclass(frozen=True)
class ShiftSettings:
    """The user's choices in step 2. The defaults are the design's."""

    mode: Mode = Mode.RELATIVE
    time_of_day: TimeOfDay = TimeOfDay.KEEP_LOCAL
    new_date: date = DEFAULT_NEW_DATE
    #: Used only with ``TimeOfDay.SET``: the reference copy's start time on
    #: the copy's clock (UTC). Must be naive.
    new_time: time = time(0, 0)
    #: IANA name of the recording site's time zone, e.g. "Europe/Prague".
    #: Required for keep-local; when given, also used for the clock-change
    #: warnings in every mode.
    site_zone: str | None = None
    keep_weekday: bool = False
    #: None = the earliest-starting recording.
    reference_id: str | None = None


# --------------------------------------------------------------------------
# Output


@dataclass(frozen=True)
class ClockChange:
    """A daylight-saving (or other offset) change inside a recording."""

    #: Time from the recording's start to the change (not a date).
    after_us: int
    offset_before_us: int
    offset_after_us: int

    @property
    def change_us(self) -> int:
        return self.offset_after_us - self.offset_before_us


@dataclass(frozen=True)
class PlanWarning:
    code: WarningCode
    recording_id: str
    #: ALIGNED_OVERLAP: the recordings this one overlaps in the copy.
    overlaps: tuple[str, ...] = ()
    #: DST_CROSSING: every clock change inside the recording.
    clock_changes: tuple[ClockChange, ...] = ()
    #: OTHER_SEASON: how far the copy's time of day is from the wall clock at
    #: the original start (copy minus wall clock; reference offset minus this
    #: recording's).
    time_of_day_off_us: int = 0
    #: OTHER_SEASON: the copy's date minus the date it would show with the
    #: wall-clock time of day kept: -1, 0 or +1. Non-zero = the date and the
    #: weekday flip.
    day_shift: int = 0

    @property
    def weekday_flips(self) -> bool:
        return self.day_shift != 0


@dataclass(frozen=True)
class Refusal:
    code: RefusalCode
    #: The recording it concerns; None when it concerns the whole batch.
    recording_id: str | None = None
    #: BELOW_FLOOR: the smallest allowed size of a shift.
    limit_us: int | None = None
    shift_us: int | None = field(default=None, repr=False)


@dataclass(frozen=True)
class BlockedRecording:
    id: str
    reasons: tuple[Blocker, ...]


@dataclass(frozen=True)
class PlannedShift:
    id: str
    shift_us: int = field(repr=False)
    start_us: int = field(repr=False)
    end_us: int = field(repr=False)
    warnings: tuple[PlanWarning, ...] = ()

    @property
    def new_start_us(self) -> int:
        return self.start_us + self.shift_us

    @property
    def new_end_us(self) -> int:
        return self.end_us + self.shift_us


@dataclass(frozen=True)
class ShiftPlan:
    settings: ShiftSettings
    #: In input order, blocked recordings left out.
    recordings: tuple[PlannedShift, ...] = ()
    blocked: tuple[BlockedRecording, ...] = ()
    refusals: tuple[Refusal, ...] = ()
    reference_id: str | None = None
    #: The new date actually used (after the weekday option).
    new_date: date | None = None
    #: D in days (keep-local / keep-UTC); None for set-the-time.
    days: int | None = None
    #: off_ref (keep-local only).
    reference_offset_us: int | None = None
    #: The span of the planned originals, and the smallest allowed shift.
    batch_span_us: int | None = None
    floor_us: int | None = None

    @property
    def runnable(self) -> bool:
        """True when every planned recording can be anonymized as planned."""
        return bool(self.recordings) and not self.refusals

    @property
    def warnings(self) -> tuple[PlanWarning, ...]:
        return tuple(w for r in self.recordings for w in r.warnings)

    def shift_for(self, recording_id: str) -> int | None:
        for r in self.recordings:
            if r.id == recording_id:
                return r.shift_us
        return None


# --------------------------------------------------------------------------
# Time helpers (exact integer microseconds)


def _datetime(us: int, zone: tzinfo) -> datetime:
    return (_EPOCH + timedelta(microseconds=us)).astimezone(zone)


def _offset_us(us: int, zone: tzinfo) -> int:
    return _datetime(us, zone).utcoffset() // _ONE_US


def _us(moment: datetime) -> int:
    return (moment - _EPOCH) // _ONE_US


def _nearest_same_weekday(target: date, original: date) -> date:
    """The date nearest ``target`` on ``original``'s weekday (never a tie:
    the candidates are at most three days away on either side)."""
    rest = (target - original).days % 7
    return target - timedelta(days=rest) if rest <= 3 else target + timedelta(days=7 - rest)


def _resolve_zone(name: str | None) -> tuple[tzinfo | None, bool]:
    """(zone, ok). An empty name means no zone."""
    if not name:
        return None, True
    if name == "UTC":
        return UTC, True
    try:
        return ZoneInfo(name), True
    except (ZoneInfoNotFoundError, ValueError):
        return None, False


def clock_changes(start_us: int, end_us: int, zone: tzinfo) -> tuple[ClockChange, ...]:
    """Every change of the UTC offset in ``zone`` within (start, end]."""
    changes = []
    t, offset = start_us, _offset_us(start_us, zone)
    while t < end_us:
        nxt = min(t + _SCAN_STEP_US, end_us)
        nxt_offset = _offset_us(nxt, zone)
        if nxt_offset != offset:
            lo, hi = t, nxt  # offset(lo) == offset, offset(hi) != offset
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if _offset_us(mid, zone) == offset:
                    lo = mid
                else:
                    hi = mid
            changes.append(ClockChange(hi - start_us, offset, nxt_offset))
        t, offset = nxt, nxt_offset
    return tuple(changes)


def _blockers(rec: RecordingTimes) -> tuple[Blocker, ...]:
    out = []
    if rec.locked:
        out.append(Blocker.LOCKED)
    if not rec.times_verified:
        out.append(Blocker.TIMES_UNVERIFIED)
    s, e = rec.start_us, rec.end_us
    if (
        not isinstance(s, int)
        or not isinstance(e, int)
        or isinstance(s, bool)
        or isinstance(e, bool)
        or not (0 < s <= e <= MAX_TIME_US)
    ):
        out.append(Blocker.TIMES_INVALID)
    return tuple(out)


# --------------------------------------------------------------------------
# The planner


def plan_shifts(
    recordings: Iterable[RecordingTimes], settings: ShiftSettings | None = None
) -> ShiftPlan:
    """Plan every recording's shift. Never raises for data problems (those
    become blockers or refusals); raises ``ValueError`` for caller mistakes:
    duplicate ids, a reference id that is not in the batch, a ``new_time``
    with a time zone."""
    settings = settings or ShiftSettings()
    recs: Sequence[RecordingTimes] = tuple(recordings)
    ids = [r.id for r in recs]
    if len(set(ids)) != len(ids):
        raise ValueError("recording ids must be unique")
    if settings.reference_id is not None and settings.reference_id not in ids:
        raise ValueError("the reference id is not in the batch")
    if settings.new_time.tzinfo is not None:
        raise ValueError("new_time must be naive (it is read on the copy's UTC clock)")
    mode, tod = Mode(settings.mode), TimeOfDay(settings.time_of_day)

    blocked: list[BlockedRecording] = []
    plannable: list[RecordingTimes] = []
    for r in recs:
        reasons = _blockers(r)
        if reasons:
            blocked.append(BlockedRecording(r.id, reasons))
        else:
            plannable.append(r)
    base = ShiftPlan(settings=settings, blocked=tuple(blocked))

    zone, zone_ok = _resolve_zone(settings.site_zone)
    pre: list[Refusal] = []
    if not zone_ok:
        pre.append(Refusal(RefusalCode.UNKNOWN_ZONE))
    elif tod is TimeOfDay.KEEP_LOCAL and zone is None:
        pre.append(Refusal(RefusalCode.NEEDS_SITE_ZONE))
    if not plannable:
        pre.append(Refusal(RefusalCode.NOTHING_TO_PLAN))

    if settings.reference_id is None:
        ref = min(plannable, key=lambda r: r.start_us) if plannable else None
    else:
        ref = next((r for r in plannable if r.id == settings.reference_id), None)
        if ref is None:
            pre.append(Refusal(RefusalCode.REFERENCE_BLOCKED, settings.reference_id))
    if pre:
        return replace(base, refusals=tuple(pre))
    assert ref is not None

    # The reference's new start: D (and off_ref) on the clock the mode keeps.
    days = off_ref = None
    try:
        if tod is TimeOfDay.SET:
            original_date = _datetime(ref.start_us, zone or UTC).date()
            new_date = settings.new_date
            if settings.keep_weekday:
                new_date = _nearest_same_weekday(new_date, original_date)
            ref_shift = _us(datetime.combine(new_date, settings.new_time, UTC)) - ref.start_us
        else:
            clock = zone if tod is TimeOfDay.KEEP_LOCAL else UTC
            original_date = _datetime(ref.start_us, clock).date()
            new_date = settings.new_date
            if settings.keep_weekday:
                new_date = _nearest_same_weekday(new_date, original_date)
            days = (new_date - original_date).days
            off_ref = _offset_us(ref.start_us, clock) if tod is TimeOfDay.KEEP_LOCAL else None
            ref_shift = days * DAY_US + (off_ref or 0)
    except OverflowError:
        return replace(base, reference_id=ref.id, refusals=(Refusal(RefusalCode.TOO_LATE),))

    span = max(r.end_us for r in plannable) - min(r.start_us for r in plannable)
    floor = span + DAY_US
    target = ref.start_us + ref_shift

    planned, refusals = [], []
    for r in plannable:
        shift = ref_shift if mode is Mode.RELATIVE else target - r.start_us
        planned.append(PlannedShift(r.id, shift, r.start_us, r.end_us))

    if mode is Mode.RELATIVE:
        if abs(ref_shift) < floor:
            refusals.append(Refusal(RefusalCode.BELOW_FLOOR, limit_us=floor, shift_us=ref_shift))
    else:
        refusals += [
            Refusal(RefusalCode.BELOW_FLOOR, p.id, limit_us=floor, shift_us=p.shift_us)
            for p in planned
            if abs(p.shift_us) < floor
        ]
    for p in planned:
        if p.new_start_us < MIN_SHIFTED_US:
            refusals.append(Refusal(RefusalCode.TOO_EARLY, p.id, shift_us=p.shift_us))
        if p.new_end_us > MAX_TIME_US:
            refusals.append(Refusal(RefusalCode.TOO_LATE, p.id, shift_us=p.shift_us))

    warnings: dict[str, list[PlanWarning]] = {p.id: [] for p in planned}
    if mode is Mode.ALIGNED:
        _overlap_warnings(planned, warnings)
    if zone is not None:
        for p in planned:
            changes = clock_changes(p.start_us, p.end_us, zone)
            if changes:
                warnings[p.id].append(
                    PlanWarning(WarningCode.DST_CROSSING, p.id, clock_changes=changes)
                )
    if mode is Mode.RELATIVE and tod is TimeOfDay.KEEP_LOCAL:
        for p in planned:
            off = _offset_us(p.start_us, zone)
            if off == off_ref:
                continue
            try:
                expected = _datetime(p.start_us, zone).date() + timedelta(days=days)
                day_shift = (_datetime(p.new_start_us, UTC).date() - expected).days
            except OverflowError:  # refused as too late; there is no date to compare
                day_shift = 0
            warnings[p.id].append(
                PlanWarning(
                    WarningCode.OTHER_SEASON,
                    p.id,
                    time_of_day_off_us=off_ref - off,
                    day_shift=day_shift,
                )
            )

    return replace(
        base,
        recordings=tuple(
            PlannedShift(p.id, p.shift_us, p.start_us, p.end_us, tuple(warnings[p.id]))
            for p in planned
        ),
        refusals=tuple(refusals),
        reference_id=ref.id,
        new_date=new_date,
        days=days,
        reference_offset_us=off_ref,
        batch_span_us=span,
        floor_us=floor,
    )


def _overlap_warnings(planned: list[PlannedShift], out: dict[str, list[PlanWarning]]) -> None:
    """Which copies overlap in time (half-open: touching is not overlapping)."""
    order = sorted(planned, key=lambda p: (p.new_start_us, p.new_end_us))
    hits: dict[str, list[str]] = {p.id: [] for p in planned}
    for i, a in enumerate(order):
        for b in order[i + 1 :]:
            if b.new_start_us >= a.new_end_us:
                break
            if b.new_end_us > b.new_start_us:  # an empty recording overlaps nothing
                hits[a.id].append(b.id)
                hits[b.id].append(a.id)
    for p in planned:
        if hits[p.id]:
            others = tuple(sorted(hits[p.id], key=[q.id for q in planned].index))
            out[p.id].append(PlanWarning(WarningCode.ALIGNED_OVERLAP, p.id, overlaps=others))
