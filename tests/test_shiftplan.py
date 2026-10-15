"""The anonymizer's time-shift planner (model/shiftplan.py): the rules of
docs/design/anonymizer.md step 2 and the planner items of #5's acceptance.

Every expected value is worked out independently of the planner, from
wall-clock datetimes and zoneinfo, never by calling the code under test."""

from __future__ import annotations

import ast
import random
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mef3io_studio.model import shiftplan
from mef3io_studio.model.recording import (
    ACCESS_BASIC,
    ACCESS_FULL,
    ACCESS_OPEN,
    ChannelInfo,
    RecordingDetails,
)
from mef3io_studio.model.shiftplan import (
    DAY_US,
    MAX_TIME_US,
    MIN_SHIFTED_US,
    Blocker,
    Mode,
    RecordingTimes,
    RefusalCode,
    ShiftSettings,
    TimeOfDay,
    WarningCode,
    clock_changes,
    plan_shifts,
    recording_times,
)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
HOUR = 3600 * 1_000_000
MINUTE = 60 * 1_000_000


def us(moment: datetime) -> int:
    return (moment - EPOCH) // timedelta(microseconds=1)


def local(zone: str, *args) -> int:
    """Microseconds of a wall-clock time in ``zone``."""
    return us(datetime(*args, tzinfo=ZoneInfo(zone)))


def utc(*args) -> int:
    return us(datetime(*args, tzinfo=UTC))


def to_utc(t: int) -> datetime:
    return EPOCH + timedelta(microseconds=t)


def to_local(t: int, zone: str) -> datetime:
    return to_utc(t).astimezone(ZoneInfo(zone))


def rec(rid: str, start: int, hours: float = 2.0, **flags) -> RecordingTimes:
    return RecordingTimes(rid, start, start + int(hours * HOUR), **flags)


def keep_local(zone: str, **kw) -> ShiftSettings:
    return ShiftSettings(site_zone=zone, **kw)


def codes(items) -> list:
    return [i.code for i in items]


def warned(plan, code) -> dict:
    return {w.recording_id: w for w in plan.warnings if w.code is code}


# --------------------------------------------------------------------------
# Defaults and the reference


def test_defaults_are_the_designs():
    s = ShiftSettings()
    assert s.mode is Mode.RELATIVE
    assert s.time_of_day is TimeOfDay.KEEP_LOCAL
    assert s.new_date == date(2000, 1, 1)
    assert s.keep_weekday is False
    assert s.reference_id is None


def test_keep_local_needs_a_site_zone():
    plan = plan_shifts([rec("a", utc(2021, 3, 1, 10))])
    assert codes(plan.refusals) == [RefusalCode.NEEDS_SITE_ZONE]
    assert not plan.runnable and plan.recordings == ()
    # Keep-UTC does not.
    plan = plan_shifts(
        [rec("a", utc(2021, 3, 1, 10))], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC)
    )
    assert plan.runnable


@pytest.mark.parametrize("name", ["Mars/Olympus_Mons", "../../etc/passwd", "Europe/"])
def test_an_unknown_zone_is_refused_in_every_mode(name):
    for tod in TimeOfDay:
        plan = plan_shifts(
            [rec("a", utc(2021, 3, 1, 10))], ShiftSettings(site_zone=name, time_of_day=tod)
        )
        assert codes(plan.refusals) == [RefusalCode.UNKNOWN_ZONE]


def test_reference_defaults_to_the_earliest_start_ties_to_the_first_given():
    t = utc(2021, 5, 1, 12)
    recs = [rec("late", t + DAY_US), rec("tie1", t), rec("tie2", t)]
    plan = plan_shifts(recs, ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC))
    assert plan.reference_id == "tie1"
    plan = plan_shifts(recs, ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC, reference_id="late"))
    assert plan.reference_id == "late"
    # The chosen reference starts on the new date at its own time of day.
    assert to_utc(plan.shift_for("late") + recs[0].start_us) == datetime(2000, 1, 1, 12, tzinfo=UTC)


def test_caller_mistakes_raise():
    a = rec("a", utc(2021, 1, 1, 1))
    with pytest.raises(ValueError):
        plan_shifts([a, a])
    with pytest.raises(ValueError):
        plan_shifts([a], ShiftSettings(reference_id="nope"))
    with pytest.raises(ValueError):
        plan_shifts([a], ShiftSettings(time_of_day=TimeOfDay.SET, new_time=time(1, tzinfo=UTC)))
    for bad in (datetime(2000, 1, 1), "2000-01-01"):
        with pytest.raises(ValueError):
            plan_shifts([a], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC, new_date=bad))


def test_any_integer_type_is_accepted_as_a_time():
    class NumpyLikeInt:  # what operator.index accepts, like numpy.int64
        def __init__(self, v):
            self.v = v

        def __index__(self):
            return self.v

    start = utc(2021, 1, 1, 1)
    odd = RecordingTimes("a", NumpyLikeInt(start), NumpyLikeInt(start + HOUR))
    plan = plan_shifts([odd], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC))
    assert plan.runnable
    assert type(plan.by_id["a"].start_us) is int
    assert to_utc(plan.by_id["a"].new_start_us) == datetime(2000, 1, 1, 1, tzinfo=UTC)
    for bad in (True, 1.5e15, "1"):
        plan = plan_shifts([RecordingTimes("b", bad, start)], ShiftSettings(time_of_day="keep_utc"))
        assert plan.blocked[0].reasons == (Blocker.TIMES_INVALID,)


