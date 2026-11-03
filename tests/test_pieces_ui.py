"""Explore's "Cut into pieces…", offscreen: the steps, the live list of
pieces, pieces left out, names from a pattern, and a real run through the
shared batch runner. Made-up signals only."""

from __future__ import annotations

import mef3io
import numpy as np
import pytest
from explorer_sessions import SECOND, T0, write_plain
from test_make_new_model import _no_journals  # noqa: F401 - the fixture clears the journals
from test_make_new_ui import open_all, tree_hash
from test_pieces_model import write_signal

from mef3io_studio.model import pieces as P
from mef3io_studio.pieces import PiecesDialog
from mef3io_studio.pieces import dialog as dlg


@pytest.fixture
def shown_window(window):
    window._closing = True
    window.show()
    return window


def make_dialog(qtbot, window, entries) -> PiecesDialog:
    dialog = PiecesDialog(entries, window.jobs, parent=window)
    qtbot.addWidget(dialog)
    return dialog


def reach_cut_step(qtbot, dialog):
    dialog.next()
    assert dialog.step == dlg.STEP_CUT
    qtbot.waitUntil(lambda: dialog.runs is not None, timeout=30_000)


def test_the_menus_need_a_recording(shown_window, qtbot, tmp_path):
    explorer = shown_window.explorer
    said = []
    explorer.status.connect(said.append)
    assert shown_window.cut_action.text() == "&Cut the selected recording into pieces…"
    assert explorer.cut_into_pieces_selected() is None
    assert said == ["Select a recording to cut into pieces."]
    entries = open_all(qtbot, shown_window, [write_signal_session(tmp_path / "a.mefd")])
    explorer.select_entry(entries[0])
    dialog = explorer.cut_into_pieces_selected()
    assert isinstance(dialog, PiecesDialog)
    qtbot.addWidget(dialog)
    assert dialog.step_label.text() == "Step 1 of 5: Recording"
    dialog.reject()


def write_signal_session(path):
    write_signal(path)
    return path


def test_the_pieces_are_listed_as_the_choice_changes(shown_window, qtbot, tmp_path):
    entries = open_all(qtbot, shown_window, [write_signal_session(tmp_path / "a.mefd")])
    dialog = make_dialog(qtbot, shown_window, entries)
    reach_cut_step(qtbot, dialog)
    # Every day, from midnight: the 90 s recording is one piece, so nothing to cut.
    assert "single piece" in dialog.problem.text() and not dialog.next_button.isEnabled()
    dialog.unit.setCurrentIndex(0)  # hours
    dialog.aligned.setChecked(False)
    assert not dialog.next_button.isEnabled()  # still one piece of an hour
    # At typed times.
    dialog.at_input.set_us(T0 + 30 * SECOND, dialog.zone)
    dialog.add_time()
    dialog.at_input.set_us(T0 + 60 * SECOND, dialog.zone)
    dialog.add_time()
    assert dialog.at_list.count() == 2 and dialog.table.rowCount() == 3
    assert [dialog.table.item(n, 3).text() for n in range(3)] == ["30 s", "30 s", "30 s"]
    assert dialog.problem.text() == "" and dialog.next_button.isEnabled()
    dialog.at_list.setCurrentRow(0)
    dialog.remove_time()
    assert dialog.table.rowCount() == 2
    dialog.reject()


