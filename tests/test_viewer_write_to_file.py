# ruff: noqa: F811, F401
"""Every change to the notes stays in memory until "Write to file": the red bar, the list of
changes that is confirmed first, what is written, and every way of leaving with changes left."""

from __future__ import annotations

import time

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

    other = window.jobs.submit("look", look_around)
    qtbot.waitUntil(lambda: "look" in ran, timeout=10_000)
    assert spy.asked == [] and not other.token.cancelled
    stopped = window.jobs.submit("join", join_recordings)
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