def test_settings_accept_plain_strings_for_the_choices():
    plan = plan_shifts(
        [rec("a", utc(2021, 1, 1, 1))], ShiftSettings(mode="aligned", time_of_day="keep_utc")
    )
    assert plan.runnable
    assert to_utc(utc(2021, 1, 1, 1) + plan.shift_for("a")) == datetime(2000, 1, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------
# Keep the time of day: D on the clock the mode keeps, shift = D + off_ref


@pytest.mark.parametrize(
    ("zone", "wall", "offset_h"),
    [
        ("Europe/Prague", (2021, 1, 15, 8, 30, 15, 123456), 1),  # winter
        ("Europe/Prague", (2021, 7, 15, 8, 30, 15, 123456), 2),  # summer
        ("America/Chicago", (2021, 1, 15, 8, 30), -6),
        ("America/Chicago", (2021, 7, 15, 8, 30), -5),
        ("Australia/Sydney", (2021, 1, 15, 8, 30), 11),  # southern summer
        ("Australia/Sydney", (2021, 7, 15, 8, 30), 10),
        ("Asia/Kolkata", (2021, 7, 15, 8, 30), 5.5),
        ("Asia/Kathmandu", (2021, 7, 15, 8, 30), 5.75),
        ("Australia/Lord_Howe", (2021, 1, 15, 8, 30), 11),  # half-hour DST
        ("Australia/Lord_Howe", (2021, 7, 15, 8, 30), 10.5),
        ("Pacific/Chatham", (2021, 1, 15, 8, 30), 13.75),
    ],
)
def test_keep_local_puts_the_wall_clock_on_the_copy_utc_clock(zone, wall, offset_h):
    start = local(zone, *wall)
    plan = plan_shifts([rec("a", start)], keep_local(zone))
    assert plan.runnable, plan.refusals
    assert plan.reference_offset_us == int(offset_h * HOUR)  # never rounded
    assert plan.days == (date(2000, 1, 1) - date(*wall[:3])).days
    assert plan.shift_for("a") == plan.days * DAY_US + plan.reference_offset_us
    copy = to_utc(start + plan.shift_for("a"))
    assert copy.replace(tzinfo=None) == datetime(2000, 1, 1, *wall[3:])


def test_d_is_measured_on_the_clock_the_mode_keeps_near_midnight():
    # 21:00 in Chicago on 4 July is 02:00 UTC on 5 July: the two dates differ.
    start = local("America/Chicago", 2021, 7, 4, 21, 0)
    assert to_utc(start).date() == date(2021, 7, 5)
    kept_local = plan_shifts([rec("a", start)], keep_local("America/Chicago"))
    kept_utc = plan_shifts(
        [rec("a", start)],
        ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC, site_zone="America/Chicago"),
    )
    assert kept_local.days == kept_utc.days + 1
    # Either way, the copy lands on the chosen date.
    assert to_utc(start + kept_local.shift_for("a")) == datetime(2000, 1, 1, 21, tzinfo=UTC)
    assert to_utc(start + kept_utc.shift_for("a")) == datetime(2000, 1, 1, 2, tzinfo=UTC)
    assert kept_utc.reference_offset_us is None


def test_keep_utc_shifts_by_whole_days_and_needs_no_zone():
    start = utc(2019, 11, 3, 23, 59, 59, 999999)
    plan = plan_shifts([rec("a", start)], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC))
    assert plan.shift_for("a") % DAY_US == 0
    assert to_utc(start + plan.shift_for("a")) == datetime(
        2000, 1, 1, 23, 59, 59, 999999, tzinfo=UTC
    )


def test_keep_local_with_utc_as_the_site_zone_equals_keep_utc():
    start = utc(2019, 11, 3, 23, 15)
    a = plan_shifts([rec("a", start)], keep_local("UTC"))
    b = plan_shifts([rec("a", start)], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC))
    assert a.shift_for("a") == b.shift_for("a") and a.reference_offset_us == 0


def test_a_new_date_after_the_original_shifts_forward():
    start = local("Europe/Prague", 1995, 6, 1, 10)
    plan = plan_shifts([rec("a", start)], keep_local("Europe/Prague", new_date=date(2030, 2, 2)))
    assert plan.shift_for("a") > 0
    assert to_utc(start + plan.shift_for("a")) == datetime(2030, 2, 2, 10, tzinfo=UTC)


# --------------------------------------------------------------------------
# Set the time of day too


def test_set_time_starts_the_reference_copy_at_the_chosen_utc_moment():
    start = local("Europe/Prague", 2021, 7, 15, 8, 30, 15, 500)
    plan = plan_shifts(
        [rec("a", start), rec("b", start + 5 * HOUR)],
        ShiftSettings(time_of_day=TimeOfDay.SET, new_time=time(13, 45)),
    )
    assert plan.runnable and plan.days is None and plan.reference_offset_us is None
    assert to_utc(start + plan.shift_for("a")) == datetime(2000, 1, 1, 13, 45, tzinfo=UTC)
    assert plan.shift_for("b") == plan.shift_for("a")
    assert not plan.warnings


def test_set_time_keep_weekday_uses_the_site_date():
    # Sunday 23:30 in Chicago is Monday in UTC; the user's weekday is Sunday.
    start = local("America/Chicago", 2021, 7, 4, 23, 30)
    plan = plan_shifts(
        [rec("a", start)],
        ShiftSettings(time_of_day=TimeOfDay.SET, keep_weekday=True, site_zone="America/Chicago"),
    )
    assert plan.new_date.weekday() == date(2021, 7, 4).weekday() == 6
    assert plan.new_date == date(2000, 1, 2)
    without_zone = plan_shifts(
        [rec("a", start)], ShiftSettings(time_of_day=TimeOfDay.SET, keep_weekday=True)
    )
    assert without_zone.new_date.weekday() == 0  # Monday, the UTC date


# --------------------------------------------------------------------------
# Keep the weekday: only D moves, off_ref never rounded


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        # 2000-01-01 is a Saturday.
        (date(2021, 1, 2), date(2000, 1, 1)),  # Saturday: unchanged
        (date(2021, 1, 4), date(2000, 1, 3)),  # Monday: +2 beats -5
        (date(2021, 1, 5), date(2000, 1, 4)),  # Tuesday: +3 beats -4
        (date(2021, 1, 6), date(1999, 12, 29)),  # Wednesday: -3 beats +4
        (date(2021, 1, 8), date(1999, 12, 31)),  # Friday: -1
    ],
)
def test_keep_weekday_picks_the_nearest_same_weekday(original, expected):
    start = local("Asia/Kolkata", *original.timetuple()[:3], 9, 15)
    with_weekday = plan_shifts([rec("a", start)], keep_local("Asia/Kolkata", keep_weekday=True))
    plain = plan_shifts([rec("a", start)], keep_local("Asia/Kolkata"))
    assert with_weekday.new_date == expected
    assert with_weekday.days % 7 == 0
    assert with_weekday.reference_offset_us == plain.reference_offset_us == 5.5 * HOUR
    assert with_weekday.shift_for("a") - plain.shift_for("a") == (
        (expected - date(2000, 1, 1)).days * DAY_US
    )
    copy = to_utc(start + with_weekday.shift_for("a"))
    assert copy.weekday() == original.weekday()
    assert (copy.hour, copy.minute) == (9, 15)


