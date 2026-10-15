"""The combined timeline's computation (model/coverage.py) and the table
export (model/export.py): plain Python, no Qt, no files read. Synthetic
details only (made-up channels and times in 2000)."""

from __future__ import annotations

import csv
import io
import os
from datetime import UTC

import pytest

from mef3io_studio.model.coverage import (
    PIXEL_ALL,
    PIXEL_SOME,
    STATUS_LOCKED,
    STATUS_READING,
    STATUS_READY,
    STATUS_UNREADABLE,
    STATUS_WRONG_PASSWORD,
    RecordingInput,
    Stopped,
    combine,
    merge_intervals,
    pixel_runs,
    recording_coverage,
    span_runs,
)
from mef3io_studio.model.export import (
    ExportItem,
    iso_local,
    iso_utc,
    part_table,
    recording_table,
    safe_cell,
    seconds_text,
    write_csv,
)
from mef3io_studio.model.recording import (
    ACCESS_BASIC,
    ChannelInfo,
    Part,
    PatientDetails,
    RecordingDetails,
)
from mef3io_studio.model.timefmt import axis_ticks, format_time, resolve_zone, tick_step

T0 = 946_684_800_000_000  # 2000-01-01 00:00:00 UTC, made up
S = 1_000_000
FS = 256.0


def part(n, start_s, end_s, fs=FS, missing_s=0.0):
    start, end = T0 + int(start_s * S), T0 + int(end_s * S)
    samples = int(round((end_s - start_s - missing_s) * fs))
    return Part(n, start, end, max(0, samples), 1)


def channel(name, *parts, fs=FS):
    return ChannelInfo(
        name,
        fs,
        "uV",
        sum(p.samples for p in parts),
        min((p.start for p in parts), default=T0),
        max((p.end for p in parts), default=T0),
        tuple(parts),
    )


def details(*channels, rto=0, name="rec", patient=None, access="open"):
    return RecordingDetails(
        f"/data/{name}.mefd",
        tuple(channels),
        patient=patient or PatientDetails(),
        access=access,
        recording_time_offset=rto,
    )


def item(name, d, status=STATUS_READY):
    return RecordingInput(name, name, d, status)


# -- one recording -----------------------------------------------------------


def test_channels_counted_where_they_have_data():
    d = details(
        channel("a", part(0, 0, 100), part(1, 200, 300)),
        channel("b", part(0, 0, 100)),
    )
    cov = recording_coverage(item("r", d))
    assert cov.channel_count == 2 and cov.part_count == 3
    steps = list(zip(cov.starts.tolist(), cov.ends.tolist(), cov.counts.tolist(), strict=True))
    assert steps == [(T0, T0 + 100 * S, 2), (T0 + 200 * S, T0 + 300 * S, 1)]
    assert cov.runs == ((T0, T0 + 100 * S), (T0 + 200 * S, T0 + 300 * S))
    assert cov.gaps == ((T0 + 100 * S, T0 + 200 * S),)
    assert cov.data_seconds == 200 and cov.gap_seconds == 100
    assert cov.step_at(T0 + 50 * S) == (2, 0) and cov.step_at(T0 + 250 * S) == (1, 0)
    assert cov.step_at(T0 + 150 * S) is None
    assert cov.run_at(T0 + 250 * S) == (T0 + 200 * S, T0 + 300 * S)
    assert cov.gap_at(T0 + 150 * S) == (T0 + 100 * S, T0 + 200 * S)


def test_gaps_agree_with_the_overview():
    d = details(
        channel("a", part(0, 0, 10), part(1, 10.002, 20), part(2, 40, 50)),
        channel("b", part(0, 0, 20), fs=512.0),
    )
    cov = recording_coverage(item("r", d))
    assert list(cov.gaps) == d.gaps() == [(T0 + 20 * S, T0 + 40 * S)]


def test_overlapping_parts_in_one_channel_count_once():
    d = details(
        channel("a", part(0, 0, 100), part(1, 50, 150), part(2, 60, 70)),
        channel("b", part(0, 0, 150)),
    )
    cov = recording_coverage(item("r", d))
    assert cov.counts.max() == 2  # never more than the number of channels
    assert cov.data_seconds == 150 and cov.gaps == ()


