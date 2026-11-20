# ruff: noqa: F811, F401
"""The viewer's edit mode, offscreen (docs/design/viewer.md, "Edit mode"):
off on every open, keys only inside it, the draft with undo, saving by
APPENDING (the old notes stay as they were), routing, stored notes shown as
changed but never written, import preview, export round trip, the question
when leaving with a draft. Synthetic sessions in tmp_path only."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import mef3io
import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from test_ui_language import BANNED_RE
from test_viewer_marks_ui import Shown, make_window, pair, wait_marks, write
from test_viewer_marks_ui import at as _at

from mef3io_studio.common.theme import DARK
from mef3io_studio.model import annotation_draft as ad
from mef3io_studio.model.errors import UserMessage
from mef3io_studio.viewer import edit_mode
from mef3io_studio.viewer.edit_dialogs import ImportPreview, SavePreview
from mef3io_studio.viewer.edit_mode import BANNER_TEXT

T0 = 946_684_800_000_000
SEC = 1_000_000


def key(win, k, mods=Qt.KeyboardModifier.NoModifier):
    QTest.keyClick(win.canvas, k, mods)


def records(path, pwd=""):
    r = mef3io.Reader(str(path), pwd)
    try:
        return r.records(level="all")
    finally:
        r.close()


def comparable(recs):
    return [
        (r["level"], r.get("channel"), r["type"], int(r["time"]), r.get("text"), r.get("duration"))
        for r in recs
    ]


def tree_hash(path):
    """Size, mtime and content of every file: unchanged means untouched."""
    out = {}
    for f in sorted(Path(path).rglob("*")):
        if f.is_file():
            st = f.stat()
            out[f.relative_to(path).as_posix()] = (
                st.st_size,
                st.st_mtime_ns,
                hashlib.sha256(f.read_bytes()).hexdigest(),
            )
    return out


@pytest.fixture(autouse=True)
def no_real_dialogs(monkeypatch):
    """A test that forgets to answer must not block on a modal box."""
    from mef3io_studio.viewer import edit_mode

    monkeypatch.setattr(edit_mode, "ask_unsaved", lambda parent, what="leave": "discard")


class Spy:
    """Records what the window asked the user (it never opens a real dialog)."""

    def __init__(self, win):
        self.problems: list[UserMessage] = []
        self.asked: list[str] = []
        self.extra: dict[int, str] = {}  # the other password of a protected recording
        self.answer = "discard"  # what a window left with a draft gets at teardown
        win.edit.show_problem = self.problems.append
        win.edit.ask_unsaved = lambda what: (self.asked.append(what), self.answer)[1]
        win.edit.ask_save = lambda plan_for: ({}, self.extra)
        #: The question before a stored note is taken out or replaced (default: yes, all of
        #: them) and the other password of a protected recording.
        self.stored_lines: list[str] = []
        self.stored_answer = "all"
        win.edit.ask_remove_stored = lambda line, copies: (
            self.stored_lines.append(line),
            self.stored_answer,
        )[1]
        win.edit.ask_other_password = lambda label, letter: self.extra.get(0)
        #: "Write to file": the list that was shown (default: confirmed), "Show changes" and the
        #: question before changes are discarded (default: yes).
        self.summaries: list = []
        self.write_confirmed = True
        win.edit.ask_write = lambda summary: (self.summaries.append(summary), self.write_confirmed)[
            1
        ]
        win.edit.show_summary = self.summaries.append
        self.discard_asked: list[str] = []
        win.edit.ask_discard = lambda counts: (self.discard_asked.append(counts), True)[1]
        win.confirm = lambda *a: True
        win.ask_show = lambda indices: True


@pytest.fixture
def ed(make_window, pair, qtbot):
    shown = Shown()
    shown.show(0)
    shown.show(1)
    win = make_window(*pair, reveal=shown, show_details=shown.show)
    wait_marks(qtbot, win)
    wide(win)
    return win, Spy(win), pair


def at(win, t, row=0):
    """The pointer at time ``t``; it first leaves, so a move is always seen
    (the mouse may already rest at that spot from an earlier test)."""
    QTest.mouseMove(win.canvas, QPoint(3, 3))
    return _at(win, t, row)


def wide(win):
    """A page long enough for the whole synthetic recording."""
    win.set_timebase(2)
    win.go_to(T0)


def turn_on(win):
    win.edit_button.click()
    assert win.edit.on


def finish_save(qtbot, win, **kw):
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        assert win.edit.save(**kw)
    qtbot.waitUntil(lambda: not win.edit.saving)
    from test_viewer_ui import settle

    settle(qtbot, win)


# --- off by default, keys inactive ------------------------------------------------------------


def test_edit_mode_is_off_on_open_and_its_keys_do_nothing(ed):
    win, spy, _ = ed
    assert not win.edit.on and not win.edit_button.isChecked() and win.edit_button.isVisible()
    assert not win.edit.banner.isVisible()
    at(win, T0 + 10 * SEC)
    for k in (Qt.Key.Key_A, Qt.Key.Key_2, Qt.Key.Key_1):
        key(win, k)
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert not win.edit.draft.dirty and not spy.problems
    assert not win.press("add_note") and not win.press("undo")
    assert "edit" not in win.features
    assert [t for k, t in win.help.rows if "edit mode" in t.lower()]


def test_the_switch_shows_the_banner_and_keys_work_only_inside(ed):
    win, _spy, _ = ed
    turn_on(win)
    assert win.edit.banner.isVisible() and win.edit.banner_label.text() == BANNER_TEXT
    assert (
        BANNER_TEXT == "Edit mode: Delete marks a note for removal, Ctrl+Z undoes the last change"
    )
    assert "edit" in win.features and not win.notes_tab.edit_bar.isHidden()
    win.edit_button.click()
    assert not win.edit.on and not win.edit.banner.isVisible()
    assert not not win.notes_tab.edit_bar.isHidden()


# --- adding, undo, redo -----------------------------------------------------------------------


def test_add_a_point_and_a_span_by_keys_and_dialog_with_undo_and_redo(ed):
    win, spy, _ = ed
    turn_on(win)
    # 'A' opens the editor (replaced here by its answer) at the pointer
    at(win, T0 + 10 * SEC)
    seen = {}

    def answer(editor):
        seen["start"] = editor.start_edit.text()
        seen["zone"] = editor.zone
        return {
            "time": T0 + 10 * SEC,
            "end": None,
            "label": "Note",
            "text": "typed words",
            "channel": None,
        }

    win.edit.ask_note = answer
    key(win, Qt.Key.Key_A)
    assert abs(ad.parse_local(seen["start"], seen["zone"]) - (T0 + 10 * SEC)) < 300_000
    assert len(seen["start"].rsplit(".", 1)[1]) == 3  # milliseconds, in the site zone
    # a digit adds the quick type at once (2 = Seizure onset), at the pointer
    at(win, T0 + 12 * SEC)
    key(win, Qt.Key.Key_2)
    # a Shift+drag selection gives a span (4 = Spike)
    win.canvas.selection = (T0 + 20 * SEC, T0 + 25 * SEC, 0)
    key(win, Qt.Key.Key_4)
    items = win.edit.draft.items
    assert [(i.label, i.text, i.is_span) for i in items] == [
        ("Note", "typed words", False),
        ("Seizure onset", "", False),
        ("Spike", "", True),
    ]
    assert items[2].end == T0 + 25 * SEC and items[2].time == T0 + 20 * SEC
    # shown as dashed/new draft marks, listed, and counted
    states = [m.state for m in win.notes_tab.rows if m.state]
    assert states == ["new", "new", "new"]
    assert any(c.state == "new" for c in win.chips.chips)
    assert "3 added" in win.write_bar.text.text()
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert len(win.edit.draft.items) == 2
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert not win.edit.draft.dirty
    mods = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    key(win, Qt.Key.Key_Z, mods)
    key(win, Qt.Key.Key_Z, mods)
    assert [i.label for i in win.edit.draft.items] == ["Note", "Seizure onset"]
    assert not spy.problems


def test_the_editor_dialog_checks_and_keeps_exact_microseconds(ed, qtbot):
    win, _spy, _ = ed
    turn_on(win)
    editor = win.edit._make_editor("Add a note or event")
    qtbot.addWidget(editor)
    t = T0 + 10 * SEC + 123_456
    editor.set_values(t, None, "Spike", "x", None)
    assert editor.start_edit.text().endswith(".123")
    editor._accept()
    assert editor.result() == 1 and editor.values()["time"] == t  # untouched: not rounded
    editor.start_edit.setText("tomorrow")
    editor._accept()
    assert "Write the time" in editor.error.text()
    editor.start_edit.setText("2000-01-01 00:01:10.000")  # in the gap between A and B
    editor._accept()
    assert "No recording holds data" in editor.error.text()
    editor.start_edit.setText("2000-01-01 00:00:10.500")
    editor.lasts.setChecked(True)
    editor.end_edit.setText("2000-01-01 00:00:10.100")
    editor._accept()
    assert "end must be after" in editor.error.text()
    editor.end_edit.setText("2000-01-01 00:00:12.000")
    editor.where_box.setCurrentIndex(editor.where_box.findData("Cz"))
    editor._accept()
    v = editor.values()
    assert (v["time"], v["end"], v["channel"]) == (T0 + 10_500_000, T0 + 12 * SEC, "Cz")


def test_a_note_outside_every_recording_is_refused_with_the_reason(ed):
    win, _spy, _ = ed
    turn_on(win)
    assert win.edit.add(T0 + 70 * SEC, None, "Spike") is None  # the gap between A and B
    assert "No recording holds data" in win.last_refusal and not win.edit.draft.dirty


# --- saving: append only ----------------------------------------------------------------------


def test_save_appends_and_leaves_the_old_notes_untouched(ed, qtbot):
    win, spy, (a, b) = ed
    before_a, before_b = records(a), records(b)
    rdat = next(Path(a).glob("*.rdat"))
    old_bytes = rdat.read_bytes()
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "sharp")  # A, whole recording
    win.edit.add(T0 + 35 * SEC, T0 + 38 * SEC, "Note", "span words", "Cz")  # A, channel
    win.edit.add(T0 + 120 * SEC, None, "Seizure onset", "")  # B
    epoch = win.data.epoch
    finish_save(qtbot, win)
    assert not spy.problems
    assert win.data.epoch == epoch  # only the notes were read again: nothing was reopened
    after_a, after_b = records(a), records(b)
    # the old ones are all still there, unchanged
    for old, new in ((before_a, after_a), (before_b, after_b)):
        olds = comparable(old)
        news = comparable(new)
        assert all(o in news for o in olds)
    added_a = [c for c in comparable(after_a) if c not in comparable(before_a)]
    added_b = [c for c in comparable(after_b) if c not in comparable(before_b)]
    assert sorted(added_a, key=lambda c: c[3]) == [
        ("session", None, "Note", T0 + 30 * SEC, "Spike: sharp", None),
        ("channel", "Cz", "EDFA", T0 + 35 * SEC, "span words", 3 * SEC),
    ]
    assert added_b == [("session", None, "Note", T0 + 120 * SEC, "Seizure onset:", None)]
    # the file grew by appending: its old entries are byte for byte where they were
    new_bytes = rdat.read_bytes()
    assert len(new_bytes) > len(old_bytes) and new_bytes[1024 : len(old_bytes)] == old_bytes[1024:]
    # what is shown is what is stored: the draft is gone, the list holds them as stored notes
    assert not win.edit.draft.dirty and not win.edit.draft.can_undo
    assert len(win.marks) == len(comparable(after_a)) + len(comparable(after_b))
    assert not [m for m in win.notes_tab.rows if m.state]
    assert "Written to the recordings" in win.status_message.text()
    assert "2 added" in win.status_message.text() and "1 added" in win.status_message.text()


def test_saving_twice_does_not_duplicate(ed, qtbot):
    win, spy, (a, _b) = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "once")
    finish_save(qtbot, win)
    n = len(records(a))
    assert win.edit.save() is False and not win.write_bar.isVisible()
    assert len(records(a)) == n and not spy.problems


def test_the_save_preview_lists_each_recording_the_warning_and_the_copy_sentence(ed, qtbot):
    win, _spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    win.edit.add(T0 + 31 * SEC, T0 + 33 * SEC, "Note", "y")
    win.edit.add(T0 + 120 * SEC, None, "Sleep", "")
    win.edit.draft.remove(win._display_marks()[0])  # a stored note taken out: needs a copy
    dialog = SavePreview(
        lambda c: win.edit.draft.save_plan(win.data.holders_at, c),
        win.recording_label,
        win._rec_letter,
        win.zone,
    )
    qtbot.addWidget(dialog)
    text = dialog.lines.text()
    assert "Recording A: Alpha_case: 2 notes and events, 2000-01-01 00:00:30.000 UTC to" in text
    assert "Recording B: Beta_case: 1 note or event, 2000-01-01 00:02:00.000 UTC" in text
    assert "cannot be undone in place" in dialog.warning.text()
    assert "1 change to a stored note is written first" in dialog.copy_note.text()
    assert "cannot be undone" in dialog.copy_note.text()
    assert dialog.ok.isEnabled() and dialog.ok.text() == "Add them to the recordings"
    assert not BANNED_RE.search(" ".join([text, dialog.warning.text(), dialog.copy_note.text()]))


def test_hidden_recordings_are_named_by_letter_in_the_preview(make_window, pair, qtbot):
    shown = Shown()
    win = make_window(*pair, reveal=shown, show_details=shown.show)
    wait_marks(qtbot, win)
    Spy(win)
    wide(win)
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "secret words")
    dialog = SavePreview(
        lambda c: win.edit.draft.save_plan(win.data.holders_at, c),
        win.recording_label,
        win._rec_letter,
        win.zone,
    )
    qtbot.addWidget(dialog)
    assert "Alpha" not in dialog.lines.text() and "Recording A:" in dialog.lines.text()
    assert "secret" not in dialog.lines.text()
    texts = [win.notes_tab.cell_text(r, 4) for r in range(win.notes_tab.table.rowCount())]
    assert texts and not any("secret" in t for t in texts)  # the list masks it too


def test_side_by_side_recordings_ask_where_a_whole_recording_note_goes(
    make_window,
    tmp_path,
    qtbot,
):
    a = write(tmp_path / "Left.mefd", channels=("Fp1", "Fp2"))
    b = write(tmp_path / "Right.mefd", channels=("Cz", "Pz"))
    win = make_window(a, b)
    wait_marks(qtbot, win)
    wide(win)
    spy = Spy(win)
    turn_on(win)
    win.edit.add(T0 + 10 * SEC, None, "Spike", "whole")
    win.edit.add(T0 + 11 * SEC, None, "Note", "on Cz", "Cz")
    plan = win.edit.draft.save_plan(win.data.holders_at)
    assert plan.choice_groups == [(0, 1)] and not plan.can_save
    dialog = SavePreview(
        lambda c: win.edit.draft.save_plan(win.data.holders_at, c),
        win.recording_label,
        win._rec_letter,
        win.zone,
    )
    qtbot.addWidget(dialog)
    assert (
        not dialog.ok.isEnabled()
        and "recordings A and B both hold data" in dialog.choice_layout.itemAt(0).widget().text()
    )
    combo = dialog.choice_combos[(0, 1)]
    combo.setCurrentIndex(combo.findData(0))
    assert dialog.ok.isEnabled() and dialog.choices() == {(0, 1): 0}
    win.edit.ask_save = lambda plan_for: ({(0, 1): 0}, {})
    finish_save(qtbot, win)
    assert not spy.problems
    left, right = comparable(records(a)), comparable(records(b))
    assert [c[4] for c in left] == ["Spike: whole"] and [c[4] for c in right] == ["on Cz"]
    assert right[0][:2] == ("channel", "Cz")


# --- stored notes: shown as changed, written by "remove + add" after one question -------------
# (docs/design/viewer.md, "Removing and changing a stored note"; tests/test_viewer_remove_notes.py)


def test_a_stored_note_taken_out_in_the_draft_is_shown_and_saved_after_one_question(ed, qtbot):
    win, spy, (a, b) = ed
    turn_on(win)
    stored = next(m for m in win.notes_tab.rows if m.text == "Alpha plain note")
    win.edit.draft.remove(stored)  # the draft's own way to mark a stored note as taken out
    win.edit.refresh()
    shown = [m for m in win.notes_tab.rows if m.state == "removed"]
    assert [m.text for m in shown] == ["Alpha plain note"]  # still listed, struck through
    assert [c.state for c in win.chips.chips if c.state] == ["removed"]
    assert [m.style for m in win.canvas.marks if m.style] == ["removed"]
    assert win.edit.draft.stored_changes == 1
    assert "1 removed" in win.write_bar.text.text()
    # undo brings the note back, nothing was asked or written
    snap = tree_hash(a)
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert not [m for m in win.notes_tab.rows if m.state] and not spy.stored_lines
    assert tree_hash(a) == snap
    # Save removes it (the list was confirmed before: no question per action)
    win.edit.draft.remove(stored)
    win.edit.refresh()
    finish_save(qtbot, win)
    assert not spy.stored_lines
    assert "Alpha plain note" not in [c[4] for c in comparable(records(a))]
    assert not win.edit.draft.dirty and not spy.problems


def test_changing_a_stored_note_keeps_both_visible_and_a_mixed_save_replaces_then_adds(ed, qtbot):
    win, spy, (a, _b) = ed
    turn_on(win)
    stored = next(m for m in win.notes_tab.rows if m.text == "Alpha plain note")
    win.edit.on_row(stored)
    win.edit.ask_note = lambda editor: {
        "time": stored.time, "end": None, "label": "Artifact", "text": "reworded", "channel": None,
    }  # fmt: skip
    win.edit.edit_selected()
    states = sorted(m.state for m in win.notes_tab.rows if m.state)
    assert states == ["changed", "removed"]  # the new wording and the struck-through original
    win.edit.add(T0 + 40 * SEC, None, "Spike", "new one")
    before = comparable(records(a))
    assert "Alpha plain note" in [c[4] for c in before]  # nothing is written yet
    finish_save(qtbot, win)
    got = [c[4] for c in comparable(records(a))]
    assert "Artifact: reworded" in got and "Alpha plain note" not in got  # replaced
    assert "Spike: new one" in got  # and the new note was added after it
    assert not spy.stored_lines  # no question per action: the list was confirmed before
    assert not win.edit.draft.dirty and not spy.problems


def test_changing_a_stored_note_of_a_hidden_recording_needs_it_shown(make_window, pair, qtbot):
    shown = Shown()
    win = make_window(*pair, reveal=shown, show_details=shown.show)
    wait_marks(qtbot, win)
    spy = Spy(win)
    turn_on(win)
    win.edit.on_row(win.notes_tab.rows[0])
    win.edit.ask_note = lambda editor: pytest.fail("the editor must not open")
    assert win.edit.edit_selected() is None
    assert "Show the details" in win.last_refusal and not spy.problems


# --- import and export ------------------------------------------------------------------------


def test_import_shows_a_preview_then_enters_the_draft_highlighted(ed, tmp_path, qtbot):
    win, _spy, _ = ed
    turn_on(win)
    f = tmp_path / "in.csv"
    f.write_text(
        "start,end,type,text,channel\n"
        f"{T0 + 10 * SEC},{T0 + 12 * SEC},Spike,one,\n"
        "2000-01-01T00:00:20Z,,Note,two,Fp1\n"
        "2000-01-01T00:01:10Z,,Note,between,\n"
        "nonsense,,Note,bad,\n",
        encoding="utf-8",
    )
    seen = {}
    win.edit.ask_import = lambda pv: (seen.setdefault("pv", pv), True)[1]
    with qtbot.waitSignal(win.edit.last_job_signal if False else win.edit.changed, timeout=30_000):
        win.edit.import_file(str(f))
    pv = seen["pv"]
    assert pv.rows == 4 and len(pv.usable) == 2 and [n for n, _ in pv.outside] == [3]
    assert [n for n, _ in pv.invalid] == [4] and pv.first == T0 + 10 * SEC
    assert [i.imported for i in win.edit.draft.items] == [True, True]
    assert [m.state for m in win.notes_tab.rows if m.state] == ["new", "new"]
    dialog = ImportPreview(pv, win.zone)
    qtbot.addWidget(dialog)
    assert "1 row fall outside every recording" in dialog.text.text()
    assert "Row 4:" in dialog.text.text() and "Nothing is written" in dialog.text.text()
    assert not BANNED_RE.search(dialog.text.text())
    win.edit.undo()  # one step takes the whole import back
    assert not win.edit.draft.dirty


def test_import_of_a_missing_or_unusable_file_is_said_plainly(ed, tmp_path, qtbot):
    win, spy, _ = ed
    turn_on(win)
    bad = tmp_path / "x.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    seen = {}
    win.edit.ask_import = lambda pv: (seen.setdefault("pv", pv), True)[1]
    h = win.edit.import_file(str(bad))
    qtbot.waitUntil(lambda: h.is_finished, timeout=30_000)
    qtbot.wait(50)
    assert "start column" in seen["pv"].fatal and not win.edit.draft.dirty
    h = win.edit.import_file(str(tmp_path / "missing.csv"))
    qtbot.waitUntil(lambda: h.is_finished, timeout=30_000)
    qtbot.wait(50)
    assert spy.problems and str(tmp_path) not in spy.problems[-1].text


def test_export_round_trips_and_waits_for_the_details_to_be_shown(
    make_window, pair, tmp_path, qtbot
):
    shown = Shown()
    win = make_window(*pair, reveal=shown, show_details=shown.show)
    wait_marks(qtbot, win)
    spy = Spy(win)
    out = tmp_path / "out.csv"
    win.ask_show = lambda indices: False  # the user keeps them hidden
    assert not win.edit.export_marks("csv", "all", str(out)) and not out.exists()
    assert "hidden" in win.last_refusal
    win.ask_show = lambda indices: (shown.show(0), shown.show(1), True)[2]
    assert win.edit.export_marks("csv", "all", str(out))
    result = ad.read_rows(out.read_text(encoding="utf-8"), "csv")
    assert not result.invalid
    got = sorted((r.time, r.end, r.label, r.text, r.channel) for r in result.rows)
    want = sorted(
        (m.time, m.end, *ad.split_text(m.text), m.channel)
        for m in win.marks
        if m.code in ("Note", "EDFA")
    )
    assert got == want and len(got) == len(win.marks) == 5
    assert "2000-01-01T00:00:05+00:00" in out.read_text(encoding="utf-8")
    assert not spy.problems
    j = tmp_path / "out.json"
    assert win.edit.export_marks("json", "listed", str(j))
    assert len(ad.read_rows(j.read_text(encoding="utf-8"), "json").rows) == len(win.marks)


# --- leaving with a draft ---------------------------------------------------------------------


def test_leaving_edit_mode_or_closing_with_a_draft_asks(ed, qtbot):
    win, spy, (a, _b) = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "keep me")
    # leaving edit mode keeps the draft and asks nothing: the red bar stays until it is written
    win.edit_button.click()
    assert not win.edit.on and win.edit.draft.dirty and spy.asked == []
    assert win.write_bar.isVisible()
    # closing asks (Write to file / Discard changes / Stay)
    spy.answer = "stay"
    win.close()
    assert not win._closed and spy.asked == ["close"] and win.isVisible()
    # closing with "write" writes first (after the list), then the window closes
    spy.answer = "save"
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        win.close()
    qtbot.waitUntil(lambda: win._closed, timeout=10_000)
    assert any(c[4] == "Spike: keep me" for c in comparable(records(a)))
    assert spy.summaries and spy.summaries[0].added == 1  # the list was shown first


# --- failures in plain words ------------------------------------------------------------------


def test_a_torn_notes_file_says_it_must_be_fixed_without_naming_a_missing_feature(
    ed, qtbot, tmp_path
):
    win, spy, (a, _b) = ed
    rdat = next(Path(a).glob("*.rdat"))
    rdat.write_bytes(rdat.read_bytes() + b"\x01\x02\x03")  # an unfinished entry
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    finish_save(qtbot, win)
    assert len(spy.problems) == 1
    msg = spy.problems[0]
    assert "left unfinished" in msg.text and "recording A" in msg.text
    assert "rescue" not in msg.text.lower() and "Explore" not in msg.text
    assert "Nothing was added" in msg.text
    assert "recover_session" in msg.detail  # the technical name only behind the details
    assert str(tmp_path) not in msg.text and "recover_session" not in msg.text
    assert not BANNED_RE.search(msg.title + " " + msg.text)
    assert win.edit.draft.dirty and not win.edit.saving  # nothing lost, can try again


def open_locked(make_window, qtbot, path, pwd):
    from mef3io_studio.model.viewer_source import open_recordings
    from mef3io_studio.viewer.window import ViewerWindow

    data = open_recordings([str(path)], [pwd])
    for ch in data.channels:
        data.rate(ch), data.units(ch), data.ownership(ch)
    win = ViewerWindow(data, reveal=lambda i: True)
    qtbot.addWidget(win)
    win.resize(1200, 700)
    win.show()
    qtbot.waitUntil(lambda: win._ready, timeout=10_000)
    wait_marks(qtbot, win)
    wide(win)
    return win


def all_bytes(path) -> bytes:
    return b"".join(f.read_bytes() for f in sorted(Path(path).rglob("*")) if f.is_file())


@pytest.mark.parametrize("existing", [True, False], ids=["notes file exists", "no notes file yet"])
def test_notes_added_to_a_protected_recording_are_never_stored_as_plain_text(
    make_window, tmp_path, qtbot, existing
):
    p = write(
        tmp_path / "Locked.mefd",
        password1="a",
        password2="b",
        notes=[{"time": T0 + 5 * SEC, "type": "Note", "text": "old secret"}] if existing else (),
    )
    win = open_locked(make_window, qtbot, p, "b")  # holds the full-access one
    spy = Spy(win)
    spy.extra = {0: "a"}  # the basic one, typed in the preview
    turn_on(win)
    win.edit.add(T0 + 10 * SEC, None, "Spike", "findme-session-text")
    win.edit.add(T0 + 11 * SEC, None, "Note", "findme-channel-text", "Cz")
    finish_save(qtbot, win)
    assert not spy.problems
    assert b"findme" not in all_bytes(p)  # the bodies are not plain text
    got = [c[4] for c in comparable(records(p, "b"))]
    assert got == (["old secret"] if existing else []) + [
        "Spike: findme-session-text",
        "findme-channel-text",
    ]
    assert win.marks_loaded and len(win.marks) == len(got)
    win.close()


def test_a_protected_recording_needs_both_passwords_checked_before_writing(
    make_window, tmp_path, qtbot
):
    p = write(tmp_path / "Locked.mefd", password1="a", password2="b")
    win = open_locked(make_window, qtbot, p, "a")  # holds only the basic one
    spy = Spy(win)
    turn_on(win)
    win.edit.add(T0 + 10 * SEC, None, "Spike", "x")
    snap = tree_hash(p)
    for extra, words in (
        ({}, "both its passwords are needed"),
        ({0: "zzz"}, "do not both fit"),  # wrong
        ({0: "a"}, "do not both fit"),  # the same one twice
    ):
        spy.extra = extra
        spy.problems.clear()
        finish_save(qtbot, win)
        assert len(spy.problems) == 1 and words in spy.problems[0].text, spy.problems
        assert "Nothing was added" in spy.problems[0].text and win.edit.draft.dirty
        assert tree_hash(p) == snap  # byte-identical, mtimes too
    spy.extra = {0: "b"}  # the right one
    spy.problems.clear()
    finish_save(qtbot, win)
    assert not spy.problems and not win.edit.draft.dirty
    assert [c[4] for c in comparable(records(p, "b"))] == ["Spike: x"]
    assert b"Spike: x" not in all_bytes(p)
    win.close()


def test_the_preview_asks_for_the_password_the_app_does_not_hold(ed, qtbot):
    win, _spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    dialog = SavePreview(
        lambda c: win.edit.draft.save_plan(win.data.holders_at, c),
        win.recording_label,
        win._rec_letter,
        win.zone,
        None,
        lambda i: "basic password" if i == 0 else None,
    )
    qtbot.addWidget(dialog)
    assert list(dialog.password_edits) == [0]
    assert not dialog.ok.isEnabled()  # until it is typed
    dialog.password_edits[0].setText("a")
    assert dialog.ok.isEnabled() and dialog.passwords() == {0: "a"}
    text = " ".join(w.text() for w in dialog.password_box.findChildren(type(dialog.lines)))
    assert "both its passwords are needed" in text and "basic password" in text
    assert not BANNED_RE.search(text)
    plain = SavePreview(
        lambda c: win.edit.draft.save_plan(win.data.holders_at, c),
        win.recording_label,
        win._rec_letter,
        win.zone,
    )
    qtbot.addWidget(plain)
    assert not plain.password_edits and plain.ok.isEnabled()


def test_a_save_that_fails_half_way_adds_nothing_twice(ed, qtbot, monkeypatch):
    win, spy, (a, _b) = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "whole one")  # session level, written first
    win.edit.add(T0 + 31 * SEC, None, "Note", "channel one", "Cz")  # channel level, fails
    real = mef3io.Writer.write_annotations

    def flaky(self, annotations, channel=None):
        if channel == "Cz":
            raise mef3io.IoError("disk trouble")
        return real(self, annotations, channel)

    monkeypatch.setattr(mef3io.Writer, "write_annotations", flaky)
    finish_save(qtbot, win)
    assert len(spy.problems) == 1
    text = spy.problems[0].text
    assert "Already added" in text and "recording A: 1 note or event" in text
    assert "Nothing was added" not in text
    assert [i.text for i in win.edit.draft.items] == ["channel one"]  # only what failed remains
    assert [c[4] for c in comparable(records(a)) if c[3] == T0 + 30 * SEC] == ["Spike: whole one"]
    monkeypatch.setattr(mef3io.Writer, "write_annotations", real)
    spy.problems.clear()
    finish_save(qtbot, win)  # the retry
    assert not spy.problems and not win.edit.draft.dirty
    stored = [c[4] for c in comparable(records(a))]
    assert stored.count("Spike: whole one") == 1 and stored.count("channel one") == 1


def test_a_moved_or_replaced_recording_is_refused_before_anything_is_written(ed, qtbot, tmp_path):
    win, spy, (a, b) = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    win.edit.add(T0 + 120 * SEC, None, "Spike", "y")  # recording B: fine
    before_b = tree_hash(b)
    moved = Path(a).with_name("Renamed.mefd")
    # On Windows a folder cannot be renamed while a file in it is held open, and the viewer
    # keeps its signal files open between reads: let go of them, as the idle release does.
    win.data.release_files()
    Path(a).rename(moved)
    finish_save(qtbot, win)
    assert "not where it was opened" in spy.problems[-1].title
    assert not Path(a).exists()  # the library did not create a new recording there
    assert tree_hash(b) == before_b and win.edit.draft.dirty
    # a different recording at the same place is refused too
    write(Path(a), start=T0 + 1000 * SEC, seconds=10)
    spy.problems.clear()
    finish_save(qtbot, win)
    assert "has changed" in spy.problems[-1].title and tree_hash(b) == before_b
    assert not records(a)  # nothing was added to the stand-in


def test_a_recording_that_moves_mid_save_is_refused_and_earlier_additions_reported(
    ed, qtbot, monkeypatch
):
    win, spy, (a, b) = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")  # recording A
    win.edit.add(T0 + 120 * SEC, None, "Spike", "y")  # recording B
    real = mef3io.Writer.write_annotations
    moved = Path(b).with_name("MovedAway.mefd")

    def rename_b_after_a(self, annotations, channel=None):
        out = real(self, annotations, channel)
        if not moved.exists():
            Path(b).rename(moved)
        return out

    monkeypatch.setattr(mef3io.Writer, "write_annotations", rename_b_after_a)
    finish_save(qtbot, win)
    text = spy.problems[-1].text
    assert "not where it was opened" in spy.problems[-1].title
    assert "recording A: 1 note or event" in text and "Nothing was added" not in text
    assert not Path(b).exists()  # nothing created at the old path
    assert [i.text for i in win.edit.draft.items] == ["y"]


def test_the_stop_message_says_the_rest_remains_in_the_draft(ed, qtbot, monkeypatch):
    from mef3io_studio.model import annotation_save as asave
    from mef3io_studio.model.cancel import JobCancelled as Cancelled

    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    win.edit.add(T0 + 31 * SEC, None, "Note", "c", "Cz")
    calls = []

    def stop_second(ctx_check=None):
        calls.append(1)
        if len(calls) == 2:
            raise Cancelled()

    real = asave.write_plan
    monkeypatch.setattr(
        asave, "write_plan", lambda d, p, pw, chk, rep, reg: real(d, p, pw, stop_second, rep, reg)
    )
    finish_save(qtbot, win)
    assert "remains in the draft" in spy.problems[-1].text


def test_the_draft_is_locked_while_a_save_runs_and_the_gui_thread_stays_off_mef3io(
    ed, qtbot, monkeypatch
):
    from mef3io_studio.model import annotation_save as asave

    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    gate = threading.Event()
    real = asave.write_plan

    def slow(*a, **k):
        gate.wait(30)
        return real(*a, **k)

    monkeypatch.setattr(asave, "write_plan", slow)
    main = threading.main_thread()
    on_main = []

    def guard(name, real_fn):
        def wrapped(*a, **k):
            if threading.current_thread() is main:
                on_main.append(name)
            return real_fn(*a, **k)

        return wrapped

    for cls in (mef3io.Reader, mef3io.MultiReader):
        for name in ("segments", "info", "read", "records", "block_envelope"):
            if hasattr(cls, name):
                monkeypatch.setattr(cls, name, guard(name, getattr(cls, name)))
    assert win.edit.save()
    assert win.edit.saving
    draft = win.edit.draft
    snapshot = list(draft.items)
    at(win, T0 + 12 * SEC)
    for k in (Qt.Key.Key_A, Qt.Key.Key_2, Qt.Key.Key_3):
        key(win, k)
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    mods = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    key(win, Qt.Key.Key_Z, mods)
    win.edit.ask_note = lambda editor: pytest.fail("the editor must not open")
    assert win.edit.add(T0 + 5 * SEC, None, "Spike") is None
    assert win.edit.open_new(T0 + 5 * SEC) is None and win.edit.edit_selected() is None
    assert win.edit.remove_selected() is False
    win.edit.undo(), win.edit.redo(), win.edit.discard()
    assert win.edit.import_file("nothing.csv") is None
    assert not win.edit.undo_button.isEnabled() and not win.notes_tab.add_button.isEnabled()
    assert win.edit.set_on(False) is True  # refused: still on
    assert list(draft.items) == snapshot and "Wait until" in win.status_message.text()
    # what the window redraws meanwhile never asks the library on its own thread
    win.refresh_privacy()
    win._update_marks_view()
    win._display_marks()
    win._refresh()
    gate.set()
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=60_000)
    assert not spy.problems and on_main == []


def test_reopening_has_every_cache_filled_before_the_window_can_see_it(ed):
    win, _spy, _ = ed
    data = win.data
    data.reopen()
    assert set(data._ownership) == set(data.channels)
    assert set(data._info) == set(data.channels)


def test_control_characters_are_refused_in_the_editor_and_the_draft(ed, qtbot):
    win, _spy, _ = ed
    turn_on(win)
    editor = win.edit._make_editor("Add a note or event")
    qtbot.addWidget(editor)
    editor.set_values(T0 + 10 * SEC, None, "Note", "fine\ttab\nnewline", None)
    editor._accept()
    assert editor.result() == 1
    for bad in ("nul\x00here", "bell\x07", "esc\x1b[0m", "del\x7f"):
        editor.set_values(T0 + 10 * SEC, None, "Note", bad, None)
        editor.setResult(0)
        editor._accept()
        assert editor.result() == 0 and "control character" in editor.error.text()
        assert not BANNED_RE.search(editor.error.text())
    editor.set_values(T0 + 10 * SEC, None, "Bad\x00label", "x", None)
    editor._accept()
    assert "control character" in editor.error.text()
    assert win.edit.add(T0 + 10 * SEC, None, "Spike", "x\x00y") is None
    assert "control character" in win.last_refusal and not win.edit.draft.dirty


def test_a_recording_packed_in_a_single_file_has_no_edit_mode(make_window, pair, tmp_path, qtbot):
    tar = Path(mef3io.archive_session(str(pair[0])))
    assert tar.name.endswith(".mefd.tar")
    win = make_window(tar)
    spy = Spy(win)
    win.edit_button.click()
    assert not win.edit.on and not win.edit_button.isChecked() and not win.edit.banner.isVisible()
    assert ".mefd.tar" in win.last_refusal and str(tmp_path) not in win.last_refusal
    assert not BANNED_RE.search(win.last_refusal) and not spy.problems
    assert "edit" not in win.features


def test_a_choice_for_channel_notes_says_so():
    both = lambda channel, t: [0, 1]  # noqa: E731 - a channel two recordings hold
    d = ad.Draft()
    d.add(T0 + SEC, None, "Spike", "", "Fp1")
    dialog = SavePreview(
        lambda c: d.save_plan(both, c),
        lambda i: f"Recording {'AB'[i]}",
        lambda i: "AB"[i],
        __import__("datetime").timezone.utc,
    )
    text = dialog.choice_layout.itemAt(0).widget().text()
    assert "for channel Fp1" in text and "whole recording" not in text


# --- the rules --------------------------------------------------------------------------------


def test_the_gui_thread_never_calls_mef3io_in_edit_mode(ed, qtbot, monkeypatch, tmp_path):
    win, _spy, _ = ed
    main = threading.main_thread()
    calls = []

    def guard(name, real):
        def wrapped(*a, **k):
            calls.append((name, threading.current_thread() is main))
            return real(*a, **k)

        return wrapped

    for cls in (mef3io.Reader, mef3io.MultiReader, mef3io.Writer):
        for name in ("read", "records", "info", "segments", "write_annotations", "close"):
            if hasattr(cls, name):
                monkeypatch.setattr(cls, name, guard(name, getattr(cls, name)))
    for name in ("Reader", "MultiReader", "Writer"):
        monkeypatch.setattr(mef3io, name, guard(name, getattr(mef3io, name)))
    turn_on(win)
    at(win, T0 + 10 * SEC)
    key(win, Qt.Key.Key_3)
    win.edit.add(T0 + 130 * SEC, None, "Spike", "b")
    f = tmp_path / "i.csv"
    f.write_text(f"start,type,text\n{T0 + 5 * SEC},Note,hi\n", encoding="utf-8")
    win.edit.ask_import = lambda pv: True
    h = win.edit.import_file(str(f))
    qtbot.waitUntil(lambda: h.is_finished, timeout=30_000)
    qtbot.wait(50)
    win.edit.export_marks("csv", "all", str(tmp_path / "o.csv"))
    finish_save(qtbot, win)
    assert calls, "the save should have used the library"
    assert [n for n, on_main in calls if on_main] == []


def test_edit_mode_in_the_dark_theme_and_the_banner_colours_follow(ed):
    win, _spy, _ = ed
    turn_on(win)
    win.set_theme(DARK)
    assert DARK.accent_soft.lower() in win.edit.banner.styleSheet().lower()


def test_no_note_text_reaches_the_log(ed, qtbot, caplog):
    import logging

    win, _spy, _ = ed
    with caplog.at_level(logging.DEBUG):
        turn_on(win)
        win.edit.add(T0 + 30 * SEC, None, "Spike", "findme-secret-words")
        finish_save(qtbot, win)
    assert "findme-secret-words" not in caplog.text
    assert "Alpha" not in caplog.text


def test_quick_types_preference_changes_the_keys(make_window, pair, qtbot, tmp_path):
    from mef3io_studio.common.settings import Settings

    s = Settings(str(tmp_path / "prefs.ini"))
    s.set("viewer/quick_types", "Note\nEye blink\nTremor")
    win = make_window(*pair, settings=s)
    wait_marks(qtbot, win)
    wide(win)
    turn_on(win)
    at(win, T0 + 10 * SEC)
    key(win, Qt.Key.Key_3)
    key(win, Qt.Key.Key_4)  # no fourth type: nothing
    assert [i.label for i in win.edit.draft.items] == ["Tremor"]


# --- opening never inspects -------------------------------------------------------------------


def test_opening_and_paging_never_inspect_but_a_save_does_on_a_task_thread(
    ed, qtbot, monkeypatch, tmp_path
):
    win, spy, (a, b) = ed
    calls = []
    real = mef3io.inspect

    def counting(*args, **kw):
        calls.append(threading.current_thread() is threading.main_thread())
        return real(*args, **kw)

    monkeypatch.setattr(mef3io, "inspect", counting)
    # a fresh data object (as the open job makes it) and a reopen: no inspect
    from mef3io_studio.model.viewer_source import open_recordings

    fresh = open_recordings([a, b])
    fresh.reopen()
    fresh.close()
    win.go_to(T0 + 30 * SEC)
    assert calls == [] and not win.data.facts_loaded
    # the first save reads them, off the GUI thread
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    finish_save(qtbot, win)
    assert not spy.problems and calls and not any(calls)
    assert win.data.facts_loaded  # nothing was reopened, so what was read stays


# --- the save never leaves the draft locked ----------------------------------------------------


def _unlocked(win):
    assert not win.edit.saving
    assert win.write_bar.write_button.isEnabled() and win.edit.draft.dirty
    assert win.edit.set_on(True) is True


def test_a_stopped_facts_task_unlocks_the_draft(ed, qtbot, monkeypatch):
    from mef3io_studio.model.cancel import JobCancelled

    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")

    def stopped():
        raise JobCancelled()

    monkeypatch.setattr(win.data, "read_facts", stopped)
    assert win.edit.save() is True
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=10_000)
    _unlocked(win)
    spy.answer = "stay"
    assert win.edit.confirm_close() is False  # asks; it is not "busy"
    assert spy.asked == ["close"]
    assert not spy.problems


def test_a_cancelled_preview_after_the_lazy_read_changes_nothing(ed, qtbot):
    """Same outcome as a cancelled preview always had: not saved, the mode and
    the window stay as they were, for a plain save, then_off and then_close."""
    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    win.edit.ask_save = lambda plan_for: None  # the user cancels the preview
    for kw in ({}, {"then_off": True}, {"then_close": True}):
        win.data._facts_loaded = False  # the lazy read runs again
        win.edit.save(**kw)
        qtbot.waitUntil(lambda: not win.edit.saving, timeout=10_000)
        _unlocked(win)
        assert win.edit.on and win.isVisible() and not win._closed
        assert not spy.problems


def test_a_preview_that_raises_unlocks_the_draft(ed, qtbot):
    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")

    def broken(plan_for):
        raise RuntimeError("boom")

    win.edit.ask_save = broken
    win.edit.save()
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=10_000)
    _unlocked(win)
    assert len(spy.problems) == 1


def test_a_window_that_moved_on_unlocks_the_draft(ed, qtbot):
    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    win.edit.saving = True  # as save() leaves it while the facts are read
    assert win.edit._ask_then_save(object(), lambda c: None, False, False) is False
    _unlocked(win)


def _wait_saved(qtbot, win):
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=10_000)
    assert win.write_bar.write_button.isEnabled() and win.edit.draft.dirty


def test_a_save_task_that_is_stopped_frees_the_draft_and_the_window(ed, qtbot, monkeypatch):
    """A stop that reaches the task from outside the write loop (queued task
    removed, a stop inside the library before the first write) used to leave
    the window locked: Save and Discard grey, closing refused."""
    from mef3io_studio.model.cancel import JobCancelled

    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    win.data.read_facts()
    win.edit.ask_save = lambda plan_for: ({}, {})

    def stopped(*a, **k):
        raise JobCancelled()

    monkeypatch.setattr(edit_mode.asave, "write_plan", stopped)
    win.edit.save()
    _wait_saved(qtbot, win)
    assert win.write_bar.discard_button.isEnabled()
    assert win.edit.set_on(True) is True


def test_a_stop_after_the_first_write_began_reads_the_recordings_again(ed, qtbot, monkeypatch):
    from mef3io_studio.model.cancel import JobCancelled

    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    win.data.read_facts()
    win.edit.ask_save = lambda plan_for: ({}, {})

    def stopped_in_the_middle(data, plan, pw, check, *rest):
        check()  # the loop asks before each write
        raise JobCancelled()

    monkeypatch.setattr(edit_mode.asave, "write_plan", stopped_in_the_middle)
    win.edit.save()
    _wait_saved(qtbot, win)
    qtbot.waitUntil(lambda: bool(spy.problems), timeout=20_000)
    assert len(spy.problems) == 1
    # the recordings were read again: nothing was stored, so the item is still the draft's
    assert "Nothing had been added" in spy.problems[0].text
    assert win.edit.draft.dirty and win.write_bar.write_button.isEnabled()


def test_a_failure_starting_the_save_frees_the_draft(ed, qtbot, monkeypatch):
    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    win.data.read_facts()
    win.edit.ask_save = lambda plan_for: ({}, {})
    jobs = win.edit.jobs
    real = jobs.submit
    calls = []

    def broken(title, *a, **k):
        if title == "Adding notes and events":
            calls.append(1)
            raise RuntimeError("boom")
        return real(title, *a, **k)

    monkeypatch.setattr(jobs, "submit", broken)
    win.edit.save()
    assert calls
    _wait_saved(qtbot, win)
    assert len(spy.problems) == 1 and "Nothing was written" in spy.problems[0].text


def test_a_save_of_notes_does_not_read_the_strip_again_nor_interrupt_a_read(ed, qtbot):
    win, spy, _ = ed
    qtbot.waitUntil(lambda: bool(win._stretches), timeout=20_000)
    stretches = list(win._stretches)
    data, ctl = win.data, win.controller
    # a strip read that is RUNNING while the save happens: it waits at the gate inside the read
    gate, started = threading.Event(), threading.Event()
    real_env, real_many = data.envelope, data.envelope_many

    def gated(real):
        def call(*a, **k):
            started.set()
            assert gate.wait(30)
            return real(*a, **k)

        return call

    data.envelope, data.envelope_many = gated(real_env), gated(real_many)
    win._stretches = []  # what the read delivers is seen arriving
    win.request_strip_envelope()
    qtbot.waitUntil(started.is_set, timeout=20_000)
    epoch = ctl._strip_epoch
    order, asked = [], []
    real_drain, real_reopen, real_ask = ctl.drain_strip, data.reopen, ctl.request_strip_envelope
    ctl.drain_strip = lambda *a, **k: (order.append("drain"), real_drain(*a, **k))[1]
    data.reopen = lambda: (order.append("reopen"), real_reopen())[1]
    ctl.request_strip_envelope = lambda *a, **k: (asked.append(a), real_ask(*a, **k))[1]
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "once")
    finish_save(qtbot, win)
    qtbot.wait(300)
    assert not gate.is_set() and win._stretches == []  # the read is still in flight
    assert order == []  # nothing is reopened, so no read has to end first
    assert not asked  # only notes were added: the signal is as it was
    assert ctl._strip_epoch == epoch  # and the running read was not given up
    gate.set()  # it now ends, and its result is shown as it would have been
    qtbot.waitUntil(lambda: bool(win._stretches), timeout=20_000)
    assert win._stretches == stretches


def _count(path, text):
    return sum(1 for r in records(path) if r.get("text") == text)


def test_a_save_that_ends_after_writing_does_not_store_the_notes_twice(ed, qtbot, monkeypatch):
    """The write happened but the task's report was lost (here: it raises after
    the real write). The recordings are read again, what is stored leaves the
    draft, and a second save cannot add it again."""
    win, spy, (a, _b) = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "stored once")
    win.edit.add(T0 + 31 * SEC, None, "Spike", "stored once")  # the same twice: both are ours
    win.data.read_facts()
    real = edit_mode.asave.write_plan

    def write_then_lose_the_report(*args, **kw):
        real(*args, **kw)
        raise RuntimeError("the report was lost")

    monkeypatch.setattr(edit_mode.asave, "write_plan", write_then_lose_the_report)
    win.edit.save()
    qtbot.waitUntil(lambda: bool(spy.problems), timeout=30_000)
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
    assert _count(a, "Spike: stored once") == 2
    assert "2 notes and events had already been added" in spy.problems[0].text
    assert not win.edit.draft.dirty  # nothing left to save: a second save cannot duplicate
    assert win.edit.save() is False
    monkeypatch.setattr(edit_mode.asave, "write_plan", real)
    assert win.edit.save() is False
    assert _count(a, "Spike: stored once") == 2
    assert len(win.marks) == len(records(a)) + len(records(_b))  # and the list shows them


def test_a_save_whose_recordings_cannot_be_read_again_stays_off(ed, qtbot, monkeypatch):
    win, spy, (a, _b) = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "maybe stored")
    win.data.read_facts()
    real = edit_mode.asave.write_plan

    def write_then_lose_the_report(*args, **kw):
        real(*args, **kw)
        raise RuntimeError("the report was lost")

    monkeypatch.setattr(edit_mode.asave, "write_plan", write_then_lose_the_report)

    def cannot():
        raise OSError("gone")

    monkeypatch.setattr(win.data, "reopen", cannot)
    win.edit.save()
    qtbot.waitUntil(lambda: bool(spy.problems), timeout=30_000)
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
    assert "open the recordings again" in spy.problems[0].text
    assert BANNED_RE.search(spy.problems[0].text) is None
    assert win.edit.draft.dirty and not win.write_bar.write_button.isEnabled()  # saving is off
    assert win.edit.save() is False
    assert "Writing is off" in win.write_bar.text.text()
    assert win.write_bar.discard_button.isEnabled()  # the window can still be left


def test_a_stop_before_any_write_is_not_taken_for_a_stop_after_one(ed, qtbot, monkeypatch):
    """The loop asks before each write; a stop answered THERE is before the
    write, so it is not "may already have been added"."""
    import time

    win, spy, (a, _b) = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "never written")
    win.data.read_facts()

    def late(data, plan, pw, check, *rest):
        time.sleep(0.4)  # the stop arrives while it waits ...
        check()  # ... and is answered here, before any write
        raise AssertionError("not reached")

    monkeypatch.setattr(edit_mode.asave, "write_plan", late)
    before = len(records(a))
    win.edit.save()
    win.edit.last_job.cancel()
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
    assert len(records(a)) == before
    assert win.edit.draft.dirty and win.write_bar.write_button.isEnabled() and not win.edit.stale
    assert win.status_message.text() == "The write was stopped. Nothing was added."
    assert not spy.problems


# --- the same note twice ------------------------------------------------------------------------


def test_a_note_equal_to_a_stored_or_drafted_one_is_refused_at_once(ed):
    win, spy, _ = ed
    turn_on(win)
    # "Alpha plain note" is stored at +5 s (a point, the whole recording)
    assert win.edit.add(T0 + 5 * SEC, None, "Note", "Alpha plain note") is None
    assert win.status_message.text() == ad.DUPLICATE_STORED and not win.edit.draft.dirty
    assert win.edit.add(T0 + 5 * SEC, None, "Note", "another text") is not None
    assert win.edit.add(T0 + 5 * SEC, None, "Note", "another text") is None
    assert win.status_message.text() == ad.DUPLICATE_DRAFT
    assert len(win.edit.draft.items) == 1


def test_the_editor_stays_open_with_the_sentence_for_a_copy(ed):
    win, spy, _ = ed
    turn_on(win)
    editor = win.edit._make_editor("Add a note or event")
    editor.set_values(T0 + 5 * SEC, None, "Note", "Alpha plain note", None)
    editor.ok.click()
    assert editor.error.text() == ad.DUPLICATE_STORED and editor.result() != 1


def test_a_save_never_stores_a_note_twice(ed, qtbot):
    win, spy, _ = ed
    turn_on(win)
    before = len(records(win.data.location_of(0)))
    draft = win.edit.draft
    saved = draft.stored_keys
    draft.stored_keys = None  # a copy that got into the draft before it was stored
    draft.add(T0 + 5 * SEC, None, "Note", "Alpha plain note")
    draft.stored_keys = saved
    win.edit.refresh()
    assert win.edit.save() is False  # only copies: nothing to write
    assert not draft.dirty and "already stored" in win.status_message.text()
    assert len(records(win.data.location_of(0))) == before
    # a mixed draft writes only the new one and the copy leaves the draft too
    draft.stored_keys = None
    draft.add(T0 + 5 * SEC, None, "Note", "Alpha plain note")
    draft.stored_keys = saved
    win.edit.add(T0 + 7 * SEC, None, "Note", "really new")
    finish_save(qtbot, win)
    assert len(records(win.data.location_of(0))) == before + 1 and not draft.dirty


# --- adding fast: the key N and the inline field -------------------------------------------------


def type_in(win, text):
    QTest.keyClicks(win.edit.inline.field, text)


def press(win, k, mods=Qt.KeyboardModifier.NoModifier):
    QTest.keyClick(win.edit.inline.field, k, mods)


def test_n_adds_a_note_at_the_pointer_without_first_choosing_a_mode(ed, qtbot):
    win, spy, _ = ed
    assert not win.edit.on
    at(win, T0 + 12 * SEC)
    key(win, Qt.Key.Key_N)
    inline = win.edit.inline
    assert win.edit.on and inline.isVisible() and inline.field.hasFocus()
    # right there: its left edge is at the note's time on the page
    assert abs(inline.x() - win.canvas.x_of(T0 + 12 * SEC)) <= 4
    type_in(win, "eyes closed")
    press(win, Qt.Key.Key_Return)
    assert not inline.isVisible()
    (item,) = win.edit.draft.items
    assert abs(item.time - (T0 + 12 * SEC)) < 300_000 and item.end is None
    assert (item.label, item.text) == ("Note", "eyes closed")
    # a draft: marked as not stored in the list and on the traces, and nothing is written yet
    assert [m.state for m in win.notes_tab.rows if m.state == "new"] == ["new"]
    assert "1 added" in win.write_bar.text.text()
    assert "not in the recording yet" in win.status_message.text()
    assert win.canvas.hasFocus()


def test_esc_drops_the_note_and_an_empty_enter_keeps_a_bare_marker(ed):
    win, spy, _ = ed
    at(win, T0 + 12 * SEC)
    key(win, Qt.Key.Key_N)
    type_in(win, "never kept")
    press(win, Qt.Key.Key_Escape)
    assert not win.edit.inline.isVisible() and not win.edit.draft.dirty
    key(win, Qt.Key.Key_N)
    press(win, Qt.Key.Key_Return)
    assert [(i.label, i.text) for i in win.edit.draft.items] == [("Note", "")]


def test_after_shift_drag_n_adds_a_note_with_a_duration(ed):
    win, spy, _ = ed
    win.canvas.selection = (T0 + 20 * SEC, T0 + 25 * SEC, 0)
    key(win, Qt.Key.Key_N)
    assert "lasting 5 s" in win.edit.inline.head.text()
    type_in(win, "run of spikes")
    press(win, Qt.Key.Key_Return)
    (item,) = win.edit.draft.items
    assert (item.time, item.end, item.is_span) == (T0 + 20 * SEC, T0 + 25 * SEC, True)
    assert win.canvas.selection is None  # used up


def test_a_typed_type_is_kept_as_the_type(ed):
    win, spy, _ = ed
    at(win, T0 + 12 * SEC)
    key(win, Qt.Key.Key_N)
    type_in(win, "Spike: sharp wave")
    press(win, Qt.Key.Key_Return)
    (item,) = win.edit.draft.items
    assert (item.label, item.text) == ("Spike", "sharp wave")


def test_the_last_texts_and_the_types_in_the_recording_are_one_key_away(ed):
    win, spy, _ = ed
    at(win, T0 + 12 * SEC)
    key(win, Qt.Key.Key_N)
    assert [h.kind for h in win.edit.inline.hints][:1] == ["type"]  # no text typed yet
    assert "Spike" in [h.value for h in win.edit.inline.hints]  # the recording holds one
    type_in(win, "first words")
    press(win, Qt.Key.Key_Return)
    at(win, T0 + 14 * SEC)
    key(win, Qt.Key.Key_N)
    inline = win.edit.inline
    assert inline.hints[0].kind == "text" and inline.hints[0].value == "first words"
    press(win, Qt.Key.Key_1, Qt.KeyboardModifier.AltModifier)
    assert inline.field.text() == "first words"
    spike = next(i for i, h in enumerate(inline.hints) if h.value == "Spike")
    inline.field.setText("blunt")
    press(win, Qt.Key.Key_1 + spike, Qt.KeyboardModifier.AltModifier)
    assert inline.field.text() == "Spike: blunt"  # the type goes in front, the text stays
    press(win, Qt.Key.Key_Return)
    assert win.edit.recent_texts[0] == "Spike: blunt"
    assert len(win.edit.draft.items) == 2


def test_typed_texts_are_never_stored_in_the_preferences(make_window, pair, qtbot, tmp_path):
    from mef3io_studio.common.settings import Settings

    path = tmp_path / "prefs.ini"
    s = Settings(str(path))
    win = make_window(*pair, settings=s)
    wait_marks(qtbot, win)
    wide(win)
    at(win, T0 + 12 * SEC)
    key(win, Qt.Key.Key_N)
    type_in(win, "patient-detail-xyz")
    press(win, Qt.Key.Key_Return)
    win.save_preferences()
    assert "patient-detail-xyz" not in (path.read_text() if path.exists() else "")
    assert win.edit.recent_texts == ["patient-detail-xyz"]


def test_n_outside_every_recording_says_why_and_a_copy_is_refused(ed):
    win, spy, _ = ed
    at(win, T0 + 75 * SEC)  # the gap between A and B
    key(win, Qt.Key.Key_N)
    assert not win.edit.inline.isVisible() and not win.edit.draft.dirty
    assert "No recording holds data" in win.status_message.text()
    # a copy of a stored note ("Alpha plain note" at 5 s) is refused when it is kept
    at(win, T0 + 5 * SEC)
    win.edit.quick_note()
    win.edit._inline_for = ("new", T0 + 5 * SEC, None)  # exactly the stored time
    type_in(win, "Alpha plain note")
    press(win, Qt.Key.Key_Return)
    assert not win.edit.draft.dirty and win.status_message.text() == ad.DUPLICATE_STORED


def test_a_click_elsewhere_keeps_typed_words_and_drops_an_empty_field(ed, qtbot):
    win, spy, _ = ed
    at(win, T0 + 12 * SEC)
    key(win, Qt.Key.Key_N)
    type_in(win, "kept by a click")
    win.canvas.setFocus(Qt.FocusReason.MouseFocusReason)
    assert not win.edit.inline.isVisible()
    assert [i.text for i in win.edit.draft.items] == ["kept by a click"]
    at(win, T0 + 14 * SEC)
    key(win, Qt.Key.Key_N)
    win.canvas.setFocus(Qt.FocusReason.MouseFocusReason)
    assert len(win.edit.draft.items) == 1  # an empty field is dropped


def test_n_on_a_packed_recording_is_refused_in_plain_words(make_window, pair, tmp_path, qtbot):
    win = make_window(*pair)
    wait_marks(qtbot, win)
    win.data.is_archive = lambda i: True
    key(win, Qt.Key.Key_N)
    assert not win.edit.on and not win.edit.inline.isVisible()
    assert "packed in a single file" in win.status_message.text()


def test_n_is_in_the_key_table_and_the_help(ed):
    from mef3io_studio.viewer import keymap

    entry = next(e for e in keymap.KEYMAP if e.action == "new_note")
    assert entry.keys == ("N",) and entry.feature == ""
    clashes = [e.action for e in keymap.KEYMAP if "N" in e.keys]
    assert clashes == ["new_note"]
    assert not BANNED_RE.search(entry.text)


# --- saving fast: Ctrl+S and one line -------------------------------------------------------------


class Line:
    """Answers the confirmation of "Write to file" (the counts) and records what it said."""

    def __init__(self, win, answer=True):
        self.said: list[str] = []
        self.answer = answer
        del win.edit.ask_save  # the real one (the Spy replaced it)
        win.edit.ask_write = self.ask

        def no_preview(*a, **k):
            raise AssertionError("the full preview must not open for a save that only adds")

        self._real = edit_mode.SavePreview
        edit_mode.SavePreview = no_preview

    def ask(self, summary):
        self.said.append(summary.counts_text())
        return self.answer

    def restore(self):
        edit_mode.SavePreview = self._real


@pytest.fixture
def line(ed):
    win, spy, pr = ed
    made = Line(win)
    yield win, spy, pr, made
    made.restore()


def test_ctrl_s_saves_a_draft_that_only_adds_with_one_line(line, qtbot):
    win, spy, (a, b), asked = line
    before = len(records(a))
    at(win, T0 + 12 * SEC)
    key(win, Qt.Key.Key_N)
    type_in(win, "Spike: one")
    press(win, Qt.Key.Key_Return)
    win.edit.add(T0 + 14 * SEC, None, "Note", "two")
    assert win.status_draft.isVisible() and win.status_draft.text() == "Not written yet: 2 added"
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        key(win, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: not win.edit.saving)
    (said,) = asked.said
    assert said == "2 added"
    assert not spy.problems and not win.edit.draft.dirty
    assert len(records(a)) == before + 2
    assert not win.status_draft.isVisible()  # nothing is waiting any more


def test_stopping_the_one_line_stores_nothing_and_keeps_the_draft(line, qtbot):
    win, spy, (a, b), asked = line
    asked.answer = False
    win.edit.set_on(True)
    win.edit.add(T0 + 14 * SEC, None, "Note", "two")
    snap = tree_hash(a)
    assert win.edit.write_to_file() is False  # "Keep editing": nothing starts
    assert len(asked.said) == 1 and tree_hash(a) == snap and win.edit.draft.dirty
    assert not win.edit.saving


def test_ctrl_s_in_the_text_field_keeps_the_note_and_saves_it(line, qtbot):
    win, spy, (a, b), asked = line
    before = len(records(a))
    at(win, T0 + 12 * SEC)
    key(win, Qt.Key.Key_N)
    type_in(win, "straight to the recording")
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        press(win, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: not win.edit.saving)
    assert len(asked.said) == 1 and len(records(a)) == before + 1


def test_ctrl_s_without_a_draft_says_so(ed):
    win, spy, _ = ed
    turn_on(win)
    key(win, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert win.status_message.text() == "There are no changes to write."


def test_the_line_names_the_recordings_by_letter_while_hidden(make_window, pair, qtbot):
    win = make_window(*pair)  # nothing shown: names are hidden
    wait_marks(qtbot, win)
    wide(win)
    spy = Spy(win)
    made = Line(win)
    try:
        win.edit.set_on(True)
        win.edit.add(T0 + 14 * SEC, None, "Note", "two")
        win.edit.add(T0 + 120 * SEC, None, "Note", "three")
        from mef3io_studio.viewer.write_dialog import WriteDialog

        dialog = WriteDialog(win.edit.summary(), win.edit._where, win._shown, win.zone, win)
        qtbot.addWidget(dialog)
        shown = " ".join(" ".join(row) for row in dialog.rows_text()) + dialog.per_recording.text()
        assert "Recording A" in shown and "Recording B" in shown  # by letter
        assert "Alpha" not in shown and "Beta" not in shown  # no name
        assert "two" not in shown and "three" not in shown  # no hidden text
        win.edit.write_to_file()
        qtbot.waitUntil(lambda: not win.edit.saving)
        assert made.said[0] == "2 added"
        assert not spy.problems
    finally:
        made.restore()


def test_anything_but_additions_keeps_the_full_preview(line, qtbot, monkeypatch):
    win, spy, (a, b), asked = line
    win.edit.set_on(True)
    stored = next(m for m in win.notes_tab.rows if m.text == "Alpha plain note")
    win.edit.draft.remove(stored)  # a change to a stored note
    win.edit.add(T0 + 14 * SEC, None, "Note", "two")
    plan = win.edit.draft.save_plan(win.data.holders_at, {})
    assert win.edit.quick_save_text(plan) is None  # needs the copy sentence
    plan = ad.SavePlan()
    assert win.edit.quick_save_text(plan) is None  # nothing to add
    win.edit.draft.clear()
    win.edit.add(T0 + 14 * SEC, None, "Note", "two")
    plan = win.edit.draft.save_plan(win.data.holders_at, {})
    assert win.edit.quick_save_text(plan)
    monkeypatch.setattr(win.edit, "other_password_label", lambda i: "basic password")
    assert win.edit.quick_save_text(plan) is None  # a password has to be typed: the preview
    monkeypatch.undo()
    # a note falling where two recordings hold data needs a choice: the preview
    ambiguous = ad.make_plan(win.edit.draft.additions(), 0, lambda ch, t: [0, 1])
    assert ambiguous.ambiguous and win.edit.quick_save_text(ambiguous) is None


def test_the_refusals_of_a_save_still_stand_after_the_one_line(line, qtbot):
    win, spy, (a, b), asked = line
    win.edit.set_on(True)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    moved = Path(a).with_name("Renamed.mefd")
    win.data.release_files()  # (Windows: no rename while the viewer holds a file open)
    Path(a).rename(moved)
    assert win.edit.write_to_file()
    qtbot.waitUntil(lambda: not win.edit.saving)
    assert len(asked.said) == 1
    assert "not where it was opened" in spy.problems[-1].title
    assert not Path(a).exists() and win.edit.draft.dirty


def test_the_one_line_has_plain_words_and_the_indicator_follows_the_draft(ed):
    win, spy, _ = ed
    turn_on(win)
    assert not win.status_draft.isVisible()
    win.edit.add(T0 + 14 * SEC, None, "Note", "two")
    assert win.status_draft.text() == "Not written yet: 1 added"
    win.edit.undo()
    assert not win.status_draft.isVisible()
    win.edit.add(T0 + 14 * SEC, None, "Note", "two")
    win.edit.discard()
    assert not win.status_draft.isVisible()
    plan = win.edit.draft.save_plan(win.data.holders_at, {})
    assert not BANNED_RE.search(win.edit.quick_save_text(plan) or "x")


def test_closing_with_a_draft_still_asks(ed):
    win, spy, _ = ed
    at(win, T0 + 12 * SEC)
    key(win, Qt.Key.Key_N)
    type_in(win, "unsaved")
    press(win, Qt.Key.Key_Return)
    spy.answer = "stay"
    assert win.edit.confirm_close() is False and spy.asked == ["close"]
    spy.answer = "discard"
    assert win.edit.confirm_close() is True


# --- saving without starting over -------------------------------------------------------------


def watch_the_page(win, monkeypatch, qtbot):
    """What a save may not do to the page: reopen, read tiles again, blank the canvas.
    (The banner of edit mode changes the canvas' height once: that settles first.)"""
    from test_viewer_ui import settle

    settle(qtbot, win)
    from mef3io_studio.model import viewer_source as vs
    from mef3io_studio.model.viewer_source import ViewerData

    seen = {"reopen": 0, "open": 0, "tiles": 0, "loading": []}
    real_reopen, real_tile, real_open = ViewerData.reopen, ViewerData.read_tile, vs.open_recordings

    def reopen(self):
        seen["reopen"] += 1
        return real_reopen(self)

    def tile(self, *a, **k):
        seen["tiles"] += 1
        return real_tile(self, *a, **k)

    def opened(*a, **k):
        seen["open"] += 1
        return real_open(*a, **k)

    monkeypatch.setattr(ViewerData, "reopen", reopen)
    monkeypatch.setattr(ViewerData, "read_tile", tile)
    monkeypatch.setattr(vs, "open_recordings", opened)
    real_loading = win.canvas.set_loading
    monkeypatch.setattr(
        win.canvas, "set_loading", lambda v: (seen["loading"].append(v), real_loading(v))[1]
    )
    return seen


def test_a_save_of_notes_keeps_the_page_the_tile_cache_and_the_readers(ed, qtbot, monkeypatch):
    win, spy, (a, b) = ed
    turn_on(win)
    data = win.data
    readers, combined = list(data._readers), data._combined
    info, key0 = data._info, (data.data_id, data.epoch)
    tiles_cached = len(win.controller.cache)
    seen = watch_the_page(win, monkeypatch, qtbot)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "kept page")
    win.edit.add(T0 + 120 * SEC, None, "Note", "in B")
    finish_save(qtbot, win)
    assert not spy.problems and not win.edit.draft.dirty
    # The page is never shown as loading. (A block that arrives late may redraw the rows
    # and say "not loading" again: that blanks nothing.)
    assert not any(seen.pop("loading"))
    assert seen == {"reopen": 0, "open": 0, "tiles": 0}
    assert (data.data_id, data.epoch) == key0 and win.data is data
    assert data._readers == readers and data._combined is combined and data._info is info
    assert len(win.controller.cache) >= tiles_cached  # nothing was thrown away
    # the new notes are there, stored, no longer drafts
    texts = [m.text for m in win.marks]
    assert "Spike: kept page" in texts and "in B" in texts
    assert [m for m in win.notes_tab.rows if m.state] == []
    assert win.notes_button.text() == f"Notes and events ({len(win.marks)})"
    assert len(records(a)) + len(records(b)) == len(win.marks)


