# ruff: noqa: F811, F401
"""Review round 1 of the viewer branch (PR #95): one test for each finding that was fixed.
Synthetic recordings only. Each test fails without its fix."""

from __future__ import annotations

import errno
import os
import threading
import time
import warnings

import mef3io
import pytest
from PySide6.QtCore import Qt
from test_viewer_edit import (  # noqa: F401
    Spy,
    ed,
    finish_save,
    open_locked,
    records,
    tree_hash,
    turn_on,
)
from test_viewer_marks_ui import make_window, pair, wait_marks, write  # noqa: F401
from test_viewer_open_files import HAVE_FD_LIST, held_signal_files
from test_viewer_remove_notes import NOTE, Asked, remove, stored, texts  # noqa: F401
from test_viewer_ui import LIGHT, open_data, settle

from mef3io_studio.model import annotation_save as asave
from mef3io_studio.model import viewer_marks as vm
from mef3io_studio.viewer.window import ViewerWindow

T0 = 946_684_800_000_000
SEC = 1_000_000


class Fake:
    """The stored notes, as ``ViewerData.annotations()`` hands them over."""

    def __init__(self, recs):
        self._recs = recs

    def annotations(self):
        return self._recs


def rec(i, kind="Note", text=None, recording=0, duration=None, time_=None):
    out = {
        "type": kind,
        "time": T0 + i if time_ is None else time_,
        "level": "session",
        "channel": None,
        "segment": None,
        "text": f"n{i}" if text is None else text,
        "recording": recording,
    }
    if duration is not None:
        out["duration"] = duration
    return out


def mark_of(r):
    return vm.Mark(
        r["time"], vm.mark_end(r), r["type"], r["recording"], r["level"], r["channel"], r["text"]
    )


# --- notes review 1: a kind of note an edit would change -------------------------------------


def test_a_stored_note_of_another_kind_can_be_removed_but_not_changed(make_window, tmp_path, qtbot):
    p = write(
        tmp_path / "Log.mefd",
        notes=[
            {"time": T0 + 5 * SEC, "type": "SyLg", "text": "log line"},
            {"time": T0 + 8 * SEC, "type": "Note", "text": "plain"},
        ],
    )
    win = make_window(p, reveal=lambda i: True)
    wait_marks(qtbot, win)
    spy = Spy(win)
    log = next(m for m in win.notes_tab.rows if m.code == "SyLg")
    before = tree_hash(p)
    turn_on(win)
    win.edit.ask_note = lambda editor: pytest.fail("the editor must not open")
    win.edit.select(log)
    assert win.edit.edit_selected() is None
    assert "only be removed" in win.last_refusal and tree_hash(p) == before
    # forced through the draft: Save refuses and writes nothing (the type would be lost)
    win.edit.draft.edit_stored(log, text="changed")
    assert win.edit.save() is False
    assert "only be removed" in win.last_refusal and tree_hash(p) == before
    assert not spy.stored_lines  # nothing was asked: nothing can be done
    win.edit.draft.clear()
    win.edit.refresh()
    Asked(win)
    remove(qtbot, win, log)  # removing is fine
    assert texts(p) == ["plain"]


def test_an_edited_note_keeps_its_kind_and_its_length(make_window, tmp_path, qtbot):
    p = write(
        tmp_path / "Ev.mefd",
        notes=[
            {"time": T0 + 5 * SEC, "type": "EDFA", "duration": 2 * SEC, "text": "Spike: sharp"},
            {"time": T0 + 20 * SEC, "type": "Note", "text": "plain"},
        ],
    )
    win = make_window(p, reveal=lambda i: True)
    wait_marks(qtbot, win)
    Spy(win)
    turn_on(win)
    ev = next(m for m in win.notes_tab.rows if m.code == "EDFA")
    win.edit.draft.edit_stored(ev, text="blunt")
    finish_save(qtbot, win)
    got = [r for r in records(p) if r["type"] == "EDFA"]
    assert len(got) == 1 and got[0]["duration"] == 2 * SEC and got[0]["text"] == "Spike: blunt"


