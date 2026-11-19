# ruff: noqa: F811, F401
"""Removing a note that is already STORED in a recording (mef3io 2.4 ``remove_annotations``),
and changing a stored note's text (remove + add in one call). Synthetic recordings in tmp_path.

The rules under test: the GUI thread never calls the library (a task does); one plain line
asks first and says it cannot be undone; the files of the signal are never touched; every refusal
of the library has plain words and a next step; Stop leaves the recording as it was."""

from __future__ import annotations

import re
import threading
from pathlib import Path

import mef3io
import pytest
from PySide6.QtCore import Qt
from test_ui_language import BANNED_RE
from test_viewer_edit import (  # noqa: F401
    Spy,
    ed,
    entries,
    finish_save,
    key,
    open_locked,
    records,
    tree_hash,
    turn_on,
    wide,
)
from test_viewer_marks_ui import make_window, pair, wait_marks, write  # noqa: F401
from test_viewer_ui import settle

from mef3io_studio.model import annotation_save as asave
from mef3io_studio.model.errors import UserMessage

T0 = 946_684_800_000_000
SEC = 1_000_000

NOTE = {"time": T0 + 5 * SEC, "type": "Note", "text": "Alpha plain note"}


def texts(path, pwd=""):
    return sorted(r["text"] for r in records(path, pwd))


def signal_hash(path):
    """Every file that is not a notes file: the signal and the details."""
    return {k: v for k, v in tree_hash(path).items() if not k.endswith((".rdat", ".ridx"))}


class Asked:
    """Answers the questions the window would ask, and remembers them."""

    def __init__(self, win, answer="all", other_password=None):
        self.lines: list[tuple[str, int]] = []
        self.answer = answer
        self.passwords_asked: list[str] = []
        self.other_password = other_password
        win.edit.ask_remove_stored = self.remove
        win.edit.ask_other_password = self.password

    def remove(self, line: str, copies: int):
        self.lines.append((line, copies))
        return self.answer

    def password(self, label: str, letter: str):
        self.passwords_asked.append(label)
        return self.other_password


def stored(win, text):
    return next(m for m in win.notes_tab.rows if m.text == text)


def remove(qtbot, win, mark):
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        assert win.edit.remove_mark(mark) is True
    qtbot.waitUntil(lambda: not win.edit.saving)
    settle(qtbot, win)


# --- one stored note ---------------------------------------------------------------------------


def test_a_stored_note_is_removed_after_one_plain_line(ed, qtbot):
    win, spy, (a, b) = ed
    ask = Asked(win)
    epoch = win.data.epoch
    before_signal, before_b = signal_hash(a), tree_hash(b)
    mark = stored(win, "Alpha plain note")
    remove(qtbot, win, mark)
    assert not spy.problems
    assert len(ask.lines) == 1 and ask.lines[0][1] == 1
    line = ask.lines[0][0]
    assert "cannot be undone" in line and "Recording A" in line
    assert "Alpha plain note" not in line  # the text of a note never goes into a line
    assert not BANNED_RE.search(line)
    assert "Alpha plain note" not in texts(a) and len(texts(a)) == 2  # the others stay
    assert signal_hash(a) == before_signal  # the signal files: same bytes, same times
    assert tree_hash(b) == before_b  # another recording is not touched
    assert all(m.text != "Alpha plain note" for m in win.notes_tab.rows)  # the window shows it
    assert win.data.epoch == epoch  # the notes were read again, nothing was reopened
    assert win.marks_loaded


def test_a_no_leaves_everything_as_it_was(ed, qtbot):
    win, spy, (a, _b) = ed
    Asked(win, answer=None)
    snap = tree_hash(a)
    assert win.edit.remove_mark(stored(win, "Alpha plain note")) is False
    assert tree_hash(a) == snap and not win.edit.saving and not spy.problems


def test_delete_on_a_selected_stored_note_removes_it(ed, qtbot):
    win, spy, (a, _b) = ed
    Asked(win)
    turn_on(win)
    win.edit.select(stored(win, "Alpha Cz note"))
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        key(win, Qt.Key.Key_Delete)
    qtbot.waitUntil(lambda: not win.edit.saving)
    assert "Alpha Cz note" not in texts(a) and not spy.problems  # a channel note too