def test_a_second_save_after_a_notes_only_save_checks_the_recording_as_before(ed, qtbot):
    win, spy, (a, b) = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "first")
    finish_save(qtbot, win)
    win.edit.add(T0 + 31 * SEC, None, "Spike", "second")
    finish_save(qtbot, win)
    stored = [c[4] for c in comparable(records(a))]
    assert not spy.problems and "Spike: first" in stored and "Spike: second" in stored
    # a different recording is put where it was: the next save is refused, as it was
    # after a reopen (the fingerprint of the signal is the one taken at opening)
    write(Path(a), start=T0 + 1000 * SEC, seconds=10)
    win.edit.add(T0 + 32 * SEC, None, "Spike", "third")
    before = tree_hash(a)
    finish_save(qtbot, win)
    assert "has changed" in spy.problems[-1].title and win.edit.draft.dirty
    assert tree_hash(a) == before


def test_a_signal_that_changed_during_the_save_makes_the_window_reopen(ed, qtbot, monkeypatch):
    import os

    win, spy, (a, b) = ed
    turn_on(win)

    class Touching(mef3io.Writer):
        """A writer after which a signal file of the recording differs (another program)."""

        def __exit__(self, *exc):
            out = super().__exit__(*exc)
            tdat = next(Path(a).rglob("*.tdat"))
            st = tdat.stat()
            os.utime(tdat, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
            return out

    monkeypatch.setattr(mef3io, "Writer", Touching)
    seen = watch_the_page(win, monkeypatch, qtbot)
    epoch = win.data.epoch
    win.edit.add(T0 + 30 * SEC, None, "Spike", "stored")
    finish_save(qtbot, win)
    assert seen["reopen"] == 1 and win.data.epoch == epoch + 1  # shown as it is now
    assert not spy.problems and not win.edit.draft.dirty
    assert "Spike: stored" in [m.text for m in win.marks]


def test_refresh_notes_refuses_what_it_cannot_prove_and_changes_nothing(ed):
    win, _spy, (a, b) = ed
    data = win.data
    before = data.annotations()
    # it claims more notes than were written: the read-back does not match
    assert data.refresh_notes({0: 3}) is False
    assert data.annotations() == before and data.annotations_locked == ()
    # one note was appended: it matches
    with mef3io.Writer(str(a), overwrite=False) as w:
        w.write_annotations([{"time": T0 + 8 * SEC, "type": "Note", "text": "late"}])
    assert data.refresh_notes({0: 1}) is True
    assert len(data.annotations()) == len(before) + 1
    # a closed or never-read data cannot be refreshed
    fresh_data = type(data).__new__(type(data))
    fresh_data._annotations, fresh_data._closed = None, False
    fresh_data._notes_lock = threading.Lock()
    assert fresh_data.refresh_notes({0: 1}) is False


def test_when_the_notes_cannot_be_proved_the_save_reopens_like_before(ed, qtbot, monkeypatch):
    from mef3io_studio.model.viewer_source import ViewerData

    win, spy, _ = ed
    turn_on(win)
    monkeypatch.setattr(ViewerData, "refresh_notes", lambda self, appended, *rest: False)
    seen = watch_the_page(win, monkeypatch, qtbot)
    order = []
    real_drain = win.controller.drain_strip
    win.controller.drain_strip = lambda *a, **k: (order.append("drain"), real_drain(*a, **k))[1]
    epoch = win.data.epoch
    win.edit.add(T0 + 30 * SEC, None, "Spike", "after a reopen")
    finish_save(qtbot, win)
    assert order == ["drain"]  # a read of the index ends before the data is reopened
    assert seen["reopen"] == 1 and win.data.epoch == epoch + 1 and seen["loading"][:1] == [True]
    assert not spy.problems and not win.edit.draft.dirty and not win.edit.saving
    assert "Spike: after a reopen" in [m.text for m in win.marks]


def test_a_reopen_that_fails_after_the_notes_were_stored_says_so(ed, qtbot, monkeypatch):
    from mef3io_studio.model.viewer_source import ViewerData

    win, spy, (a, b) = ed
    turn_on(win)
    monkeypatch.setattr(ViewerData, "refresh_notes", lambda self, appended, *rest: False)

    def broken(self):
        raise OSError("gone")

    monkeypatch.setattr(ViewerData, "reopen", broken)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "stored anyway")
    finish_save(qtbot, win)
    assert "cannot be shown" in spy.problems[-1].title
    assert not win.edit.draft.dirty and not win.edit.saving  # stored, so not in the draft
    assert "Spike: stored anyway" in [c[4] for c in comparable(records(a))]