def test_one_copy_of_a_kind_that_cannot_be_written_back_is_refused():
    r1, r2 = rec(1, "Epoc"), rec(1, "Epoc")
    data = Fake([r1, r2])
    mark = mark_of(r1)
    remove_, add, why = asave._selectors(data, 0, [asave.StoredChange(mark, 2, 1)])
    assert why == "copy-type" and remove_ is None  # one copy would be added back incomplete
    remove_, add, why = asave._selectors(data, 0, [asave.StoredChange(mark, 2, 2)])
    assert why == "" and add == []  # all copies: fine
    # the same note of a kind that can be written back whole: one copy can stay
    n1, n2 = rec(1), rec(1)
    remove_, add, why = asave._selectors(Fake([n1, n2]), 0, [asave.StoredChange(mark_of(n1), 2, 1)])
    assert why == "" and len(add) == 1


def test_the_line_for_such_a_note_stored_twice_offers_only_all(ed):
    win, spy, _ = ed
    r = rec(1, "Epoc", recording=0)
    m = mark_of(r)
    win.marks = [m, m]  # as the notes list would show it
    asked = []
    win.edit.ask_remove_stored = lambda line, copies: (asked.append((line, copies)), "one")[1]
    assert win.edit._remove_stored(m) is False  # "one copy" is not possible: nothing starts
    assert asked and asked[0][1] == 1 and "stored twice" in asked[0][0]
    assert "only be removed with all its copies" in asked[0][0]


# --- notes review 2: matching is linear -----------------------------------------------------


def test_2000_changes_among_50000_stored_notes_are_matched_quickly(ed):
    n = 50_000
    recs = [rec(i) for i in range(n)]
    marks = [mark_of(r) for r in recs[::25]]  # 2,000 of them
    changes = [asave.StoredChange(m, 1, 1) for m in marks]
    keys = {"record": 0, "mark": 0}
    real_record, real_mark = asave.record_key, asave.mark_key

    def count_record(r):
        keys["record"] += 1
        return real_record(r)

    def count_mark(m):
        keys["mark"] += 1
        return real_mark(m)

    asave.record_key, asave.mark_key = count_record, count_mark
    try:
        started = time.perf_counter()
        remove_, add, why = asave._selectors(Fake(recs), 0, changes)
        assert why == "" and len(remove_) == 2000
        assert time.perf_counter() - started < 5  # quadratic: minutes
        # the work: every stored note looked at ONCE (the index), every change once
        assert keys == {"record": n, "mark": 2000}
        # and the window's own count of copies (the GUI thread): one pass, then lookups
        keys.update(record=0, mark=0)
        win, _spy, _ = ed
        win.marks = [mark_of(r) for r in recs]
        started = time.perf_counter()
        assert all(win.edit._identical_copies(m) == 1 for m in marks)
        assert time.perf_counter() - started < 5
        assert keys["mark"] == n + 2000  # the one pass and one lookup for each change
    finally:
        asave.record_key, asave.mark_key = real_record, real_mark


# --- notes review 3: an edited stored note stays in its own recording ----------------------------


def test_a_stored_note_cannot_be_moved_into_another_recording(ed):
    win, spy, (a, b) = ed
    values = {"time": T0 + 100 * SEC, "end": None, "label": "Note", "channel": None, "text": "x"}
    # B holds data at 100 s: for a NEW note that is fine, for a stored note of A it is not
    assert win.edit._check_values(values) == ""
    why = win.edit._check_values(values, within=0)
    assert "stays there" in why and "remove it and add a new note" in why
    inside = dict(values, time=T0 + 6 * SEC)
    assert win.edit._check_values(inside, within=0) == ""
    # and Save refuses it if it was forced into the draft
    turn_on(win)
    mark = stored(win, "Alpha plain note")
    win.edit.draft.edit_stored(mark, time=T0 + 100 * SEC)
    before = tree_hash(a), tree_hash(b)
    assert win.edit.save() is False
    assert "stays there" in win.last_refusal and (tree_hash(a), tree_hash(b)) == before


# --- notes review 4: the text is kept exactly ----------------------------------------------------