def test_keep_weekday_in_keep_utc_uses_the_utc_weekday():
    start = local("America/Chicago", 2021, 7, 4, 23, 30)  # Sunday local, Monday UTC
    plan = plan_shifts(
        [rec("a", start)], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC, keep_weekday=True)
    )
    assert plan.days % 7 == 0
    assert to_utc(start + plan.shift_for("a")).weekday() == 0


# --------------------------------------------------------------------------
# Relative and aligned


def test_relative_keeps_every_gap_and_overlap_exactly():
    zone = "Europe/Prague"
    starts = [
        local(zone, 2021, 1, 10, 8),
        local(zone, 2021, 1, 10, 9),  # overlaps the first
        local(zone, 2021, 7, 3, 23, 50),
        local(zone, 2021, 12, 31, 23),
    ]
    recs = [rec(f"r{i}", s, hours=3) for i, s in enumerate(starts)]
    plan = plan_shifts(recs, keep_local(zone))
    assert plan.runnable
    shifts = {p.shift_us for p in plan.recordings}
    assert len(shifts) == 1
    new = [p.new_start_us for p in plan.recordings]
    for i in range(len(starts)):
        for j in range(len(starts)):
            assert new[i] - new[j] == starts[i] - starts[j]
    assert not warned(plan, WarningCode.ALIGNED_OVERLAP)


def test_aligned_starts_every_recording_at_the_reference_copy_start():
    zone = "Australia/Sydney"
    recs = [
        rec("a", local(zone, 2015, 3, 1, 7), hours=10),
        rec("b", local(zone, 2018, 8, 9, 22), hours=1),
        rec("c", local(zone, 2012, 1, 2, 3), hours=30),
        rec("empty", local(zone, 2016, 1, 2, 3), hours=0),
    ]
    plan = plan_shifts(recs, keep_local(zone, mode=Mode.ALIGNED))
    assert plan.reference_id == "c" and plan.runnable, plan.refusals
    target = datetime(2000, 1, 1, 3, tzinfo=UTC)
    assert {to_utc(p.new_start_us) for p in plan.recordings} == {target}
    overlaps = warned(plan, WarningCode.ALIGNED_OVERLAP)
    assert overlaps["a"].overlaps == ("b", "c")
    assert overlaps["b"].overlaps == ("a", "c")
    assert overlaps["c"].overlaps == ("a", "b")
    assert "empty" not in overlaps  # a recording with no length overlaps nothing
    assert not warned(plan, WarningCode.OTHER_SEASON)


def test_a_single_aligned_recording_has_no_overlap():
    plan = plan_shifts(
        [rec("a", utc(2020, 1, 1))],
        ShiftSettings(mode=Mode.ALIGNED, time_of_day=TimeOfDay.KEEP_UTC),
    )
    assert plan.runnable and not plan.warnings


# --------------------------------------------------------------------------
# The leak-scan floor and the range of shifted times


def _floor_batch():
    # Spans exactly 9 days, so the floor is exactly 10 days.
    x = utc(2020, 3, 1)
    return [
        RecordingTimes("a", x, x + 2 * HOUR),
        RecordingTimes("b", x + 8 * DAY_US, x + 9 * DAY_US),
    ]


@pytest.mark.parametrize(
    ("new_date", "refused"),
    [
        (date(2020, 3, 11), False),  # +10 days = the floor
        (date(2020, 3, 10), True),
        (date(2020, 2, 20), False),  # -10 days
        (date(2020, 2, 21), True),
        (date(2020, 3, 1), True),  # no shift at all
    ],
)
def test_relative_refuses_a_shift_below_span_plus_one_day(new_date, refused):
    plan = plan_shifts(
        _floor_batch(), ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC, new_date=new_date)
    )
    assert plan.batch_span_us == 9 * DAY_US and plan.floor_us == 10 * DAY_US
    if refused:
        assert codes(plan.refusals) == [RefusalCode.BELOW_FLOOR]
        refusal = plan.refusals[0]
        assert refusal.recording_id is None and refusal.limit_us == 10 * DAY_US
        assert not plan.runnable
        assert plan.recordings  # still previewable
    else:
        assert plan.runnable


def test_the_floor_counts_the_whole_batch_not_one_recording():
    # Each recording is short; the batch spans 30 days, so a 20-day shift is refused.
    x = utc(2020, 3, 1)
    recs = [rec("a", x), rec("b", x + 30 * DAY_US)]
    plan = plan_shifts(
        recs, ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC, new_date=date(2020, 3, 21))
    )
    assert codes(plan.refusals) == [RefusalCode.BELOW_FLOOR]


def test_aligned_applies_the_floor_to_each_recordings_own_shift():
    recs = [
        rec("old", utc(1990, 6, 1, 10)),
        rec("near", utc(2000, 1, 3, 10)),  # its own shift is -2 days
        rec("new", utc(2010, 6, 1, 10)),
    ]
    plan = plan_shifts(recs, ShiftSettings(mode=Mode.ALIGNED, time_of_day=TimeOfDay.KEEP_UTC))
    floor = plan.floor_us
    assert floor == recs[2].end_us - recs[0].start_us + DAY_US
    below = [r for r in plan.refusals if r.code is RefusalCode.BELOW_FLOOR]
    assert {r.recording_id for r in below} == {
        p.id for p in plan.recordings if abs(p.shift_us) < floor
    }
    assert "near" in {r.recording_id for r in below}
    # A copy far from every original passes, each with its own shift.
    far = [rec("x", utc(2010, 6, 1, 10)), rec("y", utc(2011, 2, 1, 23))]
    plan = plan_shifts(far, ShiftSettings(mode=Mode.ALIGNED, time_of_day=TimeOfDay.KEEP_UTC))
    assert plan.runnable and len({p.shift_us for p in plan.recordings}) == 2


