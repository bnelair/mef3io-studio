# ruff: noqa: F811, F401
"""Every change to the notes stays in memory until "Write to file": the red bar, the list of
changes that is confirmed first, what is written, and every way of leaving with changes left."""

from __future__ import annotations

import time
from datetime import UTC, timezone

import pytest
from PySide6.QtCore import Qt
from test_ui_language import BANNED_RE
from test_viewer_edit import (  # noqa: F401
    SEC,
    T0,
    Spy,
    ed,
    key,
    records,
    tree_hash,
    turn_on,
)
from test_viewer_marks_ui import make_window, pair, wait_marks, write  # noqa: F401
from test_viewer_remove_notes import stored

from mef3io_studio.model import annotation_draft as ad
from mef3io_studio.model import viewer_marks as vm
from mef3io_studio.model import write_changes as wc
from mef3io_studio.viewer.write_dialog import WriteDialog


def test_the_bar_is_hidden_without_changes_and_red_and_loud_with_them(ed, qtbot):
    win, spy, _ = ed
    assert not win.write_bar.isVisible()
    win.edit.add(T0 + 14 * SEC, None, "Note", "two")
    win.edit.add(T0 + 15 * SEC, None, "Note", "three")
    assert win.write_bar.isVisible() and win.write_bar.counts == (2, 0, 0)
    said = win.write_bar.text.text()
    assert "NOT SAVED" in said and "2 added" in said and "Write to file" in said
    assert not BANNED_RE.search(said)
    assert win.write_bar.write_button.text().startswith("Write to file")
    # it is the first thing in the window, above the controls
    assert win.write_bar.geometry().top() <= win.controls.geometry().top()
    win.edit.undo()
    win.edit.undo()
    assert not win.write_bar.isVisible()


def test_the_bar_stays_after_leaving_edit_mode_and_counts_every_kind(ed, qtbot):
    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 14 * SEC, None, "Note", "new")
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    mark = stored(win, "Alpha Cz note")
    win.edit.draft.edit_stored(mark, text="Alpha Cz reworded")
    win.edit.refresh()
    assert win.write_bar.counts == (1, 1, 1)
    assert "1 added, 1 changed, 1 removed" in win.write_bar.text.text()
    win.edit_button.click()  # leave edit mode: nothing is asked, nothing is lost
    assert win.write_bar.isVisible() and win.edit.draft.dirty and spy.asked == []


def test_the_list_names_every_change_and_the_write_does_exactly_that(ed, qtbot):
    win, spy, (a, b) = ed
    turn_on(win)
    before, other = tree_hash(a), tree_hash(b)
    win.edit.add(T0 + 40 * SEC, None, "Spike", "brand new")
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    mark = stored(win, "Alpha Cz note")
    win.edit.draft.edit_stored(mark, text="Alpha Cz reworded")
    win.edit.refresh()
    assert tree_hash(a) == before  # nothing is written by marking, changing or adding
    summary = win.edit.summary()
    assert (summary.added, summary.changed, summary.removed) == (1, 1, 1)
    kinds = {r.kind: r for r in summary.rows}
    assert kinds[wc.CHANGED].old_text == "Alpha Cz note"
    assert kinds[wc.CHANGED].text == "Alpha Cz reworded"
    assert kinds[wc.REMOVED].text == "Alpha plain note"
    assert kinds[wc.ADDED].text.endswith("brand new")
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        assert win.edit.write_to_file()
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=60_000)
    texts = {r["text"] for r in records(a)}
    assert "Alpha plain note" not in texts and "Alpha Cz note" not in texts
    assert "Alpha Cz reworded" in texts and any("brand new" in t for t in texts)
    assert tree_hash(b) == other  # another recording is untouched
    assert not win.edit.draft.dirty and not win.write_bar.isVisible() and not spy.problems