def test_the_menu_entry_is_enabled_and_the_reason_line_is_gone(ed):
    from PySide6.QtWidgets import QMenu

    win, _spy, _ = ed
    menu = QMenu()
    win.edit.note_menu(menu, stored(win, "Alpha plain note"))
    found = entries(menu)
    assert found["Remove this note"].isEnabled() and len(found) == 1


def test_the_notes_tab_remove_button_removes_a_stored_note_too(ed, qtbot):
    win, spy, (a, _b) = ed
    Asked(win)
    turn_on(win)
    win.edit.select(stored(win, "Beta plain note"))
    with qtbot.waitSignal(win.edit.save_finished, timeout=60_000):
        assert win.edit.remove_selected()
    qtbot.waitUntil(lambda: not win.edit.saving)
    assert not spy.problems


# --- the same note stored twice -----------------------------------------------------------------


@pytest.fixture
def twice(make_window, tmp_path, qtbot):
    p = write(
        tmp_path / "Twice.mefd",
        notes=[
            NOTE,
            dict(NOTE),
            {"time": T0 + 10 * SEC, "type": "Note", "text": "Once"},
        ],
    )
    win = make_window(p, reveal=lambda i: True)
    wait_marks(qtbot, win)
    return win, Spy(win), p


def test_a_note_stored_twice_is_said_and_one_copy_can_go(twice, qtbot):
    win, spy, p = twice
    ask = Asked(win, answer="one")
    remove(qtbot, win, stored(win, "Alpha plain note"))
    assert ask.lines[0][1] == 2 and "twice" in ask.lines[0][0]
    assert texts(p) == ["Alpha plain note", "Once"]  # exactly one copy is left
    assert not spy.problems


def test_both_copies_can_go(twice, qtbot):
    win, spy, p = twice
    Asked(win, answer="all")
    remove(qtbot, win, stored(win, "Alpha plain note"))
    assert texts(p) == ["Once"] and not spy.problems


# --- changing the text of a stored note (remove + add in one call) -----------------------------


def test_a_changed_stored_note_is_replaced_in_one_call(ed, qtbot, monkeypatch):
    win, spy, (a, b) = ed
    Asked(win)
    calls = []
    real = mef3io.remove_annotations
    monkeypatch.setattr(
        mef3io, "remove_annotations", lambda *x, **k: (calls.append(k), real(*x, **k))[1]
    )
    turn_on(win)
    before_signal = signal_hash(a)
    mark = stored(win, "Alpha plain note")
    win.edit.draft.edit_stored(mark, text="Alpha corrected")
    win.edit.refresh()
    finish_save(qtbot, win)
    assert not spy.problems
    assert len(calls) == 1  # one library call: nothing is ever half-changed
    got = texts(a)
    assert "Alpha corrected" in got and "Alpha plain note" not in got and len(got) == 3
    assert signal_hash(a) == before_signal
    assert not win.edit.draft.dirty


# --- the library's refusals, in plain words -----------------------------------------------------

#: Codes of this one call that the library gives no hyphen (or no mention in its docstring).
_PLAIN_CODES = {"ambiguous", "password", "edit-interrupted", "mid-operation"}
#: Hyphenated strings of the library that are codes of OTHER calls (merge, validate,
#: transform ...), not of ``remove_annotations``.
_OTHER_CALLS = {
    "records-ambiguous", "edit-for", "edit-format", "edit-leftovers", "records-edit",
    "annotation-edit-interrupted", "offset-applied", "offset-assumed-zero",
    "offset-dependent", "no-channels-left", "ambiguous-time-convention",
    "password-level1-only", "password-protected", "password-required", "password-to-key",
    "password-wrong",
}  # fmt: skip
_PREFIX = re.compile(
    rb"^(record|records|edit|selector|selectors|add|bad|count|no-match|nothing|offset|unknown"
    rb"|being|mid|tar|verify|ambiguous)-"
)


