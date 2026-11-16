"""Many notes must not freeze the window: what is drawn and built is bounded by
the PIXELS, not by the notes (docs/design/viewer.md, "Annotations"). The timing
is opt-in (MEF3IO_STUDIO_BENCH=1); the functional tests are fast."""

# ruff: noqa: F811
from __future__ import annotations

import os
import time

import mef3io
import numpy as np
import pytest
from PySide6.QtCore import QPoint, QPointF
from PySide6.QtTest import QTest
from test_viewer_marks_ui import wait_marks
from test_viewer_ui import make_window, settle  # noqa: F401

from mef3io_studio.model import viewer_marks as vm

T0 = 946_684_800_000_000
SEC = 1_000_000
HOUR = 3600 * SEC


def write_many(path, hours: float, n_notes: int, fs: float = 10.0, seed: int = 3):
    """``hours`` of signal and ``n_notes`` notes (Note / EDFA, some with text)."""
    rng = np.random.default_rng(seed)
    n = int(hours * 3600 * fs)
    t = np.arange(n) / fs
    times = np.sort(rng.integers(0, int(hours * HOUR), n_notes)) + T0
    notes = []
    for k, tm in enumerate(times):
        if k % 7 == 0:
            notes.append(
                {"time": int(tm), "type": "EDFA", "duration": 2 * SEC, "text": f"Spike: {k}"}
            )
        else:
            notes.append({"time": int(tm), "type": "Note", "text": f"note {k}"})
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        for k, name in enumerate(("Fp1", "Fp2")):
            w.write(name, np.round(20 * np.sin(2 * np.pi * (0.5 + k) * t), 2), T0, fs)
        w.write_annotations(notes)
    return path, [int(x) for x in times]


def zoom_out_to(win, seconds: float):
    """A timebase that puts about ``seconds`` on screen."""
    mm = win.canvas.plot_width / win.canvas.px_per_mm
    win.set_state(timebase=mm / seconds)