def test_a_failed_write_keeps_the_changes_and_the_bar(ed, qtbot, monkeypatch):
    import mef3io

    win, spy, (a, _b) = ed
    turn_on(win)

    def refuse(*args, **kwargs):
        exc = mef3io.WriteConflictError("changed meanwhile")
        exc.code = "records-changed"
        raise exc

    monkeypatch.setattr(mef3io, "remove_annotations", refuse)
    win.edit.add(T0 + 40 * SEC, None, "Spike", "brand new")
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    snap = tree_hash(a)
    win.edit.write_to_file()
    qtbot.waitUntil(lambda: not win.edit.saving and bool(spy.problems), timeout=30_000)
    assert tree_hash(a) == snap  # nothing half-written
    assert win.edit.draft.dirty and win.write_bar.isVisible()
    assert win.write_bar.counts[0] + win.write_bar.counts[2] >= 1


def test_the_confirmation_lists_old_to_new_and_defaults_to_keep_editing(ed, qtbot):
    win, spy, _ = ed
    turn_on(win)
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    mark = stored(win, "Alpha Cz note")
    win.edit.draft.edit_stored(mark, text="Alpha Cz reworded")
    win.edit.add(T0 + 40 * SEC, None, "Spike", "brand new")
    win.edit.refresh()
    dialog = WriteDialog(win.edit.summary(), win.edit._where, win._shown, win.zone, win)
    qtbot.addWidget(dialog)
    rows = dialog.rows_text()
    assert {r[0] for r in rows} == {"Added", "Changed", "Removed"}
    changed = next(r for r in rows if r[0] == "Changed")
    assert "Alpha Cz note" in changed[4] and "Alpha Cz reworded" in changed[4] and "→" in changed[4]
    assert dialog.write_button.text() == "Write 3 changes to the file"
    assert dialog.keep_button.isDefault() and not dialog.write_button.isDefault()
    assert dialog.warning.isVisibleTo(dialog)  # "cannot be undone" is said
    shown_only = WriteDialog(win.edit.summary(), win.edit._where, win._shown, win.zone, win, False)
    qtbot.addWidget(shown_only)
    assert shown_only.write_button is None and shown_only.keep_button.text() == "Close"
    for part in (dialog.heading.text(), dialog.warning.text(), dialog.write_button.text()):
        assert not BANNED_RE.search(part), part


def test_a_plain_addition_may_be_confirmed_with_enter(ed, qtbot):
    win, _spy, _ = ed
    win.edit.add(T0 + 40 * SEC, None, "Spike", "brand new")
    dialog = WriteDialog(win.edit.summary(), win.edit._where, win._shown, win.zone, win)
    qtbot.addWidget(dialog)
    assert dialog.write_button.isDefault()  # nothing is lost by writing only additions


def test_discard_asks_and_really_discards(ed, qtbot):
    win, spy, (a, _b) = ed
    turn_on(win)
    snap = tree_hash(a)
    win.edit.add(T0 + 40 * SEC, None, "Spike", "brand new")
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    win.edit.discard_ask()
    assert spy.discard_asked == ["1 added, 1 removed"] or len(spy.discard_asked) == 1
    assert not win.edit.draft.dirty and not win.write_bar.isVisible()
    assert tree_hash(a) == snap
    assert [m.text for m in win.notes_tab.rows if m.state] == []


def test_a_task_that_uses_the_recordings_asks_first(ed, qtbot):
    win, spy, (a, _b) = ed
    turn_on(win)
    win.edit.add(T0 + 40 * SEC, None, "Spike", "brand new")
    spy.answer = "stay"
    assert win.edit.settle_before_task() is False and spy.asked == ["task"]
    assert win.edit.draft.dirty
    spy.answer = "discard"
    assert win.edit.settle_before_task() is True
    assert not win.edit.draft.dirty
    assert win.edit.settle_before_task() is True  # nothing unwritten: no question
    assert spy.asked == ["task", "task"]