def test_shifted_times_must_stay_well_above_zero():
    zone = "America/Chicago"
    recs = [rec("ref", local(zone, 2021, 6, 1, 9)), rec("earlier", local(zone, 2020, 6, 1, 9))]
    plan = plan_shifts(recs, keep_local(zone, reference_id="ref", new_date=date(1970, 6, 1)))
    too_early = [r.recording_id for r in plan.refusals if r.code is RefusalCode.TOO_EARLY]
    assert too_early == ["earlier"]
    early = next(p for p in plan.recordings if p.id == "earlier")
    assert early.new_start_us < MIN_SHIFTED_US
    # 1970-01-01 itself is below the margin for the reference too.
    plan = plan_shifts(recs[:1], keep_local(zone, new_date=date(1970, 1, 1)))
    assert codes(plan.refusals) == [RefusalCode.TOO_EARLY]
    # A day later is fine.
    plan = plan_shifts(
        recs[:1], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC, new_date=date(1970, 1, 2))
    )
    assert plan.runnable


def test_shifted_times_past_the_last_date_are_refused_not_crashing():
    a = rec("a", utc(2021, 6, 1, 9), hours=24 * 5)
    long = rec("long", utc(2021, 6, 1, 9), hours=24 * 40)
    plan = plan_shifts(
        [long], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC, new_date=date(9999, 12, 1))
    )
    assert codes(plan.refusals) == [RefusalCode.TOO_LATE]
    assert plan.refusals[0].recording_id == "long"
    plan = plan_shifts(
        [a], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC, new_date=date(9999, 12, 1))
    )
    assert plan.runnable
    # New dates outside the accepted range: one batch-level refusal, never a crash.
    for new_date in (date.max, date(9999, 12, 2), date(1969, 12, 31), date.min):
        for tod in TimeOfDay:
            plan = plan_shifts(
                [a],
                ShiftSettings(
                    time_of_day=tod,
                    new_date=new_date,
                    keep_weekday=True,
                    site_zone="Pacific/Kiritimati",
                ),
            )
            assert codes(plan.refusals) == [RefusalCode.NEW_DATE_OUT_OF_RANGE]
            assert plan.refusals[0].recording_id is None and not plan.runnable


def test_a_relative_shift_too_late_for_a_later_recording_only():
    recs = [rec("ref", utc(2021, 6, 1, 9)), rec("later", utc(2021, 7, 30, 9))]
    plan = plan_shifts(
        recs, ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC, new_date=date(9999, 12, 1))
    )
    assert [(r.code, r.recording_id) for r in plan.refusals] == [(RefusalCode.TOO_LATE, "later")]
    assert plan.recordings  # still previewable


# --------------------------------------------------------------------------
# Daylight-saving crossings


@pytest.mark.parametrize(
    ("zone", "change_utc", "change_h"),
    [
        ("America/Chicago", (2021, 3, 14, 8), 1),  # 02:00 CST -> 03:00 CDT
        ("America/Chicago", (2021, 11, 7, 7), -1),  # 02:00 CDT -> 01:00 CST
        ("Europe/Prague", (2021, 3, 28, 1), 1),
        ("Europe/Prague", (2021, 10, 31, 1), -1),
        ("Australia/Sydney", (2021, 4, 3, 16), -1),  # southern autumn
        ("Australia/Sydney", (2021, 10, 2, 16), 1),
        ("Australia/Lord_Howe", (2021, 4, 3, 15), -0.5),  # half-hour DST
        ("Australia/Lord_Howe", (2021, 10, 2, 15, 30), 0.5),
    ],
)
def test_a_recording_crossing_a_clock_change_is_warned_exactly(zone, change_utc, change_h):
    change = utc(*change_utc)
    start = change - 3 * HOUR - 17 * MINUTE
    for tod in (TimeOfDay.KEEP_LOCAL, TimeOfDay.KEEP_UTC, TimeOfDay.SET):
        for mode in Mode:
            plan = plan_shifts(
                [RecordingTimes("a", start, change + 2 * HOUR)],
                ShiftSettings(site_zone=zone, time_of_day=tod, mode=mode),
            )
            (warning,) = warned(plan, WarningCode.DST_CROSSING).values()
            (c,) = warning.clock_changes
            assert c.after_us == change - start
            assert c.change_us == int(change_h * HOUR)
    # Ending just before the change: no warning.
    plan = plan_shifts([RecordingTimes("a", start, change - 1)], keep_local(zone))
    assert not warned(plan, WarningCode.DST_CROSSING)


def test_a_recording_ending_exactly_at_the_change_crosses_it():
    change = utc(2021, 3, 14, 8)
    plan = plan_shifts([RecordingTimes("a", change - HOUR, change)], keep_local("America/Chicago"))
    assert warned(plan, WarningCode.DST_CROSSING)


def test_a_long_recording_lists_every_change_even_when_they_cancel_out():
    zone = "Europe/Prague"
    start, end = utc(2021, 2, 1), utc(2021, 12, 1)
    assert to_local(start, zone).utcoffset() == to_local(end, zone).utcoffset()
    changes = clock_changes(start, end, ZoneInfo(zone))
    assert [start + c.after_us for c in changes] == [utc(2021, 3, 28, 1), utc(2021, 10, 31, 1)]
    assert [c.change_us for c in changes] == [HOUR, -HOUR]


def test_no_clock_change_warning_without_a_site_zone_or_in_a_zone_without_dst():
    start, end = utc(2021, 3, 1), utc(2021, 12, 1)
    plan = plan_shifts(
        [RecordingTimes("a", start, end)], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC)
    )
    assert not plan.warnings
    plan = plan_shifts([RecordingTimes("a", start, end)], keep_local("Asia/Kolkata"))
    assert not plan.warnings


# --------------------------------------------------------------------------
# Other-season recordings in relative keep-local mode (#5 acceptance)