def test_changing_only_the_time_keeps_the_text_exactly(make_window, tmp_path, qtbot):
    p = write(
        tmp_path / "Sp.mefd",
        notes=[{"time": T0 + 5 * SEC, "type": "Note", "text": "trail "}],
    )
    win = make_window(p, reveal=lambda i: True)
    wait_marks(qtbot, win)
    Spy(win)
    turn_on(win)
    mark = next(m for m in win.notes_tab.rows if m.text == "trail ")
    win.edit.draft.edit_stored(mark, time=mark.time + 1000)
    finish_save(qtbot, win)
    got = records(p)
    assert [r["text"] for r in got] == ["trail "] and got[0]["time"] == T0 + 5 * SEC + 1000


# --- notes review 5: words for the two codes it missed ---------------------------------------


def test_the_interrupted_edit_words_name_the_working_rescue_command():
    for code in ("edit-journal-inconsistent", "edit-interrupted", "edit-unsettled"):
        exc = Exception("x")
        exc.code = code
        text = asave.describe_removal_error(exc, "Recording A").text
        assert "python -m mef3io recover --apply <folder>" in text, code
        assert "rescue" in text and "Nothing was removed" in text
    exc = Exception("x")
    exc.code = "records-encryption-mismatch"
    message = asave.describe_removal_error(exc, "Recording A")
    assert message.title != "Something went wrong" and "password protection" in message.title


# --- notes review 6: an interrupted notes edit says so ----------------------------------------


def test_a_recording_with_an_interrupted_notes_edit_says_why_and_what_to_do(
    make_window, tmp_path, qtbot
):
    from mef3io.annotations import _remove_annotations

    p = write(tmp_path / "Half.mefd", notes=[NOTE, {"time": T0 + 9 * SEC, "text": "two"}])
    with pytest.raises(Exception):  # noqa: B017 - the library's own simulated crash
        _remove_annotations(
            str(p),
            [dict(r) for r in records(p) if r["text"] == NOTE["text"]],
            [],
            _crash_at=6,
        )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        win = make_window(p, reveal=lambda i: True)
        wait_marks(qtbot, win)
    assert win.notes_failed
    tip = win.notes_button.toolTip()
    assert "rescued" in tip and "Check for problems" in tip
    assert win.notes_tab.failed.text() == tip and not win.notes_tab.failed.isHidden()


# --- notes review 7: messages ---------------------------------------------------------------------


def test_a_partial_result_says_exactly_what_was_and_was_not_done():
    exc = Exception("x")
    exc.code = "edit-failed"
    text = asave.describe_removal_error(exc, "Recording B", ["A"]).text
    assert "Nothing was removed" not in text
    assert "Recording B was not changed" in text and "recording A" in text and "undone" in text
    assert "Nothing was removed" in asave.describe_removal_error(exc, "Recording B").text


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions")
def test_a_refused_write_is_read_only_only_when_the_folder_is(tmp_path):
    folder = tmp_path / "r.mefd"
    folder.mkdir()
    denied = Exception("cannot replace x: Access is denied")  # as Windows says it, for a file
    # held open by another program: the folder is writable, so both causes are named
    message = asave.describe_removal_error(denied, "Recording A", path=str(folder))
    assert message.code == "denied" and "another program holds" in message.text
    assert "read-only" in message.text  # named as a possibility, not claimed
    coded = Exception("cannot replace x: Access is denied")
    coded.code = "edit-failed"
    held = asave.describe_removal_error(coded, "Recording A", path=str(folder))
    assert held.code == "edit-failed" and "Windows" in held.text
    # the same words for a folder that really is not writable
    os.chmod(folder, 0o500)
    try:
        message = asave.describe_removal_error(denied, "Recording A", path=str(folder))
    finally:
        os.chmod(folder, 0o700)
    assert message.code == "read-only" and "Windows" not in message.text
    # the system itself says read-only
    assert asave.describe_removal_error(OSError(errno.EROFS, "x"), "Recording A").code == (
        "read-only"
    )


def test_a_failure_without_a_code_after_earlier_recordings_were_changed_is_truthful():
    message = asave.describe_removal_error(Exception("x"), "Recording B", ["A"])
    assert "were not changed" not in message.text and "Nothing was removed" not in message.text
    assert "Recording B was not changed" in message.text and "recording A" in message.text
    alone = asave.describe_removal_error(Exception("x"), "Recording B")
    assert "Nothing was removed" in alone.text