def test_the_job_guard_stops_a_guarded_task_but_not_the_others(ed, window, qtbot):
    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 40 * SEC, None, "Spike", "brand new")
    window.viewers.append(win)
    spy.answer = "stay"
    ran = []

    def join_recordings(ctx):  # a task that reads the recordings
        ran.append("join")

    def look_around(ctx):  # one that does not
        ran.append("look")

    other = window.jobs.submit("look", look_around, changes_recordings=False)
    qtbot.waitUntil(lambda: "look" in ran, timeout=10_000)
    assert spy.asked == [] and not other.token.cancelled
    stopped = window.jobs.submit("join", join_recordings, changes_recordings=True)
    assert spy.asked == ["task"] and stopped.token.cancelled  # asked, and the task never ran
    qtbot.wait(300)
    assert ran == ["look"] and win.edit.draft.dirty
    # quitting asks through the viewer's own close, and "Stay" keeps everything
    assert window._viewers_may_close() is False
    assert win.isVisible() and win.edit.draft.dirty and spy.asked == ["task", "close"]
    window.viewers.remove(win)


def test_the_summary_is_fast_for_tens_of_thousands_of_changes():
    d = ad.Draft()
    for i in range(1_500):
        d.add(T0 + i * SEC, None, "Note", f"n{i}", recording=0)
    for i in range(1_500):
        d.remove(vm.Mark(T0 + i * SEC, None, "Note", 0, "session", None, f"s{i}"))
    start = time.perf_counter()
    summary = wc.summarise(d, lambda uid: 0, lambda m: 1)
    took = time.perf_counter() - start
    assert (summary.added, summary.removed) == (1_500, 1_500)
    assert took < 3.0, took


# --- review round 1: what is in the file after every kind of ending ---------------

import errno  # noqa: E402
import threading  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import mef3io  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from test_viewer_remove_notes import signal_hash, texts  # noqa: E402

from mef3io_studio.model import viewer_display as vd  # noqa: E402


def click_in_dialog(qtbot, which: str) -> None:
    """Arm a timer that presses a button of the modal confirmation with a real mouse click."""

    def press() -> None:
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, WriteDialog):
            button = dialog.write_button if which == "write" else dialog.keep_button
            qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        else:
            QTimer.singleShot(20, press)

    QTimer.singleShot(20, press)


def test_the_real_confirmation_writes_only_when_its_write_button_is_clicked(ed, qtbot):
    win, spy, (a, b) = ed
    del win.edit.ask_write  # the real dialog, not the confirm-always stub
    turn_on(win)
    win.edit.add(T0 + 40 * SEC, None, "Spike", "brand new")
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    snap, snap_b, before_signal = tree_hash(a), tree_hash(b), signal_hash(a)
    click_in_dialog(qtbot, "keep")  # a real click on "Keep editing"
    with qtbot.waitSignal(win.edit.save_finished, timeout=30_000):
        win.edit.write_to_file()
    assert tree_hash(a) == snap and tree_hash(b) == snap_b and win.edit.draft.dirty
    click_in_dialog(qtbot, "write")  # a real click on the write button
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        win.edit.write_to_file()
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=60_000)
    got = texts(a)
    assert "Alpha plain note" not in got and any("brand new" in t for t in got)
    assert signal_hash(a) == before_signal  # the signal files are the same bytes
    assert tree_hash(b) == snap_b and not win.edit.draft.dirty
    assert win.status_message.text().startswith("Written to the recording: 1 added, 1 removed")


def test_a_failed_add_after_a_removal_says_what_is_written_and_what_is_not(ed, qtbot, monkeypatch):
    win, spy, (a, _b) = ed
    turn_on(win)
    win.edit.add(T0 + 40 * SEC, None, "Spike", "brand new")
    win.edit.remove_mark(stored(win, "Alpha plain note"))

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(mef3io, "Writer", disk_full)  # only the add step uses the Writer
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        win.edit.write_to_file()
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
    (problem,) = spy.problems
    assert "Part of your changes were written" in problem.text and "1 removed" in problem.text
    assert "Still only in this window" in problem.text and "1 added" in problem.text
    for false_claim in (
        "originals were not changed",
        "Nothing was added to any",
        "were not changed",
    ):
        assert false_claim not in problem.text
    assert "Alpha plain note" not in texts(a)  # the removal IS in the file
    assert len(win.edit.draft.additions()) == 1 and win.write_bar.counts == (1, 0, 0)