# --- removing and changing: a draft note yes, a stored note not yet ------------------------------


def entries(menu):
    return {a.text(): a for a in menu.actions() if a.text()}


def test_delete_removes_a_draft_note_and_undo_brings_it_back(ed):
    win, spy, _ = ed
    at(win, T0 + 12 * SEC)
    key(win, Qt.Key.Key_N)
    type_in(win, "to be removed")
    press(win, Qt.Key.Key_Return)
    (item,) = win.edit.draft.items
    win.edit.select_item(item)
    key(win, Qt.Key.Key_Delete)
    assert not win.edit.draft.dirty and "left the draft" in win.status_message.text()
    assert not [m for m in win.notes_tab.rows if m.state]
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert [i.text for i in win.edit.draft.items] == ["to be removed"]
    # without a selection Delete says what to do
    win.edit.select(None)
    key(win, Qt.Key.Key_Delete)
    assert win.status_message.text() == "Select a note or event first."


def test_the_menu_on_a_draft_note_removes_it_and_changes_its_text_in_the_field(ed):
    win, spy, _ = ed
    turn_on(win)
    item = win.edit.add(T0 + 12 * SEC, None, "Note", "first words")
    mark = next(m for m in win.notes_tab.rows if m.state == "new")
    from PySide6.QtWidgets import QMenu

    menu = QMenu()
    win.edit.note_menu(menu, mark)
    found = entries(menu)
    assert set(found) == {"Change the text", "Remove this note (Delete)"}
    assert all(a.isEnabled() for a in found.values())
    found["Change the text"].trigger()
    inline = win.edit.inline
    assert inline.isVisible() and inline.field.text() == "first words"
    assert "Change the note" in inline.head.text()
    inline.field.setText("better words")
    press(win, Qt.Key.Key_Return)
    assert [(i.uid, i.text) for i in win.edit.draft.items] == [(item.uid, "better words")]
    mark = next(m for m in win.notes_tab.rows if m.state == "new")
    menu = QMenu()
    win.edit.note_menu(menu, mark)
    entries(menu)["Remove this note (Delete)"].trigger()
    assert not win.edit.draft.dirty