def test_the_same_note_in_two_recordings_is_two_notes(make_window, tmp_path, qtbot):
    note = {"time": T0 + 5 * SEC, "type": "Note", "text": "same words"}
    a = write(tmp_path / "Left.mefd", channels=("Fp1", "Fp2"), notes=[note])
    b = write(tmp_path / "Right.mefd", channels=("Cz", "Pz"), notes=[dict(note)])
    win = make_window(a, b, reveal=lambda i: True)
    wait_marks(qtbot, win)
    Spy(win)
    Asked(win)
    mark = next(m for m in win.notes_tab.rows if m.recording == 0)
    assert win.edit._identical_copies(mark) == 1  # one in A, one in B: not two copies
    remove(qtbot, win, mark)
    assert texts(a) == [] and texts(b) == ["same words"]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX permissions")
@pytest.mark.parametrize(
    ("folder", "note"),
    [("", "Alpha plain note"), ("Cz.timd", "Alpha Cz note")],
    ids=["the recording's folder", "a channel's folder"],
)
def test_removing_from_a_read_only_recording_says_so_and_changes_nothing(ed, qtbot, folder, note):
    win, spy, (a, _b) = ed
    Asked(win)
    before = tree_hash(a)
    target = os.path.join(a, folder) if folder else str(a)
    os.chmod(target, 0o500)
    try:
        win.edit.remove_mark(stored(win, note))
        qtbot.waitUntil(lambda: not win.edit.saving and bool(spy.problems), timeout=30_000)
    finally:
        os.chmod(target, 0o700)
    assert tree_hash(a) == before
    assert "read-only" in spy.problems[0].text and "Windows" not in spy.problems[0].text


def test_the_line_before_changing_one_of_two_identical_copies_says_so(make_window, tmp_path, qtbot):
    p = write(tmp_path / "Two.mefd", notes=[NOTE, dict(NOTE)])
    win = make_window(p, reveal=lambda i: True)
    wait_marks(qtbot, win)
    spy = Spy(win)
    turn_on(win)
    mark = stored(win, "Alpha plain note")
    win.edit.draft.edit_stored(mark, text="Alpha changed")
    finish_save(qtbot, win)
    assert "stored more than once" in spy.stored_lines[0]
    assert sorted(texts(p)) == ["Alpha changed", "Alpha plain note"]


# --- merge review 1: a closed window lets go at once, and stays counted until it has ----------


def test_a_closing_window_holds_no_file_before_it_has_shut_down(qtbot, qapp, tmp_path):
    p = write(tmp_path / "a.mefd", seconds=120)
    data = open_data(p)
    real = data.envelope
    gate, started = threading.Event(), threading.Event()

    def slow(*a, **k):
        started.set()
        gate.wait(10)  # the index read is in flight for as long as the test wants
        return real(*a, **k)

    data.envelope = slow
    win = ViewerWindow(data, theme=LIGHT)
    qtbot.addWidget(win)
    win.resize(1200, 700)
    win.show()
    qtbot.waitUntil(lambda: win._ready, timeout=10_000)
    settle(qtbot, win)
    qtbot.waitUntil(started.is_set, timeout=20_000)  # the index read is in flight
    if HAVE_FD_LIST:  # (Linux only: no list of open files elsewhere without a helper)
        qtbot.waitUntil(lambda: bool(held_signal_files(tmp_path)), timeout=10_000)
    released = []
    win.released.connect(lambda: released.append(1))
    win.close()
    if HAVE_FD_LIST:
        assert held_signal_files(tmp_path) == []  # at once, not when the read has ended
    qtbot.wait(300)
    assert not released  # the window still counts as holding its recordings
    gate.set()
    qtbot.waitUntil(lambda: bool(released), timeout=20_000)


def test_the_app_keeps_a_closing_viewer_in_its_list_until_it_is_released(window, qtbot, tmp_path):
    p = write(tmp_path / "a.mefd", seconds=120)
    data = open_data(p)
    real, gate, started = data.envelope, threading.Event(), threading.Event()

    def slow(*a, **k):
        started.set()
        gate.wait(10)
        return real(*a, **k)

    data.envelope = slow
    win = ViewerWindow(data, theme=LIGHT)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitUntil(started.is_set, timeout=20_000)  # the index read is in flight
    window.add_viewer(win)  # what the app does with every viewer it opens
    win.close()
    qtbot.wait(300)
    assert win in window.viewers  # closed, but still shutting down: still counted
    gate.set()
    qtbot.waitUntil(lambda: win not in window.viewers, timeout=20_000)