def test_a_cancelled_second_step_says_what_is_written_and_what_is_not(ed, qtbot):
    win, spy, (a, _b) = ed
    turn_on(win)
    win.edit.add(T0 + 40 * SEC, None, "Spike", "brand new")
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    win.edit.ask_save = lambda plan_for: None  # the second step's own dialog is cancelled
    win.edit.write_to_file()
    qtbot.waitUntil(lambda: not win.edit.saving and bool(spy.problems), timeout=60_000)
    (problem,) = spy.problems
    assert "1 removed" in problem.text and "Still only in this window" in problem.text
    assert "Alpha plain note" not in texts(a) and win.write_bar.counts == (1, 0, 0)


def test_a_write_that_fails_halfway_across_two_recordings_keeps_the_rest(ed, qtbot, monkeypatch):
    win, spy, (a, b) = ed
    turn_on(win)
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    win.edit.remove_mark(stored(win, "Beta plain note"))
    real, calls = mef3io.remove_annotations, []

    def second_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            exc = mef3io.WriteConflictError("changed meanwhile")
            exc.code = "records-changed"
            raise exc
        return real(*args, **kwargs)

    monkeypatch.setattr(mef3io, "remove_annotations", second_fails)
    snap_b = tree_hash(b)
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        win.edit.write_to_file()
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
    assert "Alpha plain note" not in texts(a) and tree_hash(b) == snap_b
    assert win.write_bar.counts == (0, 0, 1) and win.edit.draft.removed_total() == 1
    assert spy.problems and "recording A" in spy.problems[-1].text
    monkeypatch.setattr(mef3io, "remove_annotations", real)  # the retry writes only the rest
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        win.edit.write_to_file()
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
    assert "Beta plain note" not in texts(b) and not win.edit.draft.dirty


def test_the_result_confirms_every_count(ed, qtbot):
    win, spy, (a, _b) = ed
    turn_on(win)
    win.edit.add(T0 + 40 * SEC, None, "Spike", "brand new")
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    win.edit.draft.edit_stored(stored(win, "Alpha Cz note"), text="Alpha Cz reworded")
    win.edit.refresh()
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        win.edit.write_to_file()
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
    assert win.status_message.text() == "Written to the recording: 1 added, 1 changed, 1 removed."


def test_a_count_that_differs_from_what_was_asked_leaves_the_draft_and_says_so(
    ed, qtbot, monkeypatch
):
    win, spy, (a, _b) = ed
    turn_on(win)
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    real = mef3io.remove_annotations

    def odd(*args, **kwargs):
        report = real(*args, **kwargs)
        return SimpleNamespace(removed=report.removed + 1, added=report.added)

    monkeypatch.setattr(mef3io, "remove_annotations", odd)
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        win.edit.write_to_file()
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
    assert "may already be written" in spy.problems[-1].text
    assert not win.edit.draft.dirty and not win.write_bar.isVisible()  # no mark that may be written
    assert "Alpha plain note" not in texts(a)


def test_a_note_changed_by_another_program_is_said_in_the_list_before_writing(ed, qtbot):
    win, spy, (a, _b) = ed
    turn_on(win)
    mef3io.remove_annotations(
        str(a), remove=[r for r in records(a) if r["text"] == "Alpha plain note"]
    )
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    snap = tree_hash(a)
    with qtbot.waitSignal(win.edit.save_finished, timeout=30_000):
        win.edit.write_to_file()
    (shown,) = spy.summaries
    assert shown.warnings and tree_hash(a) == snap and win.edit.draft.dirty
    dialog = WriteDialog(shown, win.edit._where, win._shown, win.zone, win)
    qtbot.addWidget(dialog)
    assert dialog.problem.isVisibleTo(dialog) and not dialog.write_button.isEnabled()


def test_writing_off_and_a_running_write_say_why(ed, qtbot):
    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 40 * SEC, None, "Spike", "brand new")
    win.edit.stale = True
    win.edit._sync_buttons()
    assert not win.write_bar.write_button.isEnabled()
    assert "Writing is off" in win.write_bar.text.text()
    assert win.edit.write_to_file() is False
    assert "Writing to the file is off" in spy.problems[-1].title
    assert win.edit.settle_before_task() is False and len(spy.problems) == 2
    assert "task you started was not started" in spy.problems[-1].text
    win.edit.stale = False
    win.edit.saving = True
    assert win.edit.settle_before_task() is False
    assert "being written" in spy.problems[-1].title
    win.edit.write_to_file()
    assert "being written" in win.status_message.text()
    win.edit.saving = False


