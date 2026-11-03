"""Cut into pieces (#31, 2): the plan (model/pieces.py) and pieces run as
"change" items of the ONE batch runner. Made-up signals only."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import mef3io
import numpy as np
import pytest
from explorer_sessions import SECOND, T0, write_large
from test_make_new_model import (  # noqa: F401 - the fixture clears the journals
    _no_journals,
    results,
    run,
    tree_hash,
)

from mef3io_studio.model import journal, pipeline
from mef3io_studio.model import makenew as mk
from mef3io_studio.model import pieces as P
from mef3io_studio.model.timefmt import from_local, resolve_zone
from mef3io_studio.workers import tasks

HOUR = 3600 * SECOND
PRAGUE = resolve_zone("Europe/Prague")


def test_every_so_many_hours_from_the_start():
    plan = P.plan_pieces(
        [(T0, T0 + 10 * HOUR)], 10_000_000, P.Choice(P.EVERY, 3, P.HOURS, aligned=False)
    )
    assert [(p.start - T0) // HOUR for p in plan.pieces] == [0, 3, 6, 9]
    assert plan.pieces[-1].end == T0 + 10 * HOUR and plan.left_out == 0
    assert [p.number for p in plan.pieces] == [1, 2, 3, 4]
    # Pieces tile the recording: each ends where the next starts; windows too.
    for a, b in zip(plan.pieces, plan.pieces[1:], strict=False):
        assert a.end == b.start and a.window[1] == b.window[0]
    assert sum(p.bytes for p in plan.pieces) > 10_000_000  # about the whole, plus room each


def test_aligned_hours_start_at_midnight_in_the_zone():
    import datetime as dt

    start = from_local(dt.datetime(2021, 6, 1, 7, 30), PRAGUE)
    plan = P.plan_pieces(
        [(start, start + 30 * HOUR)], 1, P.Choice(P.EVERY, 12, P.HOURS, aligned=True), PRAGUE
    )
    firsts = [P.to_datetime(p.start, PRAGUE) for p in plan.pieces]
    assert [(d.day, d.hour, d.minute) for d in firsts] == [
        (1, 7, 30),
        (1, 12, 0),
        (2, 0, 0),
        (2, 12, 0),
    ]


def test_one_piece_per_calendar_day_across_a_clock_change():
    import datetime as dt

    start = from_local(dt.datetime(2021, 3, 27, 12, 0), PRAGUE)
    end = from_local(dt.datetime(2021, 3, 30, 12, 0), PRAGUE)
    plan = P.plan_pieces([(start, end)], 1_000_000, P.Choice(P.EVERY, 1, P.DAYS, True), PRAGUE)
    starts = [P.to_datetime(p.start, PRAGUE) for p in plan.pieces]
    assert [(d.month, d.day, d.hour) for d in starts] == [
        (3, 27, 12),
        (3, 28, 0),
        (3, 29, 0),
        (3, 30, 0),
    ]
    lengths = [round(p.seconds / 3600, 3) for p in plan.pieces]
    assert lengths == [12.0, 23.0, 24.0, 12.0]  # the 28th lost an hour
    # And when the clocks go back: 25 hours.
    s2 = from_local(dt.datetime(2021, 10, 30, 12, 0), PRAGUE)
    e2 = from_local(dt.datetime(2021, 11, 1, 12, 0), PRAGUE)
    plan2 = P.plan_pieces([(s2, e2)], 1, P.Choice(P.EVERY, 1, P.DAYS, True), PRAGUE)
    assert [round(p.seconds / 3600, 3) for p in plan2.pieces] == [12.0, 25.0, 12.0]
    names = P.piece_names(plan2, "Rec", "<name>_day<number>_<date>", PRAGUE)
    assert names == ["Rec_day01_2021-10-30", "Rec_day02_2021-10-31", "Rec_day03_2021-11-01"]


def test_typed_times_are_sorted_each_once_and_only_inside():
    runs = [(T0, T0 + 10 * HOUR)]
    choice = P.Choice(
        P.AT, at=(T0 + 5 * HOUR, T0 + 2 * HOUR, T0 + 5 * HOUR, T0 - HOUR, T0 + 99 * HOUR, T0)
    )
    plan = P.plan_pieces(runs, 1, choice)
    assert [p.start for p in plan.pieces] == [T0, T0 + 2 * HOUR, T0 + 5 * HOUR]
    assert P.plan_pieces(runs, 1, P.Choice(P.AT, at=(T0 - 5,))).problem == P.NO_CUTS


def test_pieces_with_no_data_are_left_out_and_said_so():
    runs = [(T0, T0 + HOUR), (T0 + 5 * HOUR, T0 + 6 * HOUR)]
    plan = P.plan_pieces(runs, 2_000_000, P.Choice(P.EVERY, 1, P.HOURS, aligned=False))
    # (a stretch counts through its last sample's time, so the piece starting
    # where the first one ends is kept: a writer may state the end that way)
    assert [(p.start - T0) // HOUR for p in plan.pieces] == [0, 1, 5]
    assert plan.left_out == 3  # hours 2..4
    assert "3 pieces would hold no data" in P.describe_left_out(plan.left_out)
    assert "4 pieces would hold no data" in P.describe_left_out(4)
    assert "1 piece would hold no data" in P.describe_left_out(1)
    assert P.describe_left_out(0) == ""
    assert [p.number for p in plan.pieces] == [1, 2, 3]  # numbered among those made


def test_problems_are_said_in_plain_words():
    runs = [(T0, T0 + HOUR)]
    assert P.plan_pieces(runs, 1, P.Choice(P.EVERY, 5, P.HOURS, False)).problem == P.ONE_PIECE
    assert P.plan_pieces(runs, 1, P.Choice(P.EVERY, 0, P.HOURS)).problem == P.NOT_POSITIVE
    assert P.plan_pieces([], 1, P.Choice()).problem
    assert P.plan_pieces(runs, 1, P.Choice(), verified=False).problem == P.NO_TIMES
    huge = [(T0, T0 + 5000 * HOUR)]
    assert P.plan_pieces(huge, 1, P.Choice(P.EVERY, 1, P.HOURS, False)).problem == P.TOO_MANY


def test_names_come_from_a_pattern_and_must_differ():
    plan = P.plan_pieces([(T0, T0 + 3 * HOUR)], 1, P.Choice(P.EVERY, 1, P.HOURS, False))
    assert P.piece_names(plan, "Rec", P.DEFAULT_PATTERN) == ["Rec_01", "Rec_02", "Rec_03"]
    assert "number" in P.pattern_problem("fixed") and P.pattern_problem("x<number>") == ""
    src = mk.Source("k", "/x/Rec.mefd", "Rec", ("a",), {"a": 1})
    same = ["Rec"] * 3
    found = mk.name_problems(P.piece_sources(src, plan), same, "/")
    assert any("same name" in f for f in found)


def write_signal(path: Path, seconds: int = 90, fs: float = 256.0, start: int = T0) -> np.ndarray:
    rng = np.random.default_rng(3)
    data = rng.integers(-5000, 5000, int(seconds * fs)).astype(float)
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        w.write("a1", data, start, fs)
        w.write("a2", data[::-1].copy(), start, fs)
    return data


def batch_for(
    tmp_path, source: Path, choice: P.Choice, pattern="Piece-<number>", password=None, runs=None
):
    found = mef3io.time_range(str(source))
    runs = runs or [(int(found.start_uutc), int(found.end_uutc))]
    total = sum(f.stat().st_size for f in source.rglob("*.tdat"))
    plan = P.plan_pieces(runs, total, choice)
    src = mk.Source(
        "src", str(source), source.stem, ("a1", "a2"), {"a1": total // 2, "a2": total // 2}
    )
    dest = tmp_path / "out"
    dest.mkdir(exist_ok=True)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    names = P.piece_names(plan, src.name, pattern)
    requests = P.requests_for(src, plan, names, str(dest), password)
    batch = pipeline.plan_batch(
        pipeline.FOLDERS,
        requests,
        str(dest),
        str(work),
        P.estimates_for(src, plan),
        {r.key: (src.name, p.start, p.end) for r, p in zip(requests, plan.pieces, strict=True)},
    )
    return plan, batch, dest, work


def test_three_pieces_hold_exactly_the_samples_of_the_original(qtbot, jobs, tmp_path):
    source = tmp_path / "Long.mefd"
    data = write_signal(source)
    before = tree_hash(source)
    # One cut on a sample, one between two samples (the boundary is exact either way).
    cuts = (T0 + 30 * SECOND, T0 + 60 * SECOND + 1_300)
    plan, batch, dest, work = batch_for(tmp_path, source, P.Choice(P.AT, at=cuts))
    assert len(plan.pieces) == 3
    handle, published = run(qtbot, jobs, batch)
    assert handle.record.status.name == "SUCCEEDED"
    done = results(published)
    assert len(done) == 3 and all(r.ok for r in done.values())
    parts = []
    for n, piece in enumerate(plan.pieces, 1):
        folder = dest / f"Piece-0{n}.mefd"
        assert folder.is_dir()
        # Each piece was checked against the original by the library.
        report = mef3io.transform.verify(
            str(source), str(folder), time_range=piece.window, exact=True
        )
        assert report.ok, (n, report)
        parts.append(np.asarray(mef3io.Reader(str(folder)).read("a1"), dtype=float))
    joined = np.concatenate(parts)
    assert not np.isnan(joined).any()
    assert np.array_equal(joined, data)  # every sample, once, in order
    assert sum(map(len, parts)) == len(data)
    assert tree_hash(source) == before  # the original is byte for byte the same
    assert list(work.iterdir()) == [] and not list(dest.rglob("*.partial"))
    assert journal.unfinished() == []


def test_a_stop_leaves_only_whole_checked_pieces(qtbot, jobs, tmp_path):
    source = write_large(tmp_path / "Big.mefd", channels=2, parts=2, seconds=600)
    before = tree_hash(source)
    found = mef3io.time_range(str(source))
    mid = (int(found.start_uutc) + int(found.end_uutc)) // 2
    quarter = (int(found.end_uutc) - int(found.start_uutc)) // 4
    cuts = (int(found.start_uutc) + quarter, mid, mid + quarter)
    plan, batch, dest, work = batch_for(tmp_path, source, P.Choice(P.AT, at=cuts))
    handle = jobs.submit("batch", tasks.run_batch, batch, None)

    def started() -> bool:
        p = handle.record.progress
        return handle.is_finished or (p is not None and p.phase.startswith("Recording 1 of"))

    qtbot.waitUntil(started, timeout=120_000)
    handle.cancel()
    if not handle.is_finished:
        with qtbot.waitSignal(handle.finished, timeout=120_000):
            pass
    # Wherever the Stop lands: a run of whole pieces from the first on, each
    # checked against the original; nothing half-made; the original unchanged.
    made = sorted(p.name for p in dest.iterdir())
    names = [f"Piece-0{n}.mefd" for n in range(1, len(plan.pieces) + 1)]
    assert made == names[: len(made)]
    assert not list(dest.rglob("*.partial")) and list(work.iterdir()) == []
    for name, piece in zip(made, plan.pieces, strict=False):
        report = mef3io.transform.verify(
            str(source), str(dest / name), time_range=piece.window, exact=True
        )
        assert report.ok, name
    assert tree_hash(source) == before
    assert journal.unfinished() == []


def test_the_pieces_are_change_items_with_their_window_and_no_password(tmp_path):
    source = tmp_path / "S.mefd"
    write_signal(source, seconds=20)
    plan, batch, _d, _w = batch_for(
        tmp_path, source, P.Choice(P.EVERY, 5, P.HOURS, False), password=None
    )
    assert plan.problem  # 20 s are one piece of 5 hours: nothing to cut
    plan, batch, _d, _w = batch_for(tmp_path, source, P.Choice(P.AT, at=(T0 + 10 * SECOND,)))
    assert [i.kind for i in batch.items] == [pipeline.KIND_CHANGE] * 2
    assert [i.request.time_range for i in batch.items] == [p.window for p in plan.pieces]
    assert all(i.request.exact for i in batch.items)
    text = repr(batch.items[0].request)
    assert str(source) not in text and str(T0) not in text  # nothing identifying in a repr


def test_utc_is_the_default_zone():
    assert resolve_zone("") is UTC


def test_a_stretch_includes_its_last_sample_time():
    s = P.Stretches([(100, 200)])
    assert s.holds_data(200, 300)  # the last sample may sit exactly at 200
    assert not s.holds_data(201, 300) and not s.holds_data(0, 100)
    assert s.holds_data(0, 101) and s.holds_data(150, 160)
    point = P.Stretches([(50, 50)])
    assert point.holds_data(50, 51) and point.holds_data(0, 51) and not point.holds_data(51, 90)


def test_the_lookup_agrees_with_a_plain_scan_on_many_stretches():
    rng = np.random.default_rng(5)
    edges = np.sort(rng.integers(0, 10**9, 4000))
    runs = [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2], strict=True)]
    s = P.Stretches(runs)
    for _ in range(300):
        a, b = sorted(int(x) for x in rng.integers(-10, 10**9 + 10, 2))
        if a == b:
            continue
        brute_data = sum(max(0, min(e, b) - max(st, a)) for st, e in runs)
        brute_holds = any(st < b and e >= a for st, e in runs)
        assert s.data_inside(a, b) == brute_data
        assert s.holds_data(a, b) == brute_holds
    # A plan over thousands of stretches and hundreds of pieces is the same call.
    plan = P.plan_pieces(s, 10**9, P.Choice(P.EVERY, 10_000_000, P.HOURS, aligned=False))
    assert plan.problem or plan.pieces


@pytest.mark.parametrize(
    ("zone", "day"),
    [("America/Sao_Paulo", (2018, 11, 4)), ("America/Havana", (2019, 3, 10))],
)
def test_a_midnight_that_does_not_exist_is_the_first_instant_of_the_day(zone, day):
    import datetime as dt

    z = resolve_zone(zone)
    start = from_local(dt.datetime(*day, 12, 0), z) - 24 * HOUR
    end = start + 3 * 24 * HOUR
    plan = P.plan_pieces([(start, end)], 1, P.Choice(P.EVERY, 1, P.DAYS, True), z)
    starts = [P.to_datetime(p.start, z) for p in plan.pieces]
    jump = [d for d in starts if d.date() == dt.date(*day)]
    assert len(jump) == 1
    # No instant of the day before belongs to the day; the cut is the jump itself.
    cut = jump[0]
    assert cut.hour == 1 and cut.minute == 0
    assert all(a.start < b.start for a, b in zip(plan.pieces, plan.pieces[1:], strict=False))


def write_gappy_signal(path: Path, fs: float = 256.0) -> np.ndarray:
    rng = np.random.default_rng(11)
    first = rng.integers(-5000, 5000, int(30 * fs)).astype(float)
    second = rng.integers(-5000, 5000, int(30 * fs)).astype(float)
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        w.write("a1", first, T0, fs)
        w.write("a1", second, T0 + 60 * SECOND, fs)
        w.write("a2", first, T0, fs)
        w.write("a2", second, T0 + 60 * SECOND, fs)
    return np.concatenate([first, second])


def test_pieces_of_a_recording_with_a_gap_hold_every_sample_once(qtbot, jobs, tmp_path):
    source = tmp_path / "Gap.mefd"
    data = write_gappy_signal(source)
    runs = [(T0, T0 + 30 * SECOND), (T0 + 60 * SECOND, T0 + 90 * SECOND)]
    # Two cuts inside the gap (the piece between holds nothing) and one in the data.
    cuts = (T0 + 15 * SECOND, T0 + 40 * SECOND, T0 + 50 * SECOND, T0 + 75 * SECOND)
    plan, batch, dest, work = batch_for(tmp_path, source, P.Choice(P.AT, at=cuts), runs=runs)
    assert len(plan.pieces) == 4 and plan.left_out == 1
    handle, published = run(qtbot, jobs, batch)
    assert all(r.ok for r in results(published).values()) and len(results(published)) == 4
    joined = np.concatenate(
        [
            np.asarray(mef3io.Reader(str(dest / f"Piece-0{n}.mefd")).read("a1"), dtype=float)
            for n in range(1, 5)
        ]
    )
    assert np.array_equal(joined[~np.isnan(joined)], data)


def test_the_first_and_last_pieces_of_a_day_cut_are_partial(qtbot, jobs, tmp_path):
    source = tmp_path / "Midnight.mefd"
    start = T0 + 24 * HOUR - 30 * SECOND  # 23:59:30 UTC
    data = write_signal(source, seconds=90, start=start)
    plan, batch, dest, work = batch_for(tmp_path, source, P.Choice(P.EVERY, 1, P.DAYS, True))
    assert [round(p.seconds) for p in plan.pieces] == [30, 60]
    handle, published = run(qtbot, jobs, batch)
    assert all(r.ok for r in results(published).values()) and len(results(published)) == 2
    parts = [
        np.asarray(mef3io.Reader(str(dest / f"Piece-0{n}.mefd")).read("a1"), dtype=float)
        for n in (1, 2)
    ]
    assert [len(x) for x in parts] == [30 * 256, 60 * 256]
    assert np.array_equal(np.concatenate(parts), data)