def test_zero_length_and_backwards_parts_are_points():
    d = details(channel("a", part(0, 0, 0), Part(1, T0 + 50 * S, T0 + 40 * S, 0, 1)))
    cov = recording_coverage(item("r", d))
    assert cov.has_data and cov.data_seconds == 0
    assert cov.runs == ((T0, T0), (T0 + 50 * S, T0 + 50 * S))
    assert len(cov.starts) == 0  # no stretch of time with data
    # Still drawn: one pixel column each.
    runs = pixel_runs(cov, T0 - 10 * S, T0 + 90 * S, 100)
    assert [(a, b - a) for a, b, _c, _g in runs] == [(10, 1), (60, 1)]
    layout, _ = combine([item("r", d)])
    assert layout.first == T0 and layout.data_microseconds == 0 and layout.overlaps == ()


def test_parts_with_gaps_inside_are_marked():
    d = details(channel("a", part(0, 0, 100, missing_s=20), part(1, 100, 200)))
    cov = recording_coverage(item("r", d))
    assert cov.parts_with_gaps_inside == 1
    assert cov.step_at(T0 + 50 * S) == (1, 1) and cov.step_at(T0 + 150 * S) == (1, 0)
    runs = pixel_runs(cov, T0, T0 + 200 * S, 200)
    assert runs[0][3] is True and runs[-1][3] is False


def test_partial_coverage_is_drawn_lighter():
    d = details(channel("a", part(0, 0, 200)), channel("b", part(0, 0, 100)))
    cov = recording_coverage(item("r", d))
    runs = pixel_runs(cov, T0, T0 + 200 * S, 200)
    assert [(a, b, c) for a, b, c, _g in runs] == [(0, 100, PIXEL_ALL), (100, 200, PIXEL_SOME)]


def test_many_tiny_parts_are_merged_into_pixels():
    # 20,000 parts of 1 s with 1 s gaps: at 400 px the drawing is bounded
    # by the width, not by the number of parts.
    parts = [part(i, 2 * i, 2 * i + 1) for i in range(20_000)]
    cov = recording_coverage(item("r", details(channel("a", *parts))))
    runs = pixel_runs(cov, T0, T0 + 40_000 * S, 400)
    assert 1 <= len(runs) <= 400
    # Zoomed in, the gaps come back.
    assert len(pixel_runs(cov, T0, T0 + 20 * S, 400)) == 10
    assert pixel_runs(cov, T0 - 10 * S, T0 - 5 * S, 400) == []


def test_rows_without_data_say_why():
    rows = [
        item("reading", None, STATUS_READING),
        item("locked", None, STATUS_LOCKED),
        item("wrong", None, STATUS_WRONG_PASSWORD),
        item("broken", None, STATUS_UNREADABLE),
        item("empty", details(channel("a"))),
    ]
    layout, _ = combine(rows)
    by = {r.key: r for r in layout.rows}
    assert {k: by[k].status for k in by} == {
        "reading": STATUS_READING,
        "locked": STATUS_LOCKED,
        "wrong": STATUS_WRONG_PASSWORD,
        "broken": STATUS_UNREADABLE,
        "empty": STATUS_READY,
    }
    assert not any(r.has_data for r in layout.rows)
    assert layout.first is None and layout.counted == []
    assert len(layout.without_data) == 5


def test_empty_selection():
    layout, cache = combine([])
    assert layout.rows == () and layout.first is None and cache == {}
    assert layout.span_seconds is None and layout.overlap_seconds == 0


# -- across recordings -------------------------------------------------------