def test_changing_the_text_into_a_copy_is_refused(ed):
    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 12 * SEC, None, "Note", "one")
    other = win.edit.add(T0 + 12 * SEC, None, "Note", "two")
    win.edit.change_text(other.uid)
    win.edit.inline.field.setText("one")
    press(win, Qt.Key.Key_Return)
    assert win.status_message.text() == ad.DUPLICATE_DRAFT
    assert win.edit.draft.get(other.uid).text == "two"


def test_a_right_click_on_a_chip_and_on_a_list_row_opens_that_menu(ed, qtbot):
    win, spy, _ = ed
    from PySide6.QtCore import QPoint

    win.edit.add(T0 + 12 * SEC, None, "Note", "draft")  # (turns nothing on: add works)
    rect = next(r for r, c, _l in win.chips.drawn if c.state == "new")
    QTest.mouseClick(
        win.chips,
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
        rect.center().toPoint(),
    )
    assert "Remove this note (Delete)" in entries(win._menu)
    win._menu.close()
    row = next(i for i, m in enumerate(win.notes_tab.rows) if m.text == "Alpha plain note")
    table = win.notes_tab.table
    pos = table.visualItemRect(table.item(row, 0)).center()
    win.notes_tab._row_menu(QPoint(pos.x(), pos.y()))
    assert entries(win._menu)["Mark for removal"].isEnabled()  # (nothing is written until Write)
    win._menu.close()
    # on the traces: right-click on the drawn mark of a stored note
    from PySide6.QtCore import QPointF

    mark = next(
        m for m in win.canvas.marks if m.source is not None and m.source.text == "Alpha plain note"
    )
    win.canvas._pointer = QPointF(win.canvas.x_of(mark.time), win.canvas.plot_rect().top() + 5)
    win._context_menu(0, mark.time, win.canvas.mapToGlobal(QPoint(5, 5)), False)
    assert "Mark for removal" in entries(win._menu)
    win._menu.close()