def test_pieces_without_data_are_left_out_and_said(shown_window, qtbot, tmp_path):
    from explorer_sessions import write_gappy

    entries = open_all(qtbot, shown_window, [write_gappy(tmp_path / "gap.mefd")])
    dialog = make_dialog(qtbot, shown_window, entries)
    reach_cut_step(qtbot, dialog)
    first, last = dialog.runs[0][0], max(e for _s, e in dialog.runs)
    gap = next((a, b) for (_x, a), (b, _y) in zip(dialog.runs, dialog.runs[1:], strict=False))
    # Cut inside the data, then twice inside the gap: the middle piece holds nothing.
    for t in (
        first + (gap[0] - first) // 2,
        gap[0] + (gap[1] - gap[0]) // 3,
        gap[0] + 2 * (gap[1] - gap[0]) // 3,
    ):
        dialog.at_input.set_us(t // SECOND * SECOND, dialog.zone)
        dialog.add_time()
    plan = dialog.plan()
    assert plan.left_out >= 1 and "hold no data" in dialog.left_out_note.text()
    assert dialog.table.rowCount() == len(plan.pieces)
    assert last > first
    dialog.reject()


def test_names_and_folder_step_blocks_until_it_can_run(shown_window, qtbot, tmp_path):
    entries = open_all(qtbot, shown_window, [write_signal_session(tmp_path / "a.mefd")])
    dialog = make_dialog(qtbot, shown_window, entries)
    reach_cut_step(qtbot, dialog)
    for s in (30, 60):
        dialog.at_input.set_us(T0 + s * SECOND, dialog.zone)
        dialog.add_time()
    dialog.next()
    assert dialog.step == dlg.STEP_PASSWORDS
    dialog.passwords.both.setChecked(True)
    dialog.passwords.basic.setText("b-pass-12")
    assert dialog.problem.text() != "" and not dialog.next_button.isEnabled()
    dialog.passwords.none.setChecked(True)
    dialog.next()
    assert dialog.step == dlg.STEP_WHERE
    assert dialog.new_names() == ["a_day01", "a_day02", "a_day03"]
    dialog.pattern.setText("fixed")
    assert "<number>" in dialog.problem.text() and not dialog.start_button.isEnabled()
    dialog.pattern.setText("<name>_<number>")
    assert dialog.names_preview.item(0).text() == "1. a_01"
    assert "Choose a folder" in dialog.problem.text()
    dialog.folder = str(tmp_path)
    (tmp_path / "a_02.mefd").mkdir()
    dialog._changed()
    assert "already in the chosen folder" in dialog.problem.text()
    dialog.pattern.setText("<name>-<number>")
    assert dialog.problem.text() == "" and dialog.start_button.isEnabled()
    dialog.back()
    assert dialog.step == dlg.STEP_PASSWORDS
    dialog.reject()


def test_a_whole_cut_through_the_dialog(shown_window, qtbot, tmp_path):
    source = tmp_path / "Rec.mefd"
    data = write_signal(source)
    before = tree_hash(source)
    out = tmp_path / "out"
    out.mkdir()
    entries = open_all(qtbot, shown_window, [source])
    dialog = make_dialog(qtbot, shown_window, entries)
    reach_cut_step(qtbot, dialog)
    for s in (30, 60):
        dialog.at_input.set_us(T0 + s * SECOND, dialog.zone)
        dialog.add_time()
    dialog.next()
    dialog.next()
    dialog.pattern.setText("Cut-<number>")
    dialog.folder = str(out)
    dialog._changed()
    assert dialog.problem.text() == ""
    assert dialog.start() is not None
    qtbot.waitUntil(lambda: dialog.finished_run, timeout=180_000)
    assert [dialog.results_table.item(n, 1).text() for n in range(3)] == ["Saved and checked"] * 3
    assert dialog.summary.text() == "Finished: 3 of 3 pieces saved and checked."
    assert sorted(p.name for p in out.iterdir()) == [f"Cut-0{i}.mefd" for i in (1, 2, 3)]
    joined = np.concatenate(
        [np.asarray(mef3io.Reader(str(out / f"Cut-0{i}.mefd")).read("a1")) for i in (1, 2, 3)]
    )
    assert np.array_equal(joined, data)
    assert tree_hash(source) == before
    assert all(i.request.output_password is None for i in dialog.batch.items)
    opened = []
    dialog.open_requested.connect(lambda paths, pw: opened.append((paths, pw)))
    dialog.open_button.click()
    assert len(opened[0][0]) == 3
    dialog.reject()


def test_a_recording_with_unconfirmed_times_is_not_offered(shown_window, qtbot, tmp_path):
    from explorer_sessions import set_time_offset

    entries = open_all(qtbot, shown_window, [set_time_offset(write_plain(tmp_path / "o.mefd"), 5)])
    assert not entries[0].details.times_verified
    assert shown_window.explorer._cuttable(entries) == []
    assert P.plan_pieces([(T0, T0 + 10 * SECOND)], 1, P.Choice(), verified=False).problem == (
        P.NO_TIMES
    )


def test_a_failed_search_for_data_is_shown_not_waited_for(
    shown_window, qtbot, tmp_path, monkeypatch
):
    def broken(ctx, item):
        raise ValueError("boom")

    monkeypatch.setattr(dlg, "data_stretches", broken)
    entries = open_all(qtbot, shown_window, [write_signal_session(tmp_path / "a.mefd")])
    dialog = make_dialog(qtbot, shown_window, entries)
    dialog.next()
    qtbot.waitUntil(lambda: dialog.runs_problem != "", timeout=30_000)
    assert dialog.problem.text() == dialog.runs_problem
    assert "Finding out" not in dialog.problem.text() and not dialog.next_button.isEnabled()
    dialog.reject()


def test_another_recording_starts_clean(shown_window, qtbot, tmp_path):
    entries = open_all(
        qtbot,
        shown_window,
        [write_signal_session(tmp_path / "a.mefd"), write_signal_session(tmp_path / "b.mefd")],
    )
    dialog = make_dialog(qtbot, shown_window, entries)
    reach_cut_step(qtbot, dialog)
    dialog.at_input.set_us(T0 + 30 * SECOND, dialog.zone)
    dialog.add_time()
    assert dialog.at_times
    dialog.back()
    dialog.recording_list.setCurrentRow(1)
    assert dialog.at_times == [] and dialog.runs is None
    dialog.next()
    qtbot.waitUntil(lambda: dialog.runs is not None, timeout=30_000)
    assert dialog.at_times == [] and dialog.at_list.count() == 0
    dialog.reject()


def test_the_plan_is_worked_out_once_per_change(shown_window, qtbot, tmp_path, monkeypatch):
    entries = open_all(qtbot, shown_window, [write_signal_session(tmp_path / "a.mefd")])
    dialog = make_dialog(qtbot, shown_window, entries)
    reach_cut_step(qtbot, dialog)
    calls = []
    real = P.plan_pieces
    monkeypatch.setattr(P, "plan_pieces", lambda *a, **k: calls.append(1) or real(*a, **k))
    dialog.at_input.set_us(T0 + 30 * SECOND, dialog.zone)
    dialog.add_time()
    assert len(calls) == 1
    dialog.blocker()
    dialog.new_names()
    dialog._changed()
    assert len(calls) == 1
    dialog.every.setValue(2)
    dialog.mode_every.setChecked(True)  # a change of choice: one more
    assert len(calls) >= 2
    before = len(calls)
    dialog._changed()
    dialog.blocker()
    assert len(calls) == before
    dialog.reject()


def test_a_gap_shorter_than_the_explorers_merge_does_not_make_a_piece_look_full(
    shown_window, qtbot, tmp_path
):
    from mef3io_studio.model.coverage import RecordingInput, recording_coverage
    from mef3io_studio.workers.tasks import data_stretches

    fs = 256.0
    period = int(1e6 / fs)
    path = tmp_path / "tiny.mefd"
    n = 2560
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        w.write("a1", np.arange(n, dtype=float), T0, fs)
        second = T0 + int(n * 1e6 / fs) + int(1.2 * period)  # a gap of 1.2 periods
        w.write("a1", np.arange(n, dtype=float), second, fs, new_segment=True)
    entries = open_all(qtbot, shown_window, [path])
    item = RecordingInput(entries[0].key, entries[0].name, entries[0].details)
    assert len(recording_coverage(item).runs) == 1  # the Explorer joins them
    runs, verified = data_stretches(None, item)
    assert verified and len(runs) == 2
    cuts = (runs[0][1] + 1000, runs[0][1] + 2000)
    plan = P.plan_pieces(runs, 1, P.Choice(P.AT, at=cuts))
    assert plan.left_out == 1 and len(plan.pieces) == 2


def test_piece_name_problems_say_piece(shown_window, qtbot, tmp_path):
    entries = open_all(qtbot, shown_window, [write_signal_session(tmp_path / "a.mefd")])
    dialog = make_dialog(qtbot, shown_window, entries)
    reach_cut_step(qtbot, dialog)
    for s in (30, 60):
        dialog.at_input.set_us(T0 + s * SECOND, dialog.zone)
        dialog.add_time()
    dialog.next()
    dialog.next()
    dialog.folder = str(tmp_path)
    dialog.pattern.setText("same-<date>")
    assert dialog.problem.text().startswith("Pieces 1 and 2 of 3")
    dialog.reject()


def test_a_piece_in_a_gap_inside_a_stored_part_is_left_out_not_a_stop(
    shown_window, qtbot, tmp_path
):
    """The Explorer's stretches cannot see a gap between blocks of ONE stored
    part; mef3io refuses a piece cut there ("no recording"). That piece is
    left out with a note, the others are made, nothing is half-made."""
    rng = np.random.default_rng(2)
    data = rng.integers(-5000, 5000, 90 * 256).astype(float)
    data[30 * 256 : 60 * 256] = np.nan
    source = tmp_path / "Hole.mefd"
    with mef3io.Writer(str(source), overwrite=True, units="uV") as w:
        w.write("a1", data, T0, 256.0)
    out = tmp_path / "out"
    out.mkdir()
    before = tree_hash(source)
    entries = open_all(qtbot, shown_window, [source])
    dialog = make_dialog(qtbot, shown_window, entries)
    reach_cut_step(qtbot, dialog)
    assert len(dialog.runs) == 1  # one stored part: the plan sees no gap
    for s in (40, 50):
        dialog.at_input.set_us(T0 + s * SECOND, dialog.zone)
        dialog.add_time()
    assert len(dialog.plan().pieces) == 3
    dialog.next()
    dialog.next()
    dialog.pattern.setText("Cut-<number>")
    dialog.folder = str(out)
    dialog._changed()
    assert dialog.start() is not None
    qtbot.waitUntil(lambda: dialog.finished_run, timeout=180_000)
    assert sorted(p.name for p in out.iterdir()) == ["Cut-01.mefd", "Cut-03.mefd"]
    assert len(dialog.batch.items) == 2
    texts = [
        dialog.messages.itemAt(i).widget().text()
        for i in range(dialog.messages.count())
        if hasattr(dialog.messages.itemAt(i).widget(), "text")
    ]
    assert "Piece 2 holds no signal (a gap in the recording) and is left out." in texts
    assert dialog.summary.text() == "Finished: 2 of 2 pieces saved and checked."
    assert not list(out.rglob("*.partial")) and tree_hash(source) == before
    dialog.reject()