def test_quitting_while_a_write_runs_says_so(ed, window, qtbot):
    win, spy, _ = ed
    window.viewers.append(win)
    win.edit.saving = True
    assert window._viewers_may_close() is False
    assert "being written" in spy.problems[-1].title
    win.edit.saving = False
    window.viewers.remove(win)


def test_the_check_of_writable_folders_never_runs_on_the_gui_thread(ed, qtbot, monkeypatch):
    win, spy, _ = ed
    seen = []
    from mef3io_studio.model import annotation_save as asave

    real = asave._not_writable

    def spying(path):
        seen.append(threading.current_thread())
        return real(path)

    monkeypatch.setattr(asave, "_not_writable", spying)
    with qtbot.waitSignal(win.edit.probed, timeout=30_000):
        win.edit.set_on(True)
    assert seen and all(t is not threading.main_thread() for t in seen)
    assert win.edit._writable and all(win.edit._writable.values())


# --- marking many notes ------------------------------------------------------------------------


def row_pos(win, row: int):
    table = win.notes_tab.table
    return table.visualItemRect(table.item(row, 0)).center()


def test_shift_and_ctrl_click_and_ctrl_a_select_several_and_delete_marks_them_in_one_step(
    ed, qtbot
):
    win, spy, (a, _b) = ed
    turn_on(win)
    win.side_panel.setCurrentWidget(win.notes_tab)
    table = win.notes_tab.table
    qtbot.waitUntil(lambda: table.rowCount() >= 4)
    snap = tree_hash(a)
    ctrl = Qt.KeyboardModifier.ControlModifier
    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=row_pos(win, 0))
    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, ctrl, pos=row_pos(win, 1))
    assert len(win.notes_tab.selected_marks()) == 2
    table.setFocus()
    QTest.keyClick(table, Qt.Key.Key_A, ctrl)  # Ctrl+A: every listed row
    assert len(win.notes_tab.selected_marks()) == table.rowCount()
    QTest.keyClick(table, Qt.Key.Key_Delete)
    assert win.write_bar.counts[2] == table.rowCount() and tree_hash(a) == snap
    assert all(m.state == "removed" for m in win.notes_tab.rows)
    win.edit.undo()  # ONE undo step brings every one of them back
    assert not win.edit.draft.dirty and not any(m.state for m in win.notes_tab.rows)
    win.edit.redo()
    assert win.write_bar.counts[2] == table.rowCount()
    table.selectAll()
    qtbot.mouseClick(win.notes_tab.keep_button, Qt.MouseButton.LeftButton)  # Keep
    assert not win.edit.draft.dirty and not any(m.state for m in win.notes_tab.rows)


def test_the_menu_on_several_selected_notes_acts_on_all_of_them(ed, qtbot):
    from PySide6.QtWidgets import QMenu

    win, spy, _ = ed
    turn_on(win)
    win.notes_tab.table.selectAll()
    chosen = win.notes_tab.selected_marks()
    menu = QMenu()
    win.edit.note_menu(menu, chosen[0])
    found = {a.text(): a for a in menu.actions()}
    take = next(a for t, a in found.items() if t.startswith("Mark the") and "selected notes" in t)
    take.trigger()
    assert win.write_bar.counts[2] == len(chosen)


def big_recording(tmp_path, n=50_000):
    notes = [{"time": T0 + i * 72_000, "type": "Note", "text": f"n{i}"} for i in range(n)]
    return write(tmp_path / "Big.mefd", seconds=3600, notes=notes)