def test_the_only_calls_that_change_a_recordings_notes_are_in_one_module():
    """Appending (the Writer) and taking out (remove_annotations) are each called once, in
    annotation_save; nothing there deletes, renames or truncates a file."""
    import inspect as _inspect

    from mef3io_studio.model import annotation_save as asave

    source = _inspect.getsource(asave)
    assert source.count("mef3io.Writer(") == 1 and "write_annotations" in source
    assert source.count("mef3io.remove_annotations(") == 1
    for word in ("unlink", "os.remove", "shutil", "truncate", "os.rename", "os.replace"):
        assert word not in source


def test_closing_or_discarding_with_changes_leaves_the_recording_as_it_was(ed, qtbot):
    win, spy, (a, _b) = ed
    turn_on(win)
    snap = tree_hash(a)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "dropped")
    win.edit.remove_mark(next(m for m in win.notes_tab.rows if m.text == "Alpha plain note"))
    # leaving edit mode and then discarding from the red bar (the question is the real one's
    # stand-in: "Discard changes" answered yes)
    win.edit_button.click()
    assert not win.edit.on and win.write_bar.isVisible()
    assert win.edit.discard_ask() is True
    assert spy.discard_asked and not win.edit.draft.dirty and not win.write_bar.isVisible()
    assert tree_hash(a) == snap
    # closing with "Discard changes" closes without touching the recording
    turn_on(win)
    win.edit.add(T0 + 31 * SEC, None, "Spike", "dropped too")
    spy.answer = "discard"
    win.close()
    assert win._closed and tree_hash(a) == snap