def library_codes() -> set[str]:
    """Every refusal code of ``remove_annotations``, derived from the installed library: the
    codes its docstring names, plus every code-shaped string of its compiled part that starts
    like one (minus those that belong to other calls). A new library code with no words
    fails ``test_no_library_code_is_left_without_words``."""
    import inspect

    doc = inspect.getdoc(mef3io.remove_annotations) or ""
    found = set(re.findall(r"``([a-z]+(?:-[a-z]+)+)``", doc))
    so = next(Path(mef3io.__file__).parent.glob("_mef3io*"))
    data = so.read_bytes()
    for token in set(re.findall(rb"(?<![\w-])([a-z]+(?:-[a-z0-9]+)+)(?![\w-])", data)):
        if _PREFIX.match(token):
            found.add(token.decode())
    return (found - _OTHER_CALLS) | _PLAIN_CODES


CODES = sorted(library_codes() | {"copy-type", "edit-type", "read-only"})


def test_no_library_code_is_left_without_words():
    missing = sorted(library_codes() - set(asave.REMOVAL_WORDS))
    assert not missing, f"codes of remove_annotations without plain words: {missing}"
    assert {"records-encryption-mismatch", "edit-journal-inconsistent"} <= library_codes()


@pytest.mark.parametrize("code", CODES)
def test_every_refusal_has_plain_words_and_a_next_step(code):
    class Refused(Exception):
        pass

    exc = Refused("a library sentence naming /some/path and a note text")
    exc.code = code
    message = asave.describe_removal_error(exc, "Recording A")
    assert isinstance(message, UserMessage) and message.code == code
    for part in (message.title, message.text):
        shown = part.replace("python -m mef3io recover --apply <folder>", "")  # the technical()
        assert part and not BANNED_RE.search(shown), (code, part)
    assert "/some/path" not in message.text and "note text" not in message.text
    assert "Recording A" in message.text or "recording A" in message.text
    assert any(w in message.text for w in ("Nothing was", "put back", "were not changed")), code
    assert message.detail  # the library's own sentence stays behind "Technical details"
    assert message.title != "Something went wrong"  # none falls through to the general text


def test_an_unknown_error_gets_the_general_words_and_says_nothing_was_removed():
    message = asave.describe_removal_error(OSError("disk"), "Recording B")
    assert message.title and "Nothing was" in message.text


def test_assume_owner_gone_is_never_passed():
    import inspect

    # the app must never skip the library's check for a program that is still editing
    assert "assume_owner_gone" not in inspect.getsource(asave)


def test_a_note_that_changed_underneath_is_refused_and_nothing_changes(ed, qtbot):
    win, spy, (a, _b) = ed
    Asked(win)
    # another program removes it first: the window still shows it
    mef3io.remove_annotations(
        str(a), remove=[dict(r) for r in records(a) if r["text"] == NOTE["text"]]
    )
    snap = tree_hash(a)
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    qtbot.waitUntil(lambda: not win.edit.saving and bool(spy.problems), timeout=30_000)
    assert len(spy.problems) == 1 and "Nothing was" in spy.problems[0].text
    assert tree_hash(a) == snap


def test_the_library_refusing_is_shown_with_its_words(ed, qtbot, monkeypatch):
    win, spy, (a, _b) = ed
    Asked(win)

    def refuse(*args, **kwargs):
        exc = mef3io.WriteConflictError("the annotations changed while the call worked")
        exc.code = "records-changed"
        raise exc

    monkeypatch.setattr(mef3io, "remove_annotations", refuse)
    snap = tree_hash(a)
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    qtbot.waitUntil(lambda: not win.edit.saving and bool(spy.problems), timeout=30_000)
    assert spy.problems[0].code == "records-changed"
    assert "/" not in spy.problems[0].text  # no path
    assert tree_hash(a) == snap and not win.edit.saving


# --- protected and single-file recordings -------------------------------------------------------