def test_overlaps_gaps_and_figures_across_recordings():
    a = details(channel("x", part(0, 0, 100)))
    b = details(channel("x", part(0, 50, 150)))  # shares 50 s with a
    c = details(channel("x", part(0, 300, 400)))  # a gap of 150 s before it
    d = details(channel("x", part(0, 400, 450)))  # touches c: not an overlap
    layout, _ = combine([item("c", c), item("a", a), item("d", d), item("b", b)])
    assert [r.key for r in layout.rows] == ["a", "b", "c", "d"]
    assert layout.first == T0 and layout.last == T0 + 450 * S
    assert layout.span_seconds == 450
    assert layout.data_seconds == 300  # 0-150 and 300-450
    assert layout.gaps_between == ((T0 + 150 * S, T0 + 300 * S),)
    assert [(o.first, o.second, o.seconds, o.may_be_copies) for o in layout.overlaps] == [
        ("a", "b", 50, False)
    ]
    assert layout.overlap_spans == ((T0 + 50 * S, T0 + 100 * S),)
    assert layout.overlap_spans_of["a"] == ((T0 + 50 * S, T0 + 100 * S),)
    assert set(layout.overlapping) == {"a", "b"}
    assert span_runs(layout.overlap_spans, T0, T0 + 100 * S, 100) == [(50, 100)]


def test_a_copy_is_flagged():
    a = details(channel("x", part(0, 0, 100), part(1, 200, 300)))
    b = details(channel("x", part(0, 0, 100)))  # all of b is inside a
    layout, _ = combine([item("a", a), item("b", b)])
    assert layout.overlaps[0].may_be_copies
    assert layout.overlaps[0].seconds == 100


def test_unverified_times_are_kept_out_of_the_figures():
    good = details(channel("x", part(0, 0, 100)))
    offset = details(channel("x", part(0, 0, 100)), rto=5 * S)  # deprecated date masking
    unknown = details(channel("x", part(0, 1000, 1100)), rto=None)  # needs the password
    layout, _ = combine([item("offset", offset), item("unknown", unknown), item("good", good)])
    assert [r.key for r in layout.rows] == ["good", "offset", "unknown"]
    assert [r.verified for r in layout.rows] == [True, False, False]
    assert layout.rows[1].uses_time_offset and layout.rows[2].offset_unknown
    # Same times as "good", but not counted: no overlap, no extra span.
    assert layout.overlaps == () and layout.last == T0 + 100 * S
    assert [r.key for r in layout.unverified] == ["offset", "unknown"]


def test_unchanged_recordings_are_not_worked_out_again():
    d1 = details(channel("x", part(0, 0, 100)))
    d2 = details(channel("x", part(0, 0, 100)))
    layout, cache = combine([item("a", d1), item("b", d2)])
    first_a = layout.row("a")
    layout2, cache2 = combine([item("a", d1), item("b", d2)], cache)
    assert layout2.row("a") is first_a
    # New details for "a" (opened again): worked out again.
    layout3, _ = combine([item("a", details(channel("x", part(0, 0, 50)))), item("b", d2)], cache2)
    assert layout3.row("a") is not first_a and layout3.row("a").data_seconds == 50


def test_a_newer_layout_stops_an_older_one():
    items = [item(str(i), details(channel("x", part(0, i, i + 1)))) for i in range(50)]
    with pytest.raises(Stopped):
        combine(items, should_stop=lambda: True)


def test_merge_intervals_with_tolerance():
    s, e = merge_intervals([0, 10, 12, 30], [10, 11, 20, 30], tolerance=1)
    assert list(zip(s.tolist(), e.tolist(), strict=True)) == [(0, 20), (30, 30)]


def test_axis_marks_follow_the_site_time_zone_across_daylight_saving():
    prague = resolve_zone("Europe/Prague")
    # 2024-03-31: clocks go from 02:00 to 03:00 in Prague.
    start = 1_711_839_600_000_000  # 2024-03-31 00:00 +01:00
    marks = axis_ticks(start, start + 6 * 3600 * S, 3600 * S, prague)
    labels = [format_time(m, prague)[11:] for m in marks]
    assert labels[0] == "00:00:00 +01:00" and "03:00:00 +02:00" in labels
    assert all(b > a for a, b in zip(marks, marks[1:], strict=False))
    assert tick_step(24 * 3600 * S, 6) == 6 * 3600 * S


# -- the table export --------------------------------------------------------


def _read(path):
    raw = open(path, "rb").read()
    assert raw.startswith(b"\xef\xbb\xbf")  # a byte-order mark: Excel reads UTF-8
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    return rows[0], [dict(zip(rows[0], r, strict=True)) for r in rows[1:]]