def test_relative_batch_in_both_seasons_warns_for_exactly_the_other_season():
    zone = "Europe/Prague"
    recs = [
        rec("ref", local(zone, 2021, 1, 11, 12)),  # winter, the reference
        rec("winter", local(zone, 2021, 2, 15, 12)),
        rec("summer_noon", local(zone, 2021, 7, 14, 12)),
        rec("summer_00_30", local(zone, 2021, 7, 15, 0, 30)),  # flips the day
        rec("winter_late", local(zone, 2021, 11, 20, 23, 30)),
    ]
    plan = plan_shifts(recs, keep_local(zone))
    assert plan.runnable, plan.refusals
    other = warned(plan, WarningCode.OTHER_SEASON)
    assert set(other) == {"summer_noon", "summer_00_30"}
    # Summer is +2, the reference +1: the copy shows one hour earlier.
    assert other["summer_noon"].time_of_day_off_us == -HOUR
    assert not other["summer_noon"].weekday_flips
    assert other["summer_00_30"].day_shift == -1 and other["summer_00_30"].weekday_flips
    # Check against the copy itself: 00:30 on the 15th (a Thursday) becomes
    # 23:30 the day before on the copy's clock.
    p = next(p for p in plan.recordings if p.id == "summer_00_30")
    copy = to_utc(p.new_start_us)
    assert (copy.hour, copy.minute) == (23, 30)
    assert copy.date() == date(2021, 7, 15) + timedelta(days=plan.days - 1)


@pytest.mark.parametrize(
    ("ref_wall", "other_wall", "off_h", "day_shift"),
    [
        # Southern hemisphere: the reference in winter (+10), the other in summer (+11).
        ((2021, 7, 5, 12), (2021, 1, 20, 23, 30), -1, 0),
        ((2021, 7, 5, 12), (2021, 1, 20, 0, 15), -1, -1),
        # The reference in summer (+11), the other in winter (+10): one hour later.
        ((2021, 1, 5, 12), (2021, 7, 20, 23, 30), 1, 1),
        ((2021, 1, 5, 12), (2021, 7, 20, 22, 30), 1, 0),
    ],
)
def test_other_season_in_the_southern_hemisphere(ref_wall, other_wall, off_h, day_shift):
    zone = "Australia/Sydney"
    recs = [rec("ref", local(zone, *ref_wall)), rec("other", local(zone, *other_wall))]
    plan = plan_shifts(recs, keep_local(zone, reference_id="ref", new_date=date(1990, 5, 5)))
    assert plan.runnable, plan.refusals
    (w,) = warned(plan, WarningCode.OTHER_SEASON).values()
    assert w.recording_id == "other"
    assert w.time_of_day_off_us == off_h * HOUR
    assert w.day_shift == day_shift
    copy = to_utc(next(p.new_start_us for p in plan.recordings if p.id == "other"))
    wall = datetime(*other_wall)
    assert copy.replace(tzinfo=None) == wall + timedelta(days=plan.days, hours=off_h)


def test_half_hour_other_season_offset():
    zone = "Australia/Lord_Howe"
    recs = [rec("ref", local(zone, 2021, 7, 5, 12)), rec("summer", local(zone, 2021, 1, 9, 0, 10))]
    plan = plan_shifts(recs, keep_local(zone, reference_id="ref"))
    (w,) = warned(plan, WarningCode.OTHER_SEASON).values()
    assert w.time_of_day_off_us == -30 * MINUTE and w.day_shift == -1


def test_other_season_is_only_a_relative_keep_local_warning():
    zone = "Europe/Prague"
    recs = [
        rec("ref", local(zone, 2021, 1, 11, 12)),
        rec("summer", local(zone, 2021, 7, 15, 0, 30)),
    ]
    for settings in (
        keep_local(zone, mode=Mode.ALIGNED),
        ShiftSettings(site_zone=zone, time_of_day=TimeOfDay.KEEP_UTC),
        ShiftSettings(site_zone=zone, time_of_day=TimeOfDay.SET),
    ):
        assert not warned(plan_shifts(recs, settings), WarningCode.OTHER_SEASON)


def test_a_zone_rule_change_between_recordings_is_also_an_offset_difference():
    # Samoa skipped 30 Dec 2011 and moved from -10 (DST) to +14 (DST).
    zone = "Pacific/Apia"
    recs = [rec("ref", local(zone, 2011, 12, 1, 12)), rec("after", local(zone, 2012, 1, 5, 12))]
    plan = plan_shifts(recs, keep_local(zone))
    (w,) = warned(plan, WarningCode.OTHER_SEASON).values()
    assert w.time_of_day_off_us == -24 * HOUR
    assert w.day_shift == -1


# --------------------------------------------------------------------------
# Blockers


def test_blocked_recordings_are_left_out_of_the_plan_and_the_span():
    t = utc(2021, 6, 1, 9)
    recs = [
        rec("ok", t),
        rec("unverified", t - 3000 * DAY_US, times_verified=False),
        rec("locked", t - 2000 * DAY_US, locked=True),
        rec("both", t - 1000 * DAY_US, locked=True, times_verified=False),
        RecordingTimes("no_times", None, None),
        RecordingTimes("backwards", t, t - 1),
        RecordingTimes("zero", 0, HOUR),
        RecordingTimes("huge", t, MAX_TIME_US + 1),
    ]
    plan = plan_shifts(recs, ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC))
    assert plan.runnable
    assert [p.id for p in plan.recordings] == ["ok"]
    assert plan.reference_id == "ok"  # not the earlier, blocked ones
    assert plan.batch_span_us == 2 * HOUR
    blocked = {b.id: b.reasons for b in plan.blocked}
    assert blocked == {
        "unverified": (Blocker.TIMES_UNVERIFIED,),
        "locked": (Blocker.LOCKED,),
        "both": (Blocker.LOCKED, Blocker.TIMES_UNVERIFIED),
        "no_times": (Blocker.TIMES_INVALID,),
        "backwards": (Blocker.TIMES_INVALID,),
        "zero": (Blocker.TIMES_INVALID,),
        "huge": (Blocker.TIMES_INVALID,),
    }


def test_a_blocked_reference_or_an_all_blocked_batch_is_refused():
    t = utc(2021, 6, 1, 9)
    recs = [rec("ok", t), rec("bad", t - DAY_US, times_verified=False)]
    plan = plan_shifts(recs, ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC, reference_id="bad"))
    assert codes(plan.refusals) == [RefusalCode.REFERENCE_BLOCKED]
    assert plan.refusals[0].recording_id == "bad"
    plan = plan_shifts(recs[1:], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC))
    assert codes(plan.refusals) == [RefusalCode.NOTHING_TO_PLAN]
    plan = plan_shifts([], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC))
    assert codes(plan.refusals) == [RefusalCode.NOTHING_TO_PLAN] and not plan.runnable