def locked(make_window, tmp_path, qtbot, held):
    p = write(
        tmp_path / "Locked.mefd",
        password1="a",
        password2="b",
        notes=[NOTE, {"time": T0 + 10 * SEC, "type": "Note", "text": "Second"}],
    )
    win = open_locked(make_window, qtbot, p, held)
    return win, Spy(win), p


def test_a_protected_recording_asks_for_the_other_password_then_removes(
    make_window, tmp_path, qtbot
):
    win, spy, p = locked(make_window, tmp_path, qtbot, "b")
    ask = Asked(win, other_password="a")
    remove(qtbot, win, stored(win, "Alpha plain note"))
    assert ask.passwords_asked == ["basic password"] and not spy.problems
    assert texts(p, "b") == ["Second"]
    assert b"Second" not in b"".join(f.read_bytes() for f in Path(p).rglob("*") if f.is_file())
    win.close()


@pytest.mark.parametrize("typed", [None, "", "zzz", "b"], ids=["none", "empty", "wrong", "same"])
def test_a_protected_recording_without_the_right_password_is_refused_untouched(
    make_window, tmp_path, qtbot, typed
):
    win, spy, p = locked(make_window, tmp_path, qtbot, "b")
    Asked(win, other_password=typed)
    snap = tree_hash(p)
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
    if typed is None or typed == "":
        assert not spy.problems  # the person did not give one: nothing was started
    else:
        assert len(spy.problems) == 1 and "Nothing was" in spy.problems[0].text
        assert "Both passwords" in spy.problems[0].title or "passwords" in spy.problems[0].text
        assert not BANNED_RE.search(spy.problems[0].text)
    assert tree_hash(p) == snap
    win.close()


def test_a_single_file_recording_says_it_cannot_be_changed(make_window, pair, tmp_path, qtbot):
    tar = Path(mef3io.archive_session(str(pair[0])))
    win = make_window(tar, reveal=lambda i: True)
    wait_marks(qtbot, win)
    Spy(win)  # (nothing may be asked or shown)
    ask = Asked(win)
    snap = tar.read_bytes()
    mark = next(m for m in win.notes_tab.rows if m.text == "Alpha plain note")
    assert win.edit.remove_mark(mark) is False
    assert not ask.lines  # nothing was asked: it cannot be done
    assert win.status_message.text() and ".mefd.tar" not in win.status_message.text()
    assert "single-file" in win.last_refusal and not BANNED_RE.search(win.last_refusal)
    assert tar.read_bytes() == snap


# --- Stop ---------------------------------------------------------------------------------------


def test_stop_leaves_the_recording_as_it_was(ed, qtbot, monkeypatch):
    win, spy, (a, _b) = ed
    Asked(win)
    started = threading.Event()

    def slow(path, *args, cancel=None, **kwargs):
        started.set()
        while not cancel.is_set() if hasattr(cancel, "is_set") else not cancel.cancelled:
            threading.Event().wait(0.01)
        raise mef3io.Cancelled()

    monkeypatch.setattr(mef3io, "remove_annotations", slow)
    snap = tree_hash(a)
    win.edit.remove_mark(stored(win, "Alpha plain note"))
    qtbot.waitUntil(started.is_set, timeout=20_000)
    win.edit.last_job.cancel()
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
    assert tree_hash(a) == snap
    assert any(
        "stopped" in m.title.lower() or "stopped" in m.text.lower() for m in spy.problems
    ) or ("stopped" in win.status_message.text().lower())
    assert win.marks_loaded and any(m.text == "Alpha plain note" for m in win.notes_tab.rows)


# --- the GUI thread never calls the library -------------------------------------------------------


def test_the_library_is_called_from_a_task_not_the_gui_thread(ed, qtbot, monkeypatch):
    win, spy, _ = ed
    Asked(win)
    threads = []
    real = mef3io.remove_annotations
    monkeypatch.setattr(
        mef3io,
        "remove_annotations",
        lambda *a, **k: (threads.append(threading.current_thread()), real(*a, **k))[1],
    )
    remove(qtbot, win, stored(win, "Alpha plain note"))
    assert threads and all(t is not threading.main_thread() for t in threads)