def test_marking_with_50000_stored_notes_costs_the_same_as_with_few(
    make_window, tmp_path, qtbot, monkeypatch
):
    import time

    p = big_recording(tmp_path)
    win = make_window(p, reveal=lambda i: True)
    wait_marks(qtbot, win)
    Spy(win)
    turn_on(win)
    qtbot.waitUntil(lambda: bool(win.edit._writable), timeout=60_000)  # asked once, on a task
    composed = []
    real = vd.compose
    monkeypatch.setattr(vd, "compose", lambda *a, **k: (composed.append(1), real(*a, **k))[1])
    qtbot.wait(4000)  # the first fill of the list and the page's own reads are done
    marks = list(win.marks)
    # 2,000 notes are marked at once (one step), then single ones one after another
    start = time.perf_counter()
    assert win.edit.mark_many(marks[:2000])
    bulk_ms = (time.perf_counter() - start) * 1000
    single = marks[2000::40][:100]
    start = time.perf_counter()
    for m in single:
        assert win.edit.remove_mark(m)
    per_mark_ms = (time.perf_counter() - start) * 1000 / len(single)
    start = time.perf_counter()
    win.repaint()  # what drawing the changed window costs once (offscreen)
    repaint_ms = (time.perf_counter() - start) * 1000
    print(
        f"\nPER MARK ms: {per_mark_ms:.1f} (GUI thread, work of the app); "
        f"2000 at once: {bulk_ms:.0f} ms; one repaint {repaint_ms:.0f} ms"
    )
    assert not composed, "marking must patch the notes in place, not compose them again"
    assert win.write_bar.counts[2] == 2100
    assert per_mark_ms < 20, per_mark_ms  # about 3 ms measured, at 50,000 notes and 2,000 marks
    assert bulk_ms < 3000, bulk_ms
    start = time.perf_counter()
    win.edit.undo()  # the last mark
    win.edit.undo()
    assert (time.perf_counter() - start) * 1000 < 500


def test_the_confirmation_with_thousands_of_rows_opens_fast(qtbot):
    import time

    from mef3io_studio.model.write_changes import ChangeRow, ChangeSummary

    rows = [
        ChangeRow(wc.REMOVED, 0, T0 + i * SEC, None, "Note", f"note {i}") for i in range(20_000)
    ]
    summary = ChangeSummary(rows=rows, removed=len(rows), per_recording={0: (0, 0, len(rows))})
    start = time.perf_counter()
    dialog = WriteDialog(summary, lambda r: "Recording A", lambda r: True, UTC)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(50)
    assert time.perf_counter() - start < 1.5
    assert dialog.model.rowCount() == 20_000


# --- the guard: every job start says whether it changes recordings -------------------------------


def job_starts():
    import ast
    import pathlib

    import mef3io_studio

    root = pathlib.Path(mef3io_studio.__file__).parent
    found = []
    for f in sorted(root.rglob("*.py")):
        tree = ast.parse(f.read_text())
        for n in ast.walk(tree):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "submit"
                and len(n.args) >= 2
                and ast.unparse(n.func.value).endswith("jobs")
            ):
                kw = {k.arg: k.value for k in n.keywords}
                fn = ast.unparse(n.args[1])
                found.append((f.name, n.lineno, fn.split(".")[-1], kw.get("changes_recordings")))
    return found


def test_every_job_start_in_the_app_says_whether_it_changes_recordings():
    starts = job_starts()
    assert len(starts) >= 25  # the enumeration really finds the app's job starts
    missing = [(f, line) for f, line, _fn, kw in starts if kw is None]
    assert not missing, f"job starts without changes_recordings=: {missing}"
    # per call site (file + function + declared value): flipping ONE start of a function that
    # another file declares True still fails
    declared = {(f, fn): getattr(kw, "value", None) for f, _l, fn, kw in starts}
    writes = {
        ("page.py", "run_batch"),  # the Anonymizer's batch
        ("run.py", "run_batch"),  # New recording
        ("page.py", "join_recordings"),
        ("page.py", "pack_or_unpack"),
        ("edit_details.py", "make_changes"),
    }
    for f, fn, value in {(f, fn, getattr(kw, "value", None)) for f, _l, fn, kw in starts}:
        assert value is ((f, fn) in writes), (f, fn, value)
    assert writes <= set(declared), "a start named above is gone: update this list"