def _details(rto, access):
    t = utc(2021, 6, 1, 9)
    channels = (
        ChannelInfo("a", 256.0, "uV", 10, t, t + HOUR),
        ChannelInfo("b", 256.0, "uV", 10, t - 5, t + 2 * HOUR),
    )
    return RecordingDetails(
        location="x.mefd", channels=channels, recording_time_offset=rto, access=access
    )


@pytest.mark.parametrize(
    ("rto", "access", "reasons"),
    [
        (0, ACCESS_OPEN, ()),
        (0, ACCESS_FULL, ()),
        (123, ACCESS_FULL, (Blocker.TIMES_UNVERIFIED,)),
        # Locked: the password is the one thing to fix, so one code only.
        (None, ACCESS_BASIC, (Blocker.LOCKED,)),
        # Not locked, but the offset could not be read: unverified.
        (None, ACCESS_FULL, (Blocker.TIMES_UNVERIFIED,)),
        (123, ACCESS_BASIC, (Blocker.LOCKED, Blocker.TIMES_UNVERIFIED)),
    ],
)
def test_explorer_details_become_planner_input(rto, access, reasons):
    times = recording_times(_details(rto, access), "id-1")
    assert times.start_us == utc(2021, 6, 1, 9) - 5 and times.end_us == utc(2021, 6, 1, 11)
    plan = plan_shifts([times], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC))
    if reasons:
        assert plan.blocked[0].reasons == reasons
    else:
        assert plan.runnable


def test_a_recording_without_channels_is_blocked():
    details = RecordingDetails(location="x.mefd", channels=())
    plan = plan_shifts([recording_times(details, "x")], ShiftSettings(time_of_day="keep_utc"))
    assert plan.blocked[0].reasons == (Blocker.TIMES_INVALID,)


# --------------------------------------------------------------------------
# Privacy and purity


def test_no_original_time_or_shift_appears_in_any_repr():
    start = local("Europe/Prague", 2021, 7, 15, 0, 30)
    recs = [rec("ref", local("Europe/Prague", 2021, 1, 11, 12)), rec("s", start)]
    plan = plan_shifts(recs, keep_local("Europe/Prague", new_date=date(2021, 1, 15)))
    assert plan.refusals  # below the floor: the refusal carries the shift
    text = repr(plan) + repr(recs) + repr(plan.recordings) + repr(plan.refusals)
    secrets = {r.start_us for r in recs} | {r.end_us for r in recs}
    secrets |= {p.shift_us for p in plan.recordings} | {p.new_start_us for p in plan.recordings}
    # off_ref equals the size of the season difference here, so check by name.
    for name in ("reference_offset_us", "days=", "new_date=", "shift_us", "start_us", "end_us"):
        assert name not in text
    original_date = plan.new_date - timedelta(days=plan.days)
    assert original_date == date(2021, 1, 11)
    for value in secrets:
        assert str(value) not in text
    assert repr(original_date) not in text
    assert "2021" not in text
    assert "Prague" not in text  # the site zone is a location hint


def test_clock_changes_and_settings_hide_their_details_in_repr():
    zone = "America/Chicago"
    change = utc(2021, 3, 14, 8)
    plan = plan_shifts(
        [RecordingTimes("a", change - 7 * HOUR, change + HOUR)],
        keep_local(zone, keep_weekday=True),
    )
    (warning,) = plan.warnings
    (c,) = warning.clock_changes
    text = repr(plan) + repr(plan.warnings)
    for value in (c.after_us, c.offset_before_us, c.offset_after_us, plan.new_date.isoformat()):
        assert str(value) not in text
    assert "Chicago" not in text
    # Other-season details hint at the season and a start near midnight.
    zone = "Europe/Prague"
    recs = [rec("ref", local(zone, 2021, 1, 11, 12)), rec("s", local(zone, 2021, 7, 15, 0, 30))]
    text = repr(plan_shifts(recs, keep_local(zone)).warnings)
    assert "time_of_day_off_us" not in text and "day_shift" not in text