# --- merge review 2: a channel picked for the band is not kept waiting -----------------------


def test_a_picked_overview_channel_does_not_wait_behind_the_automatic_read(
    make_window, tmp_path, qtbot, monkeypatch
):
    from mef3io_studio.viewer import controller as ctl
    from mef3io_studio.workers.viewer_scheduler import TileScheduler

    monkeypatch.setattr(ctl, "INDEX_WAIT", 6.0)
    monkeypatch.setattr(TileScheduler, "pending", lambda self: 1)  # the automatic read waits
    p = write(tmp_path / "a.mefd")
    data = open_data(p)
    win = ViewerWindow(data, theme=LIGHT)
    qtbot.addWidget(win)
    win.resize(1200, 700)
    win.show()
    qtbot.waitUntil(lambda: win._ready, timeout=10_000)
    qtbot.wait(400)  # the automatic read is queued and waiting
    other = next(c for c in win._channels if c not in win.overview_channels())
    started = time.monotonic()
    win.pick_overview_channel(other, True)
    qtbot.waitUntil(lambda: other in win.controller.overview, timeout=5_000)
    assert time.monotonic() - started < 3.0


# --- merge review 3: a release during a read does not lose the hold --------------------------


def test_a_release_during_a_read_keeps_the_files_known_as_held(tmp_path):
    p = write(tmp_path / "a.mefd", seconds=60)
    data = open_data(p)
    try:
        names = list(data.channels)
        with data._reading():
            data.release_files()  # the idle release, while a read begins
            data._combined.read(names, data.timeline_start, data.timeline_start + 5 * SEC)
        if HAVE_FD_LIST:
            assert held_signal_files(tmp_path)  # the read opened them again
        assert data.idle_with_files(0.0)  # so the next idle release still lets go
    finally:
        data.close()


# --- merge review 4: the strip's stretches come back after a reopen with an unchanged signal -----


def test_the_stretches_are_asked_for_again_after_a_reopen_of_an_unchanged_signal(
    make_window, tmp_path, qtbot
):
    data = open_data(write(tmp_path / "a.mefd", seconds=120))
    real, gate, started = data.envelope, threading.Event(), threading.Event()

    def slow(*a, **k):
        started.set()
        gate.wait(10)
        return real(*a, **k)

    data.envelope = slow
    win = ViewerWindow(data, theme=LIGHT)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitUntil(started.is_set, timeout=20_000)  # the read of the stretches is in flight
    assert win._stretches == []
    # what a save does when the signal could not be shown unchanged: the read is ended, the
    # recordings are opened again, and the signal turns out to be as it was
    win.begin_data_change()
    win.controller.scheduler.drain(10)
    threading.Timer(0.3, gate.set).start()
    win.controller.drain_strip(10)  # waits for the read, drops its result
    data.reopen()
    qtbot.wait(100)
    assert win._stretches == []  # the dropped read left nothing
    win.end_data_change(list(data.annotations()), tuple(data.annotations_locked))
    qtbot.waitUntil(lambda: bool(win._stretches), timeout=20_000)


# --- merge review 6: at 1280 px nothing essential is out of reach -----------------------------


@pytest.mark.parametrize("pixels", [8, 11, 14, 17, 24, 40])
def test_the_axis_leaves_air_between_its_two_lines_at_every_font_size(
    make_window, tmp_path, pixels
):
    """The time labels and the note under them (the overview text) are separated by
    ``axis_height - 2 * line - 6`` px; the rule must leave more than half a line at any font."""
    from PySide6.QtGui import QFont

    win = make_window(write(tmp_path / "a.mefd"))
    font = QFont(win.canvas.font())
    font.setPixelSize(pixels)
    win.canvas.setFont(font)
    line = win.canvas.fontMetrics().height()
    assert win.canvas.axis_height - 2 * line - 6 >= line // 2 + 3