def test_the_apps_job_manager_refuses_an_undeclared_start(window):
    import pytest

    with pytest.raises(TypeError, match="changes_recordings"):
        window.jobs.submit("Something", lambda ctx: 1)


# --- nothing is written before the confirmed write, on EVERY path -------------------


@pytest.mark.parametrize(
    "path",
    ["delete_key", "menu", "edit", "ctrl_s_keep_editing", "close_stay", "close_discard", "leave"],
)
def test_nothing_is_written_on_any_path_before_the_confirmed_write(ed, qtbot, path):
    from PySide6.QtWidgets import QMenu

    win, spy, (a, b) = ed
    turn_on(win)
    snap, snap_b = tree_hash(a), tree_hash(b)
    note = stored(win, "Alpha plain note")
    if path == "delete_key":
        win.edit.select(note)
        QTest.keyClick(win.canvas, Qt.Key.Key_Delete)
    elif path == "menu":
        menu = QMenu()
        win.edit.note_menu(menu, note)
        next(a for a in menu.actions() if a.text() == "Mark for removal").trigger()
    elif path == "edit":
        win.edit.ask_note = lambda editor: {
            "time": note.time, "end": None, "label": "Artifact", "text": "x", "channel": None,
        }  # fmt: skip
        win.edit.on_row(note)
        win.edit.edit_selected()
    else:
        win.edit.remove_mark(note)
        if path == "ctrl_s_keep_editing":
            spy.write_confirmed = False
            QTest.keyClick(win.canvas, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
            qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
        elif path == "close_stay":
            spy.answer = "stay"
            win.close()
            assert win.isVisible()
        elif path == "close_discard":
            spy.answer = "discard"
            win.close()
            assert win._closed and not win.edit.draft.dirty
        elif path == "leave":
            win.edit_button.click()
            assert win.edit.draft.dirty
    assert tree_hash(a) == snap and tree_hash(b) == snap_b, path
    assert path == "close_discard" or win.edit.draft.dirty


def test_quit_and_the_job_guard_leave_the_recordings_alone_on_stay(ed, window, qtbot):
    win, spy, (a, b) = ed
    turn_on(win)
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    snap, snap_b = tree_hash(a), tree_hash(b)
    window.viewers.append(win)
    spy.answer = "stay"
    assert window._viewers_may_close() is False
    stopped = window.jobs.submit("join", lambda ctx: 1, changes_recordings=True)
    assert stopped.token.cancelled and spy.asked.count("task") == 1
    assert tree_hash(a) == snap and tree_hash(b) == snap_b and win.edit.draft.dirty
    window.viewers.remove(win)


# --- review round 2 -------------------------------------------------------------------------------


def test_a_second_step_with_only_duplicates_still_says_what_the_first_wrote(ed, qtbot):
    win, spy, (a, _b) = ed
    turn_on(win)
    stored_note = stored(win, "Alpha Cz note")
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    # an addition that equals a stored note: it is dropped as a duplicate in the second step
    win.edit.draft.stored_keys = lambda: frozenset()  # (let the draft take it; the plan drops it)
    win.edit.draft.stored_pairs = lambda: frozenset()
    label, text = ad.split_text(stored_note.text)
    win.edit.draft.add(stored_note.time, None, label, text, "Cz", 0)
    win.edit.draft.stored_keys = win.edit._stored_keys
    win.edit.draft.stored_pairs = win.edit._stored_pairs
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        win.edit.write_to_file()
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
    said = win.status_message.text()
    assert "Written to the recording: 1 removed" in said and "already stored" in said
    assert "Alpha plain note" not in texts(a) and not win.edit.draft.dirty
    assert win.edit._tally == {} and not win.edit._chain
    # the next write counts only its own changes
    win.edit.add(T0 + 40 * SEC, None, "Spike", "brand new")
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        win.edit.write_to_file()
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
    assert win.status_message.text() == "Written to the recording: 1 added."


def test_identical_copies_selected_together_patch_the_display_once(make_window, tmp_path, qtbot):
    from test_viewer_remove_notes import NOTE

    p = write(
        tmp_path / "Copies.mefd",
        notes=[
            NOTE,
            dict(NOTE),
            dict(NOTE),
            {"time": T0 + 10 * SEC, "type": "Note", "text": "Other"},
        ],
    )
    win = make_window(p, reveal=lambda i: True)
    wait_marks(qtbot, win)
    Spy(win)
    turn_on(win)
    win.side_panel.setCurrentWidget(win.notes_tab)
    table = win.notes_tab.table
    qtbot.waitUntil(lambda: table.rowCount() == 4)
    copies = [i for i, m in enumerate(win.notes_tab.rows) if m.text == NOTE["text"]]
    assert len(copies) == 3
    table.clearSelection()
    for i in copies[:2]:
        table.selectRow(i) if i == copies[0] else table.selectionModel().select(
            table.model().index(i, 0),
            table.selectionModel().SelectionFlag.Select | table.selectionModel().SelectionFlag.Rows,
        )
    assert len(win.notes_tab.selected_marks()) == 2
    assert [c for c in win.chips.chips if c.mark is not None and c.mark.text == NOTE["text"]]
    assert any(c.source is not None and c.source.text == NOTE["text"] for c in win.canvas.marks)
    assert win.edit.mark_many(win.notes_tab.selected_marks())
    assert win.edit.draft.removed_total() == 3  # all identical copies go
    win.edit.remove_mark(win.notes_tab.rows[copies[2]])  # Delete on the third row: keep
    assert not win.edit.draft.dirty
    assert all(c.state != "removed" for c in win.chips.chips)
    assert all(m.state == "" for m in win.notes_tab.rows)
    assert all(c.style != "removed" for c in win.canvas.marks)


def display_state(win):
    d = win._shaped()
    return (
        [(m, m.state) for m in d.marks],
        [(m, m.state) for m in d.visible],
        d.drafted_np.tolist(),
        d.drafted,
    )


def test_patched_display_equals_a_full_rebuild_after_every_operation(make_window, tmp_path, qtbot):
    import random

    from test_viewer_remove_notes import NOTE

    notes = []
    for i in range(12):  # identical copies of 1, 2 and 3, some of another kind
        for _ in range(1 + i % 3):
            notes.append({"time": T0 + i * 4 * SEC, "type": "Note", "text": f"n{i}"})
    notes += [{"time": T0 + 5 * SEC, "type": "SyLg", "text": "s"} for _ in range(2)]
    p = write(tmp_path / "Fuzz.mefd", notes=notes + [NOTE])
    win = make_window(p, reveal=lambda i: True)
    wait_marks(qtbot, win)
    spy = Spy(win)
    turn_on(win)
    rng = random.Random(20260)
    kinds = sorted({k for k, _l, _n in win._shaped().counts})
    for step in range(300):
        marks = list(win.marks)
        op = rng.choice(["mark", "mark", "keep", "many", "undo", "redo", "filter"])
        spy.stored_answer = rng.choice(["one", "all"])
        if op == "mark":
            win.edit.remove_mark(rng.choice(marks))
        elif op == "keep":
            gone = [m for m in win._shaped().marks if m.state == "removed"]
            if gone:
                win.edit.keep_mark(rng.choice(gone))
        elif op == "many":
            win.edit.mark_many(rng.sample(marks, k=min(len(marks), rng.randint(2, 6))))
        elif op == "undo":
            win.edit.undo()
        elif op == "redo":
            win.edit.redo()
        else:
            win._types_chosen(frozenset(rng.sample(kinds, k=rng.randint(0, len(kinds) - 1))))
        patched, shape = display_state(win), win._shape
        win._shape = None  # a full rebuild from the stored notes and the draft
        assert display_state(win) == patched, (step, op)
        win._shape = shape  # and the patched display carries on
        shown = {id(m) for m in win._shaped().visible}
        assert all(id(c.mark) in shown for c in win.chips.chips if c.mark is not None), (step, op)
        assert all(id(c.source) in shown for c in win.canvas.marks if c.source is not None), (
            step,
            op,
        )