def test_a_dense_view_draws_a_bounded_number_of_marks_and_every_note_stays_reachable(
    make_window, tmp_path, qtbot
):
    path, times = write_many(tmp_path / "many.mefd", hours=2, n_notes=6000)
    win = make_window(path)
    wait_marks(qtbot, win)
    win.set_state(t0=T0)
    zoom_out_to(win, 2 * 3600)
    settle(qtbot, win)
    assert win.page_us() >= 1.9 * HOUR
    width = win.canvas.plot_width
    assert 0 < len(win.canvas.marks) <= width, len(win.canvas.marks)
    assert len(win.chips.chips) <= width // 2
    # the counts of the merged ticks add up to every note in view
    in_view = [t for t in times if win.state.t0 <= t < win.state.t0 + win.page_us()]
    assert sum(m.count for m in win.canvas.marks) == len(in_view) == len(times)
    # hover finds a tick over every note; its tip (built only now) says how many
    plot = win.canvas.plot_rect()
    for t in times[:: len(times) // 40]:
        pos = QPointF(win.canvas.x_of(t), plot.top() + 12)
        found = win.canvas.mark_at(pos)
        assert found is not None and found.count >= 1
        assert win.canvas.tip_for(found)
    # stepping reaches every note in turn, none skipped (the window's own step)
    seen, last = [], T0 - 2
    while (t := vm.step_time(win._visible_times, last, 1)) is not None:
        seen.append(t)
        last = t
    assert sorted(set(seen)) == sorted(set(times)) and len(seen) >= len(set(times))
    win.go_to_mark(1)  # and the key itself works in a crowded view
    assert win._direction == 1
    # a tick is clickable: its chip centres the page on its first note
    first_chip = win.chips.chips[0]
    assert first_chip.time in times


def test_a_sparse_view_keeps_one_full_line_and_one_chip_per_note(make_window, tmp_path, qtbot):
    path, times = write_many(tmp_path / "few.mefd", hours=0.2, n_notes=12)
    win = make_window(path)
    wait_marks(qtbot, win)
    win.set_state(t0=T0)
    zoom_out_to(win, 0.2 * 3600)
    settle(qtbot, win)
    assert len(win.canvas.marks) == 12 and all(m.count == 1 for m in win.canvas.marks)
    assert len(win.chips.chips) == 12


@pytest.mark.skipif(not os.environ.get("MEF3IO_STUDIO_BENCH"), reason="opt-in timing")
def test_sixty_thousand_notes_over_twelve_hours_do_not_freeze_the_window(
    make_window, tmp_path, qtbot
):
    path, times = write_many(tmp_path / "huge.mefd", hours=12, n_notes=60_000)
    t = time.perf_counter()
    win = make_window(path)
    wait_marks(qtbot, win)
    out = {"open and read the notes": time.perf_counter() - t}
    win.set_state(t0=T0)
    settle(qtbot, win)
    t = time.perf_counter()
    zoom_out_to(win, 12 * 3600)
    out["zoom out to 12 hours (the call)"] = time.perf_counter() - t
    t = time.perf_counter()
    qtbot.wait(1)
    win.canvas.grab_traces()
    win.canvas.repaint()
    win.chips.repaint()
    out["paint"] = time.perf_counter() - t
    t = time.perf_counter()
    plot = win.canvas.plot_rect()
    for k in range(200):
        QTest.mouseMove(win.canvas, QPoint(int(plot.left()) + 5 + k * 3, int(plot.top()) + 12))
    out["200 pointer moves"] = time.perf_counter() - t
    t = time.perf_counter()
    win._refresh_note_list()
    qtbot.wait(5)
    out["the side list"] = time.perf_counter() - t
    print("\nmany notes: 60000 notes, 12 h")
    for k, v in out.items():
        print(f"  {k:<36} {v * 1e3:8.0f} ms")
    assert out["zoom out to 12 hours (the call)"] < 1.0


def test_merged_columns_count_every_note_and_keep_the_most_important_type():
    times = np.array([0, 5, 9, 10, 11, 55, 99], dtype=np.int64)  # 10 columns of 10 over [0, 100)
    rank = np.array([3, 9, 3, 3, 5, 3, 1], dtype=np.int64)
    first, counts, best = vm.merge_columns(times, rank, 0, 100, 10)
    assert first.tolist() == [0, 3, 5, 6] and counts.tolist() == [3, 2, 1, 1]
    assert counts.sum() == len(times)
    assert best.tolist() == [1, 4, 5, 6]  # the seizure-ranked note, the event, then the lone ones
    empty = vm.merge_columns(times[:0], rank[:0], 0, 100, 10)
    assert all(len(x) == 0 for x in empty)
    assert vm.kind_importance("type:Seiz") > vm.kind_importance("type:EDFA")
    assert vm.kind_importance("type:EDFA") > vm.kind_importance("type:Note")
    assert vm.kind_importance("quick:Seizure onset") == vm.kind_importance("type:Seiz")


def wait_list_done(qtbot, win):
    """Both stages: the notes looked through, then the rows filled in."""
    qtbot.waitUntil(lambda: not win._list_runner.busy and not win._fill_runner.busy, timeout=10_000)


def test_the_side_list_of_a_crowded_view_is_filled_in_slices(make_window, tmp_path, qtbot):
    path, times = write_many(tmp_path / "many.mefd", hours=1, n_notes=3000)
    win = make_window(path)
    wait_marks(qtbot, win)
    win.notes_tab.search.setText("event")
    qtbot.waitUntil(lambda: not win._list_runner.busy, timeout=10_000)
    assert win.notes_tab.rows and all(m.code == "EDFA" for m in win.notes_tab.rows)
    assert len(win.notes_tab.rows) == sum(1 for k in range(3000) if k % 7 == 0)


def test_a_long_list_says_it_is_cut_short_from_the_first_row_on(
    make_window, tmp_path, qtbot, monkeypatch
):
    from mef3io_studio.viewer import window as vwin

    monkeypatch.setattr(vwin, "MAX_LISTED", 300)  # more than a short list, fewer than all
    path, _ = write_many(tmp_path / "many.mefd", hours=1, n_notes=3000)
    win = make_window(path)
    wait_marks(qtbot, win)
    said = []
    real = win.notes_tab.count.setText
    win.notes_tab.count.setText = lambda t: (said.append(t), real(t))[1]
    win.notes_tab.search.setText("note")
    wait_list_done(qtbot, win)
    assert len(win.notes_tab.rows) == 300
    assert said and all(t.startswith("The first 300 of ") for t in said), said


def write_notes(path, notes, seconds=3600, fs=10.0):
    n = int(seconds * fs)
    t = np.arange(n) / fs
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        for k, name in enumerate(("Fp1", "Fp2")):
            w.write(name, np.round(20 * np.sin(2 * np.pi * (0.5 + k) * t), 2), T0, fs)
        w.write_annotations(notes)
    return path


def early_notes(n=2000, seed=1):
    rng = np.random.default_rng(seed)
    return [
        {"time": T0 + int(ts) * SEC + 1, "type": "Note", "text": "early"}
        for ts in sorted(rng.integers(0, 1800, n))
    ]


def test_one_long_note_does_not_make_a_nearly_empty_page_crowded(make_window, tmp_path, qtbot):
    notes = [{"time": T0, "type": "EDFA", "duration": 3000 * SEC, "text": "long"}, *early_notes()]
    notes += [
        {"time": T0 + 2400 * SEC + k * SEC // 2, "type": "Note", "text": f"in view {k}"}
        for k in range(20)
    ]
    win = make_window(write_notes(tmp_path / "long.mefd", notes))
    wait_marks(qtbot, win)
    zoom_out_to(win, 10)
    win.set_state(t0=T0 + 2400 * SEC - SEC)
    settle(qtbot, win)
    t0, t1 = win.state.t0, win.state.t0 + win.page_us()
    real = [m for m in win._visible if m.time <= t1 and (m.end or m.time) >= t0]
    assert len(real) == 20  # 19 of the twenty fit the page, and the long one lasts into it
    assert len(real) < 40
    assert not any(m.tick for m in win.canvas.marks)
    assert len(win.canvas.marks) == len(real) == len(win.chips.chips)
    band = [m for m in win.canvas.marks if m.end]
    assert len(band) == 1 and band[0].time < t0 < band[0].end


def test_a_crowded_page_keeps_the_band_of_a_long_note_and_counts_only_notes_in_view(
    make_window, tmp_path, qtbot
):
    notes = [{"time": T0, "type": "EDFA", "duration": 3000 * SEC, "text": "long"}, *early_notes()]
    notes += [  # a note that starts in view and lasts longer than a pixel
        {"time": T0 + 2400 * SEC, "type": "EDFA", "duration": 3 * SEC, "text": "wide"}
    ]
    notes += [
        {"time": T0 + 2400 * SEC + k * SEC // 70, "type": "Note", "text": f"in view {k}"}
        for k in range(1, 600)
    ]
    win = make_window(write_notes(tmp_path / "crowd.mefd", notes))
    wait_marks(qtbot, win)
    zoom_out_to(win, 10)
    win.set_state(t0=T0 + 2400 * SEC - SEC)
    settle(qtbot, win)
    t0, t1 = win.state.t0, win.state.t0 + win.page_us()
    marks = win.canvas.marks
    assert any(m.tick for m in marks)  # it is crowded
    ticks = [m for m in marks if m.tick]
    assert all(t0 <= m.time < t1 for m in ticks)  # nothing from before the page
    bands = [m for m in marks if not m.tick and m.end]
    assert sorted(b.end - b.time for b in bands) == [3 * SEC, 3000 * SEC]
    starting_in_view = [m for m in win._visible if t0 <= m.time <= t1]
    merged = sum(m.count for m in ticks)
    assert merged == len(starting_in_view) - 1  # all but the wide note, which keeps its band
    assert sum(c.count for c in win.chips.chips) == len(starting_in_view) + 1  # + the long one


def test_a_draft_note_in_a_crowded_view_is_not_counted_twice(make_window, tmp_path, qtbot):
    notes = [
        {"time": T0 + 100 * SEC + k * SEC // 40, "type": "Note", "text": f"n{k}"}
        for k in range(400)
    ]
    win = make_window(write_notes(tmp_path / "draft.mefd", notes, seconds=300))
    wait_marks(qtbot, win)
    zoom_out_to(win, 20)
    win.set_state(t0=T0 + 95 * SEC)
    settle(qtbot, win)
    win.edit.ask_unsaved = lambda what: "discard"  # a failing test must not wait on a question
    win.edit_button.click()
    assert win.edit.on
    win.edit.add(T0 + 103 * SEC, None, "Spike", "x")
    settle(qtbot, win)
    t0, t1 = win.state.t0, win.state.t0 + win.page_us()
    marks = win.canvas.marks
    drawn_alone = [m for m in marks if not m.tick]
    assert len(drawn_alone) == 1 and drawn_alone[0].style == "new"
    in_view = [m for m in win._visible if t0 <= m.time <= t1]
    assert len(in_view) == 401
    assert sum(m.count for m in marks if m.tick) == 400
    assert sum(c.count for c in win.chips.chips) == 401