def controls_of(win):
    """Every control of the window's flow, as (name, widget)."""
    return [
        ("prev", win.prev_button), ("next", win.next_button), ("day", win.day_box),
        ("time", win.time_edit), ("go", win.go_button), ("jump", win.jump_button),
        ("notes", win.notes_button), ("edit", win.edit_button),
        ("export start", win.export_start_button), ("export end", win.export_end_button),
        ("export range", win.range_button), ("help", win.help_button),
        ("montage", win.montage_bar), ("channels", win.channels_button),
        ("spacing", win.spacing_box), ("sensitivity", win.sens_box), ("auto", win.auto_button),
        ("timebase", win.timebase_box), ("time on screen", win.length_label),
        ("filters", win.filters_button), ("types", win.types_button),
        ("rows", win.rows_label), ("overview", win.overview_button),
        ("colours", win.profile_box),
    ]  # fmt: skip


@pytest.mark.parametrize("points", [9, 11, 13, 15])
@pytest.mark.parametrize("width", [1000, 1280, 1920])
def test_every_control_is_on_screen_at_any_width_and_font_size(
    make_window, tmp_path, qtbot, width, points
):
    """The controls are one wrapping flow (viewer/flow.py): nothing falls into a ">>" menu,
    whatever the width or the font. Each one is shown and lies inside the window, the flow is
    as tall as ``heightForWidth`` says, and the signal gets the rest."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    old = app.font()
    font = QFont(old)
    font.setPointSizeF(float(points))
    app.setFont(font)
    try:
        # many channels, so that not every row fits and the rows label ("Rows 1-n of m") shows
        p = write(tmp_path / "Many.mefd", channels=[f"Ch{i:02d}" for i in range(80)])
        win = make_window(p)
        win.resize(width, 800)
        qtbot.wait(300)
        assert win.rows_label.text()  # there is something to say about the rows
        inside = win.rect()
        for name, w in controls_of(win):
            assert w.isVisibleTo(win), name
            top_left = w.mapTo(win, QPoint(0, 0))
            assert inside.contains(top_left) and inside.contains(
                top_left + QPoint(w.width() - 1, w.height() - 1)
            ), (name, top_left, w.size(), inside)
        box = win.controls
        assert box.height() >= box.flow.heightForWidth(box.width())  # the flow gets its height
        assert win.canvas.height() > 150  # and the signal the rest
        if width == 1920 and points == 9:
            assert box.rows() <= 2, box.rows()  # a wide screen: the two rows it always had
    finally:
        app.setFont(old)


def test_a_group_is_never_split_over_two_rows(make_window, tmp_path, qtbot):
    from PySide6.QtCore import QPoint

    win = make_window(write(tmp_path / "a.mefd"))
    for width in (700, 900, 1100, 1500):
        win.resize(width, 800)
        qtbot.wait(150)
        for label_box in (win.timebase_box, win.sens_box, win.spacing_box):
            group = label_box.parentWidget()
            ys = {
                child.mapTo(win, QPoint(0, 0)).y()
                for child in group.findChildren(type(label_box).__mro__[0])
                if child.parent() is group
            }
            assert len(ys) == 1, (width, ys)  # the label and its control: one row


def test_tab_goes_through_the_controls_in_the_order_they_are_shown(make_window, tmp_path):
    win = make_window(write(tmp_path / "a.mefd"))
    from PySide6.QtCore import QPoint

    win.resize(900, 800)  # wrapped onto several rows
    win.show()
    mine = [
        w
        for _, w in controls_of(win)
        if w.focusPolicy() != Qt.FocusPolicy.NoFocus and w.isVisibleTo(win)
    ]

    def where(w):
        # the flow item (a group or the widget itself) decides the row; then left to right
        item = w if w.parent() is win.controls else w.parent()
        a, b = item.mapTo(win, QPoint(0, 0)), w.mapTo(win, QPoint(0, 0))
        return (a.y(), a.x(), b.x())

    visual = sorted(mine, key=where)  # row by row, left to right
    chain, w = [], visual[0]
    while True:  # the focus chain, restricted to these controls, from the first one
        if w in mine:
            chain.append(w)
        w = w.nextInFocusChain()
        if w is visual[0]:
            break
    assert chain == visual