PATIENT = PatientDetails("Quentin", "Zebedee", "MRN-77001234", "Ward 9", "night")


def _items(revealed=False):
    good = details(
        channel("a", part(0, 0, 100), part(1, 200, 300)),
        channel("b", part(0, 0, 100, missing_s=10)),
        patient=PATIENT,
    )
    copy = details(channel("a", part(0, 0, 100)), patient=PATIENT)
    masked = details(channel("a", part(0, 0, 100)), rto=5 * S, patient=PATIENT)
    return [
        ExportItem(item("good", good), "good.mefd", revealed),
        ExportItem(item("copy", copy), "copy.mefd.tar", revealed),
        ExportItem(item("masked", masked), "masked.mefd", revealed),
        ExportItem(item("locked", None, STATUS_LOCKED), "locked.mefd", revealed),
        ExportItem(item("broken", None, STATUS_UNREADABLE), "broken.mefd", revealed),
    ]


def test_recording_table_columns_and_times(tmp_path):
    header, rows = recording_table(_items())
    path = tmp_path / "t.csv"
    write_csv(str(path), header, rows)
    head, out = _read(path)
    assert "start_site_time" not in head  # UTC only until a zone is chosen
    by = {r["recording"]: r for r in out}
    good = by["good"]
    assert good["status"] == "opened" and good["times_verified"] == "yes"
    assert good["start_utc"] == "2000-01-01T00:00:00Z" and good["end_utc"].endswith("Z")
    assert int(good["start_microseconds_utc"]) == T0  # exact round trip
    assert int(good["end_microseconds_utc"]) == T0 + 300 * S
    assert good["duration_seconds"] == "300" and good["channels"] == "2"
    assert good["sampling_rates_hz"] == "256" and good["parts"] == "2"
    assert good["time_with_data_seconds"] == "200"
    assert good["gaps_between_parts"] == "1" and good["gaps_between_parts_seconds"] == "100"
    assert good["parts_with_gaps_inside"] == "1"
    assert good["overlaps_with"] == "copy" and good["overlap_seconds"] == "100"
    assert by["copy"]["file_name"] == "copy.mefd.tar"
    # Unverified times say so, and are never counted as overlapping.
    masked = by["masked"]
    assert masked["times_verified"] == "no" and masked["time_note"].startswith("unverified")
    assert masked["overlaps_with"] == ""
    # Locked and unreadable: a row saying why, no times.
    assert by["locked"]["status"] == "password needed" and by["locked"]["start_utc"] == ""
    assert by["broken"]["status"] == "could not be opened"
    assert by["broken"]["times_verified"] == ""
    assert len(head) == len(set(head))


def test_site_time_columns_carry_the_offset():
    zone = resolve_zone("Europe/Prague")
    header, rows = recording_table(_items(), zone)
    out = dict(zip(header, rows[0], strict=True))
    assert out["site_time_zone"] == "Europe/Prague"
    assert out["start_site_time"] == "2000-01-01T01:00:00+01:00"
    assert out["start_utc"] == "2000-01-01T00:00:00Z"
    assert iso_utc(T0 + 1) == "2000-01-01T00:00:00.000001Z"
    assert iso_local(T0 + 1, UTC) == "2000-01-01T00:00:00.000001+00:00"
    assert seconds_text(1_500_000) == "1.5" and seconds_text(2_000_000) == "2"


def test_patient_details_follow_show():
    header, rows = recording_table(_items(revealed=False))
    text = "\n".join(",".join(map(str, r)) for r in rows)
    for secret in ("Quentin", "Zebedee", "MRN-77001234", "Ward 9", "night"):
        assert secret not in text
    hidden = dict(zip(header, rows[0], strict=True))
    assert hidden["patient_first_name"] == "[hidden]" and hidden["patient_id"] == "[hidden]"
    header, rows = recording_table(_items(revealed=True))
    shown = dict(zip(header, rows[0], strict=True))
    assert shown["patient_first_name"] == "Quentin" and shown["patient_id"] == "MRN-77001234"
    # Show was chosen but only the basic password opened it: locked, not blank.
    basic = details(
        channel("a", part(0, 0, 1)),
        patient=PatientDetails(names_available=False),
        access=ACCESS_BASIC,
    )
    header, rows = recording_table([ExportItem(item("b", basic), "b.mefd", True)])
    assert dict(zip(header, rows[0], strict=True))["patient_first_name"] == "locked"
    # The part table never carries patient details.
    header, _rows = part_table(_items(revealed=True))
    assert not [h for h in header if h.startswith("patient")]


