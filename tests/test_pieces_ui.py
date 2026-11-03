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