def test_the_planner_module_logs_nothing_and_imports_no_mef3io():
    tree = ast.parse(Path(shiftplan.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print":
            pytest.fail("print() in the planner")
    assert not imported & {"logging", "mef3io", "PySide6"}


# --------------------------------------------------------------------------
# Property-style: random batches, every rule checked from first principles

ZONES = [
    "UTC",
    "America/Chicago",
    "America/St_Johns",
    "Europe/Prague",
    "Australia/Sydney",
    "Australia/Lord_Howe",
    "Asia/Kolkata",
    "Asia/Kathmandu",
    "Pacific/Chatham",
    "America/Santiago",
]


def _random_case(rng: random.Random):
    zone = rng.choice(ZONES)
    base = utc(2005, 1, 1) + rng.randrange(0, 15 * 365) * DAY_US
    recs = []
    for i in range(rng.randint(1, 6)):
        start = base + rng.randrange(0, 400 * DAY_US)
        recs.append(RecordingTimes(f"r{i}", start, start + rng.randrange(0, 3 * DAY_US)))
    roll = rng.random()
    if roll < 0.3:  # near the originals: below the floor some of the time
        new_date = to_utc(base).date() + timedelta(days=rng.randrange(-800, 1200))
    elif roll < 0.4:  # right at the start of 1970: too early
        new_date = date(1970, 1, 1) + timedelta(days=rng.randrange(0, 3))
    else:
        new_date = date(1975, 1, 1) + timedelta(days=rng.randrange(0, 365 * 20))
    settings = ShiftSettings(
        mode=rng.choice(list(Mode)),
        time_of_day=rng.choice(list(TimeOfDay)),
        new_date=new_date,
        new_time=time(rng.randrange(24), rng.randrange(60)),
        site_zone=zone,
        keep_weekday=rng.random() < 0.5,
        reference_id=rng.choice([None, *[r.id for r in recs]]),
    )
    return zone, recs, settings


@pytest.mark.parametrize("seed", range(40))
def test_random_batches_follow_every_rule(seed):
    _check_random_batches(seed, set())


def test_random_batches_reach_every_outcome():
    seen: set = set()
    for seed in range(40):
        _check_random_batches(seed, seen)
    assert {RefusalCode.BELOW_FLOOR, RefusalCode.TOO_EARLY, "runnable", "refused"} <= seen


def _check_random_batches(seed, seen):
    rng = random.Random(seed)
    for _ in range(25):
        zone, recs, s = _random_case(rng)
        plan = plan_shifts(recs, s)
        by_id = {r.id: r for r in recs}
        ref = by_id[plan.reference_id]
        if s.reference_id is None:
            assert ref.start_us == min(r.start_us for r in recs)
        ref_shift = plan.shift_for(ref.id)
        z = ZoneInfo(zone)

        # The reference copy: on the new date at the kept / chosen time of day.
        copy = to_utc(ref.start_us + ref_shift)
        if s.time_of_day is TimeOfDay.KEEP_LOCAL:
            wall = to_local(ref.start_us, zone)
            assert copy.replace(tzinfo=None) == datetime.combine(plan.new_date, wall.time())
            assert plan.reference_offset_us == wall.utcoffset() // timedelta(microseconds=1)
            assert plan.days == (plan.new_date - wall.date()).days
            assert ref_shift == plan.days * DAY_US + plan.reference_offset_us
            original_day = wall.date()
        elif s.time_of_day is TimeOfDay.KEEP_UTC:
            assert copy.time() == to_utc(ref.start_us).time()
            assert copy.date() == plan.new_date
            assert ref_shift % DAY_US == 0
            original_day = to_utc(ref.start_us).date()
        else:
            assert copy == datetime.combine(plan.new_date, s.new_time, UTC)
            original_day = to_local(ref.start_us, zone).date()
        if s.keep_weekday:
            assert plan.new_date.weekday() == original_day.weekday()
            assert abs((plan.new_date - s.new_date).days) <= 3
            if plan.days is not None:
                assert plan.days % 7 == 0
        else:
            assert plan.new_date == s.new_date

        # The mode.
        if s.mode is Mode.RELATIVE:
            assert {p.shift_us for p in plan.recordings} == {ref_shift}
        else:
            assert {p.new_start_us for p in plan.recordings} == {ref.start_us + ref_shift}

        # Refusals: exactly the floor and range rules.
        span = max(r.end_us for r in recs) - min(r.start_us for r in recs)
        expected = set()
        for p in plan.recordings:
            if abs(p.shift_us) < span + DAY_US:
                expected.add((RefusalCode.BELOW_FLOOR, p.id if s.mode is Mode.ALIGNED else None))
            if p.new_start_us < MIN_SHIFTED_US:
                expected.add((RefusalCode.TOO_EARLY, p.id))
            if p.new_end_us > MAX_TIME_US:
                expected.add((RefusalCode.TOO_LATE, p.id))
        got = {(r.code, r.recording_id) for r in plan.refusals}
        assert got == expected
        assert plan.runnable == (not expected)
        seen.update(code for code, _ in got)
        seen.add("runnable" if plan.runnable else "refused")

        # Warnings.
        other = warned(plan, WarningCode.OTHER_SEASON)
        dst = warned(plan, WarningCode.DST_CROSSING)
        for p in plan.recordings:
            r = by_id[p.id]
            off = to_local(r.start_us, zone).utcoffset()
            ref_off = to_local(ref.start_us, zone).utcoffset()
            should_warn = (
                s.mode is Mode.RELATIVE and s.time_of_day is TimeOfDay.KEEP_LOCAL and off != ref_off
            )
            assert (p.id in other) == should_warn
            if should_warn:
                w = other[p.id]
                assert w.time_of_day_off_us == (ref_off - off) // timedelta(microseconds=1)
                kept = to_local(r.start_us, zone).replace(tzinfo=None) + timedelta(days=plan.days)
                shown = to_utc(p.new_start_us).replace(tzinfo=None)
                assert shown - kept == timedelta(microseconds=w.time_of_day_off_us)
                assert w.day_shift == (shown.date() - kept.date()).days
            crosses = to_local(r.start_us, zone).utcoffset() != to_local(r.end_us, zone).utcoffset()
            if crosses:
                assert p.id in dst
            if p.id in dst:
                for c in dst[p.id].clock_changes:
                    t = r.start_us + c.after_us
                    assert to_utc(t - 1).astimezone(z).utcoffset() == timedelta(
                        microseconds=c.offset_before_us
                    )
                    assert to_utc(t).astimezone(z).utcoffset() == timedelta(
                        microseconds=c.offset_after_us
                    )


def test_clock_change_scan_is_fast_for_a_year_long_recording():
    import time as _time

    t0 = _time.perf_counter()
    changes = clock_changes(utc(2020, 1, 1), utc(2021, 1, 1), ZoneInfo("America/Chicago"))
    assert len(changes) == 2
    assert _time.perf_counter() - t0 < 2.0


# --------------------------------------------------------------------------
# Round-1 review additions


@pytest.mark.parametrize(("fold", "offset_h"), [(0, -5), (1, -6)])
def test_a_reference_in_the_repeated_hour_keeps_its_wall_clock(fold, offset_h):
    zone = ZoneInfo("America/Chicago")
    start = us(datetime(2021, 11, 7, 1, 30, fold=fold, tzinfo=zone))
    plan = plan_shifts([rec("a", start)], keep_local("America/Chicago"))
    assert plan.reference_offset_us == offset_h * HOUR
    assert to_utc(start + plan.shift_for("a")) == datetime(2000, 1, 1, 1, 30, tzinfo=UTC)


def test_a_reference_exactly_at_spring_forward_reads_three_oclock():
    start = utc(2021, 3, 14, 8)  # 02:00 CST does not exist; it is 03:00 CDT
    plan = plan_shifts([rec("a", start)], keep_local("America/Chicago"))
    assert plan.reference_offset_us == -5 * HOUR
    assert to_utc(start + plan.shift_for("a")) == datetime(2000, 1, 1, 3, tzinfo=UTC)
    assert not plan.warnings  # it starts after the change


def test_northern_reference_in_summer_flips_a_late_winter_recording_forward():
    zone = "Europe/Prague"
    recs = [
        rec("ref", local(zone, 2021, 7, 1, 12)),
        rec("winter_late", local(zone, 2021, 11, 20, 23, 30)),
        rec("winter_noon", local(zone, 2021, 12, 1, 12)),
    ]
    plan = plan_shifts(recs, keep_local(zone))
    other = warned(plan, WarningCode.OTHER_SEASON)
    assert set(other) == {"winter_late", "winter_noon"}
    assert other["winter_late"].time_of_day_off_us == HOUR
    assert other["winter_late"].day_shift == 1
    assert other["winter_noon"].day_shift == 0
    copy = to_utc(plan.by_id["winter_late"].new_start_us)
    assert (copy.hour, copy.minute) == (0, 30)


def test_st_johns_half_hour_zone_other_season():
    zone = "America/St_Johns"  # -3:30 standard, -2:30 daylight
    recs = [
        rec("ref", local(zone, 2021, 1, 10, 12)),
        rec("summer", local(zone, 2021, 7, 10, 0, 20)),
    ]
    plan = plan_shifts(recs, keep_local(zone))
    assert plan.reference_offset_us == -3.5 * HOUR
    (w,) = warned(plan, WarningCode.OTHER_SEASON).values()
    assert w.time_of_day_off_us == -HOUR and w.day_shift == -1


def test_a_zone_without_dst_never_warns_across_seasons():
    zone = "Asia/Kolkata"
    recs = [
        rec("jan", local(zone, 2021, 1, 10, 23, 50)),
        rec("jul", local(zone, 2021, 7, 10, 0, 5)),
    ]
    for mode in Mode:
        plan = plan_shifts(recs, keep_local(zone, mode=mode))
        assert plan.runnable
        assert not warned(plan, WarningCode.OTHER_SEASON)
        assert not warned(plan, WarningCode.DST_CROSSING)


def test_aligned_refuses_the_reference_itself_below_the_floor():
    recs = [rec("ref", utc(2000, 1, 3, 10)), rec("later", utc(2000, 1, 10, 10))]
    plan = plan_shifts(recs, ShiftSettings(mode=Mode.ALIGNED, time_of_day=TimeOfDay.KEEP_UTC))
    below = {r.recording_id for r in plan.refusals if r.code is RefusalCode.BELOW_FLOOR}
    assert "ref" in below


@pytest.mark.parametrize(("days", "refused"), [(10, False), (9, True)])
def test_aligned_floor_boundary_is_exact(days, refused):
    # Batch spans exactly 9 days; the floor is 10 days. The target is the
    # reference's new start; "b" ends the span and starts at the same time of day.
    x = utc(2020, 3, 1, 6)
    recs = [
        RecordingTimes("a", x, x + HOUR),
        RecordingTimes("b", x + 9 * DAY_US - HOUR, x + 9 * DAY_US),
    ]
    target = to_utc(x + 9 * DAY_US - HOUR + days * DAY_US)
    plan = plan_shifts(
        recs,
        ShiftSettings(
            mode=Mode.ALIGNED,
            time_of_day=TimeOfDay.KEEP_UTC,
            reference_id="b",
            new_date=target.date(),
        ),
    )
    assert plan.floor_us == 10 * DAY_US
    assert plan.by_id["b"].shift_us == days * DAY_US
    below = {r.recording_id for r in plan.refusals if r.code is RefusalCode.BELOW_FLOOR}
    assert ("b" in below) == refused


def test_span_ids_name_the_ends_of_the_batch():
    x = utc(2020, 3, 1)
    recs = [rec("mid", x + DAY_US), rec("first", x), rec("last", x + 30 * DAY_US, hours=5)]
    plan = plan_shifts(recs, ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC))
    assert plan.span_ids == ("first", "last")


def test_an_implausibly_long_recording_is_blocked_and_planning_stays_fast():
    import time as _time

    t = utc(2021, 6, 1)
    recs = [rec("ok", t), RecordingTimes("corrupt", t, MAX_TIME_US)]
    t0 = _time.perf_counter()
    plan = plan_shifts(recs, keep_local("Europe/Prague"))
    assert _time.perf_counter() - t0 < 1.0
    assert plan.blocked[0].id == "corrupt"
    assert plan.blocked[0].reasons == (Blocker.TIMES_INVALID,)
    assert plan.runnable and plan.batch_span_us == 2 * HOUR
    # Ten years is still accepted, and scanned quickly.
    decade = RecordingTimes("decade", t, t + shiftplan.MAX_DURATION_US)
    t0 = _time.perf_counter()
    plan = plan_shifts([decade], keep_local("Europe/Prague", new_date=date(1975, 1, 1)))
    assert _time.perf_counter() - t0 < 2.0
    assert not plan.blocked
    assert len(plan.warnings[0].clock_changes) == 20


def test_a_large_aligned_batch_plans_quickly():
    import time as _time

    t = utc(2010, 1, 1)
    recs = [rec(f"r{i}", t + i * HOUR, hours=1.5) for i in range(600)]
    t0 = _time.perf_counter()
    plan = plan_shifts(recs, ShiftSettings(mode=Mode.ALIGNED, time_of_day=TimeOfDay.KEEP_UTC))
    assert _time.perf_counter() - t0 < 3.0
    assert len(warned(plan, WarningCode.ALIGNED_OVERLAP)["r0"].overlaps) == 599


@pytest.mark.parametrize("name", ["europe/prague", "EUROPE/PRAGUE", "localtime"])
def test_zone_names_must_be_listed_exactly(name):
    plan = plan_shifts([rec("a", utc(2021, 3, 1, 10))], ShiftSettings(site_zone=name))
    assert codes(plan.refusals) == [RefusalCode.UNKNOWN_ZONE]


def test_the_plan_says_whether_a_site_zone_was_used():
    a = [rec("a", utc(2021, 3, 1, 10))]
    assert plan_shifts(a, keep_local("Europe/Prague")).used_site_zone
    assert not plan_shifts(a, ShiftSettings(time_of_day=TimeOfDay.SET)).used_site_zone


def test_clock_changes_accepts_only_integer_times_and_caches_ints():
    zone = ZoneInfo("America/Chicago")
    s, e = utc(2021, 3, 14, 5), utc(2021, 3, 14, 11)
    with pytest.raises(TypeError):
        clock_changes(float(s), float(e), zone)

    class Idx:
        def __init__(self, v):
            self.v = v

        def __index__(self):
            return self.v

    (c,) = clock_changes(Idx(s), Idx(e), zone)
    assert type(c.after_us) is int and c.after_us == 3 * HOUR
    plan = plan_shifts([RecordingTimes("a", s, e)], keep_local("America/Chicago"))
    assert type(plan.warnings[0].clock_changes[0].after_us) is int