def test_part_table(tmp_path):
    header, rows = part_table(_items(), resolve_zone("Europe/Prague"))
    path = tmp_path / "p.csv"
    write_csv(str(path), header, rows)
    _head, out = _read(path)
    good = [r for r in out if r["recording"] == "good"]
    assert [(r["channel"], r["part"]) for r in good] == [("a", "1"), ("a", "2"), ("b", "1")]
    assert good[2]["gaps_inside"] == "yes" and good[2]["missing_inside_seconds"] == "10"
    assert good[1]["start_utc"] == "2000-01-01T00:03:20Z"
    assert good[1]["start_site_time"] == "2000-01-01T01:03:20+01:00"
    locked = [r for r in out if r["recording"] == "locked"]
    assert len(locked) == 1 and locked[0]["status"] == "password needed"
    assert locked[0]["channel"] == ""


@pytest.mark.parametrize("bad", ["=HYPERLINK(1)", "+1", "-2+3", "@SUM(A1)", "\tx", "\rx"])
def test_formula_injection_is_neutralised(tmp_path, bad):
    assert safe_cell(bad) == "'" + bad
    assert safe_cell(-5) == -5  # a number stays a number
    d = details(channel(bad, part(0, 0, 1)))
    items = [ExportItem(RecordingInput("k", bad, d), bad + ".mefd")]
    for table in (recording_table, part_table):
        header, rows = table(items)
        path = tmp_path / "x.csv"
        write_csv(str(path), header, rows)
        _head, out = _read(path)
        assert out[0]["recording"] == "'" + bad and out[0]["file_name"] == "'" + bad + ".mefd"
        if table is part_table:
            assert out[0]["channel"] == "'" + bad


def test_quoting_round_trips(tmp_path):
    name = 'a, "b"\nc ü'
    d = details(channel("x", part(0, 0, 1)))
    header, rows = recording_table([ExportItem(RecordingInput("k", name, d), "f.mefd")])
    path = tmp_path / "q.csv"
    write_csv(str(path), header, rows)
    _head, out = _read(path)
    assert out[0]["recording"] == name


def test_a_stopped_or_failed_write_leaves_nothing_behind(tmp_path):
    from mef3io_studio.model.cancel import JobCancelled

    path = tmp_path / "keep.csv"
    path.write_text("old")
    with pytest.raises(JobCancelled):
        write_csv(str(path), ["a"], [[1]] * 5000, should_stop=lambda: True)
    assert path.read_text() == "old"
    assert os.listdir(tmp_path) == ["keep.csv"]

    class Boom:
        def __iter__(self):
            raise OSError("disk full")

    with pytest.raises(OSError):
        write_csv(str(path), ["a"], [Boom()])
    assert path.read_text() == "old" and os.listdir(tmp_path) == ["keep.csv"]
    write_csv(str(path), ["a"], [[1], [2]])
    assert _read(path)[1] == [{"a": "1"}, {"a": "2"}]


# -- review round 1 ----------------------------------------------------------


def test_many_recordings_at_the_same_time_stay_bounded():
    """400 recordings on the same day (several beds at once): the pairs are
    not all compared, but every recording's shared time still is."""
    import time

    from mef3io_studio.model.coverage import PAIR_LIMIT

    items = [
        item(
            f"r{i:03d}",
            details(channel("x", *[part(j, j * 700 + i, j * 700 + i + 600) for j in range(50)])),
        )
        for i in range(400)
    ]
    start = time.perf_counter()
    layout, _ = combine(items)
    assert time.perf_counter() - start < 10
    assert not layout.pairs_complete
    assert layout.overlaps == () and layout.overlapping == {} and PAIR_LIMIT < 400 * 399 / 2
    assert len(layout.sharing) == 400
    header, rows = recording_table([ExportItem(i, i.name + ".mefd") for i in items], UTC, layout)
    cells = [dict(zip(header, r, strict=True)) for r in rows]
    assert {c["overlaps_with"] for c in cells} == {"several, too many to list"}
    assert all(float(c["overlap_seconds"]) > 0 for c in cells)


def test_copies_need_the_same_channels():
    a = details(channel("x", part(0, 0, 100)))
    same_time_other_bed = details(channel("y", part(0, 0, 100)))
    layout, _ = combine([item("a", a), item("b", same_time_other_bed)])
    assert layout.overlaps[0].seconds == 100 and not layout.overlaps[0].may_be_copies


def test_tiny_gaps_between_recordings_are_rounding():
    a = details(channel("x", part(0, 0, 10)))
    b = details(channel("x", part(0, 10.002, 20)))  # 2 ms: under one sample period
    c = details(channel("x", part(0, 30, 40)))
    layout, _ = combine([item("a", a), item("b", b), item("c", c)])
    assert layout.gaps_between == ((T0 + 20 * S, T0 + 30 * S),)


def test_a_stretch_ending_at_the_left_edge_is_not_drawn():
    cov = recording_coverage(item("r", details(channel("a", part(0, 0, 10)))))
    assert pixel_runs(cov, T0 + 10 * S, T0 + 20 * S, 100) == []
    assert pixel_runs(cov, T0 + 9 * S, T0 + 20 * S, 110)[0][:2] == (0, 10)
    point = recording_coverage(item("p", details(channel("a", part(0, 10, 10)))))
    assert pixel_runs(point, T0 + 10 * S, T0 + 20 * S, 100) == [(0, 1, PIXEL_SOME, False)]


def test_the_repeated_hour_gets_its_marks():
    prague = resolve_zone("Europe/Prague")
    # 2026-10-25: clocks go back from 03:00 to 02:00 in Prague.
    start = 1_792_878_000_000_000  # 2026-10-25 00:00 +02:00
    marks = axis_ticks(start, start + 6 * 3600 * S, 3600 * S, prague)
    assert all(b - a == 3600 * S for a, b in zip(marks, marks[1:], strict=False))
    labels = [format_time(m, prague)[11:] for m in marks]
    assert "02:00:00 +02:00" in labels and "02:00:00 +01:00" in labels


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_table_gets_normal_permissions(tmp_path):
    fresh = tmp_path / "new.csv"
    old = os.umask(0o022)
    try:
        write_csv(str(fresh), ["a"], [[1]])
    finally:
        os.umask(old)
    assert fresh.stat().st_mode & 0o777 == 0o644
    shared = tmp_path / "shared.csv"
    shared.write_text("x")
    shared.chmod(0o664)
    write_csv(str(shared), ["a"], [[1]])
    assert shared.stat().st_mode & 0o777 == 0o664
    link = tmp_path / "link.csv"
    link.symlink_to(shared)
    write_csv(str(link), ["a"], [[2]])
    assert link.is_symlink() and _read(shared)[1] == [{"a": "2"}]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["link.csv", "new.csv", "shared.csv"]


def test_the_warning_names_the_pair_that_may_be_copies():
    from mef3io_studio.explorer.combined import overlap_text

    a = details(channel("x", part(0, 0, 1000)))
    b = details(channel("y", part(0, 0, 900)))  # same time, other channels
    c = details(channel("w", part(0, 0, 800)))
    d = details(channel("z", part(0, 2000, 2010)))
    copy = details(channel("z", part(0, 2000, 2010)))
    layout, _ = combine([item(k, v) for k, v in zip("abcde", (a, b, c, d, copy), strict=True)])
    text = overlap_text(layout)
    assert "d and e (10 s, may be copies)" in text
    assert text.count("may be copies)") == 1
    assert "the others were probably made at the same time" in text
    # Only copies: nothing about recordings made at the same time.
    layout, _ = combine([item("d", d), item("e", copy)])
    text = overlap_text(layout)
    assert "may be copies" in text and "same time" not in text.split(".", 1)[1]
