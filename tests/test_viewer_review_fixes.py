# ruff: noqa: F811, F401
"""Fixes after the independent review of the viewer upgrade (one section per item).
Synthetic sessions in tmp_path only."""

from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from test_viewer_edit import (
    SEC,
    T0,
    Spy,
    at,
    ed,
    finish_save,
    key,
    no_real_dialogs,
    press,
    turn_on,
    type_in,
    wide,
)
from test_viewer_marks_ui import Shown, make_window, pair, wait_marks, write

from mef3io_studio.model import annotation_draft as ad

# --- 1. N after paging by keyboard ----


def test_n_after_a_page_turn_with_the_mouse_at_rest_adds_on_the_page_shown(ed):
    win, spy, _ = ed
    win.set_timebase(10)
    win.go_to(T0)
    at(win, T0 + 12 * SEC)
    assert win._pointer_t is not None
    key(win, Qt.Key.Key_Right)  # the page turns; the mouse does not move
    lo, hi = win.state.t0, win.state.t0 + win.page_us()
    key(win, Qt.Key.Key_N)
    assert win.edit.inline.is_open
    type_in(win, "after paging")
    press(win, Qt.Key.Key_Return)
    (item,) = win.edit.draft.items
    assert lo <= item.time < hi


def test_the_anchor_is_never_off_the_page(ed):
    win, spy, _ = ed
    win.set_timebase(10)
    win.go_to(T0)
    win._pointer_t = T0 + 1000 * SEC  # a time long gone, no mouse move since
    win.canvas.selection = None
    t, end = win.edit._anchor()
    assert win.state.t0 <= t < win.state.t0 + win.page_us() and end is None
    assert t == win.state.t0 + win.page_us() // 2


# --- 2. the lead rule on a 10-10 cap ----

from mef3io_studio.model import montage as m  # noqa: E402

TEN_TWENTY = "Fp1 Fp2 F3 F4 F7 F8 C3 C4 T3 T4 T5 T6 P3 P4 O1 O2 Fz Cz Pz A1 A2".split()
TEN_TEN = (
    [f"F{i}" for i in range(1, 9)]
    + [f"FC{i}" for i in range(1, 5)]
    + [f"C{i}" for i in range(1, 7)]
    + [f"P{i}" for i in range(1, 9)]
    + ["Cz", "Fz", "Pz"]
)
DEPTH = [f"LA{i}" for i in range(1, 13)] + [f"RB{i}" for i in range(1, 9)]


def test_a_ten_ten_cap_has_no_leads_whatever_the_case_or_prefix():
    assert m.real_leads(TEN_TWENTY) == frozenset()
    assert m.real_leads(TEN_TEN) == frozenset()
    assert m.real_leads([f"EEG {n}" for n in TEN_TEN]) == frozenset()
    assert m.real_leads([n.lower() for n in TEN_TEN]) == frozenset()
    for mid in ("Fpz", "AFz", "FCz", "CPz", "POz", "Oz", "EEG Oz"):
        assert m.real_leads([f"P{i}" for i in range(1, 9)] + [mid]) == frozenset()


def test_depth_leads_beside_a_scalp_set_are_still_leads():
    assert m.real_leads(DEPTH) == {"LA", "RB"}
    assert m.real_leads(TEN_TEN + DEPTH) == {"LA", "RB"}
    assert m.real_leads(["EEG " + n for n in TEN_TEN] + DEPTH) == {"LA", "RB"}


def test_without_a_midline_name_the_rule_does_not_change():
    # no midline name: a four-contact prefix of scalp letters is taken for a lead, as before
    assert m.real_leads([f"F{i}" for i in range(1, 9)]) == {"F"}


# --- 3. hidden details: no typed text offered ----


def test_typed_texts_are_offered_only_while_details_are_shown(ed):
    from mef3io_studio.viewer.inline_note import Hint

    win, spy, _ = ed
    win.edit.remember_text("patient-detail-xyz")
    assert ("text", "patient-detail-xyz") in [(h.kind, h.value) for h in win.edit.hints()]
    shown = win._reveal
    win._reveal = lambda i: False  # details hidden again
    assert all(h.kind != "text" for h in win.edit.hints())
    win._reveal = lambda i: i == 0  # one recording still hidden
    assert all(h.kind != "text" for h in win.edit.hints())
    win._reveal = shown
    assert ("text", "patient-detail-xyz") in [(h.kind, h.value) for h in win.edit.hints()]
    assert "patient-detail-xyz" not in repr(Hint("text", "patient-detail-xyz"))


# --- 5. the held-centre estimate cannot lock to a period ----


@pytest.mark.parametrize("period", [40, 32, 64, 41, 100, 512, 1024, 2048])
def test_the_centre_of_a_periodic_trace_is_near_zero_whatever_the_period(period):
    from mef3io_studio.viewer.canvas import _RowShape

    n = 40_960
    ys = 500.0 * np.cos(2 * np.pi * np.arange(n) / period)  # read at one phase: 500
    shape = _RowShape([(np.arange(n) + 0.5, ys, False)], [])
    assert abs(shape.centre) < 150, (period, shape.centre)
    # the same trace in several pieces
    parts = [(np.arange(2048) + 0.5, ys[i : i + 2048], False) for i in range(0, 8192, 2048)]
    assert abs(_RowShape(parts, []).centre) < 150


def test_the_sample_is_the_same_every_time_and_short_arrays_are_kept_whole():
    from mef3io_studio.model.sampling import bounded_sample

    a = np.arange(100_000.0)
    assert np.array_equal(bounded_sample(a, 1024), bounded_sample(a, 1024))
    assert bounded_sample(a, 1024).size == 1024
    b = np.arange(500.0)
    assert bounded_sample(b, 1024) is b


# --- 6. the cost of a save: no inspect after the write ----


def test_a_save_inspects_each_recording_twice_before_the_write_and_never_after(
    ed, qtbot, monkeypatch
):
    import mef3io

    win, spy, _ = ed
    turn_on(win)
    win.data.read_facts()
    calls = []
    real = mef3io.inspect
    monkeypatch.setattr(mef3io, "inspect", lambda *a, **k: (calls.append(a[0]), real(*a, **k))[1])
    win.edit.add(T0 + 30 * SEC, None, "Spike", "in A")
    win.edit.add(T0 + 120 * SEC, None, "Note", "in B")
    finish_save(qtbot, win)
    assert not spy.problems and not win.edit.draft.dirty
    # prepare + the look right before the write, per recording; none to prove it afterwards
    assert len(calls) == 4, calls


def test_the_signal_snapshot_sees_signal_files_only_and_opens_nothing(tmp_path, monkeypatch):
    import builtins
    import os

    from mef3io_studio.model import annotation_save as asave

    path = write(tmp_path / "S.mefd")
    opened = []
    real_open = builtins.open
    monkeypatch.setattr(
        builtins, "open", lambda *a, **k: (opened.append(a[0]), real_open(*a, **k))[1]
    )
    before = asave.signal_snapshot(str(path))
    assert opened == []
    assert before and all(k.count("/") >= 2 for k in before)  # below the channel folders
    assert any(k.endswith(".tdat") for k in before)
    assert asave.signal_snapshot(str(path)) == before
    tdat = next(path.rglob("*.tdat"))
    st = tdat.stat()
    os.utime(tdat, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert asave.signal_snapshot(str(path)) != before


# --- 7. a warm page turn does not ask the same thing of every row ----


def test_the_name_rules_are_asked_once_per_name_not_once_per_row_and_page():
    m.lead_and_contact.cache_clear()
    m._surely_not_eeg.cache_clear()
    names = [f"LA{i}" for i in range(1, 65)]
    for _ in range(3):  # three page turns
        for n in names:
            m.is_non_eeg(n, {"LA"})
            m.lead_of(n, {"LA"})
    info = m.lead_and_contact.cache_info()
    assert info.misses == 64 and info.hits > 64 * 3
    assert m._surely_not_eeg.cache_info().misses == 64
    assert m.lead_and_contact("LA3") == ("LA", 3) and m.lead_and_contact("Cz") == ("Cz", None)


def test_the_geometry_of_a_row_identity_is_read_once_per_page_not_once_per_row(ed):
    win, _spy, _ = ed
    win.set_timebase(10)
    win.go_to(T0)
    asked = []
    real = type(win)._ident_tail
    type(win)._ident_tail = lambda self: (asked.append(1), real(self))[1]
    try:
        win._apply_pieces()
        n_rows = len(win.canvas.rows)
        assert n_rows >= 3 and 1 <= len(asked) <= 3, (n_rows, len(asked))
    finally:
        type(win)._ident_tail = real


# --- 8a. Delete on a combo box or a button does not take a note out ----


def test_delete_and_backspace_on_a_combo_box_or_a_button_keep_the_draft_note(ed):
    win, spy, _ = ed
    turn_on(win)
    item = win.edit.add(T0 + 30 * SEC, None, "Note", "kept")
    assert item is not None and win.edit.selected is not None
    for widget in (win.profile_box, win.edit_button):
        widget.setFocus()
        assert widget.hasFocus()
        for k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            QTest.keyClick(widget, k)
            assert [i.text for i in win.edit.draft.items] == ["kept"], (widget, k)
    # on the traces, and in the notes list, it still takes the note out
    win.canvas.setFocus()
    key(win, Qt.Key.Key_Delete)
    assert not win.edit.draft.items
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    win.edit.select_item(win.edit.draft.items[0])
    win.notes_tab.table.setFocus()
    assert win.notes_tab.table.hasFocus()
    QTest.keyClick(win.notes_tab.table, Qt.Key.Key_Backspace)
    assert not win.edit.draft.items


# --- 8b. the same note in two recordings side by side ----


def _both(channel, t):
    return [0, 1]  # two recordings hold data at every time


def test_two_recordings_side_by_side_can_each_hold_the_same_session_note():
    mk = lambda uid, rec: ad.DraftItem(uid, T0 + SEC, None, "Note", "same", None, rec)  # noqa: E731
    plan = ad.make_plan([mk(1, 0), mk(2, 1)], 0, _both)
    assert not plan.duplicates and sorted(plan.by_recording) == [0, 1]
    # the same recording twice is still one note
    again = ad.make_plan([mk(1, 0), mk(2, 0)], 0, _both)
    assert [i.uid for i in again.duplicates] == [2] and sorted(again.by_recording) == [0]
    # stored in recording 0: a copy for 0 is left out, one for 1 is written
    key = ad.item_key(mk(1, 0))
    stored = ad.make_plan([mk(1, 0), mk(2, 1)], 0, _both, stored_pairs={(0, key)})
    assert [i.uid for i in stored.duplicates] == [1] and sorted(stored.by_recording) == [1]
    # without the recording of the stored notes the old rule holds: any recording's copy counts
    old = ad.make_plan([mk(1, 0), mk(2, 1)], 0, _both, stored=[key])
    assert len(old.duplicates) == 2
    # a note whose recording is not chosen yet is asked about, not dropped
    free = ad.make_plan([ad.DraftItem(3, T0 + SEC, None, "Note", "same", None)], 0, _both)
    assert free.ambiguous and not free.duplicates


def test_the_draft_keeps_the_same_note_chosen_for_two_recordings():
    draft = ad.Draft()
    draft.stored_pairs = lambda: frozenset()
    rows = [
        {"time": T0 + SEC, "label": "Note", "text": "same", "recording": 0},
        {"time": T0 + SEC, "label": "Note", "text": "same", "recording": 1},
        {"time": T0 + SEC, "label": "Note", "text": "same", "recording": 1},
    ]
    assert draft.add_many(rows) == 2 and draft.skipped_duplicates == 1
    one = ad.DraftItem(0, T0 + SEC, None, "Note", "same", None, 0)
    assert draft.duplicate_reason(one) == ad.DUPLICATE_DRAFT
    other = ad.DraftItem(0, T0 + SEC, None, "Note", "different", None, 1)
    assert draft.duplicate_reason(other) == ""
    draft.stored_pairs = lambda: frozenset({(1, ad.item_key(other))})
    assert draft.duplicate_reason(other) == ad.DUPLICATE_STORED
    elsewhere = ad.DraftItem(0, T0 + SEC, None, "Note", "different", None, 0)
    assert draft.duplicate_reason(elsewhere) == ""


# --- 8c, 8d. a failed reopen switches saving off; a raise while it starts frees the draft ----


def _no_refresh(monkeypatch):
    from mef3io_studio.model.viewer_source import ViewerData

    monkeypatch.setattr(ViewerData, "refresh_notes", lambda self, appended, *rest: False)


def test_a_failed_reopen_after_a_notes_only_save_keeps_saving_off(ed, qtbot, monkeypatch):
    from mef3io_studio.model.viewer_source import ViewerData

    win, spy, (a, b) = ed
    turn_on(win)
    _no_refresh(monkeypatch)

    def broken(self):
        raise OSError("gone")

    monkeypatch.setattr(ViewerData, "reopen", broken)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "stored anyway")
    finish_save(qtbot, win)
    assert "cannot be shown" in spy.problems[-1].title
    assert win.edit.stale and not win.edit.saving
    # the notes on screen are the old ones: the same note added again must not be stored twice
    win.edit.add(T0 + 30 * SEC, None, "Spike", "stored anyway")
    assert win.edit.draft.dirty and not win.edit.save_button.isEnabled()
    assert win.edit.save() is False
    assert "Saving is off" in win.edit.count_label.text()


def test_a_raise_while_the_reopen_starts_frees_the_draft(ed, qtbot, monkeypatch):
    win, spy, _ = ed
    turn_on(win)
    _no_refresh(monkeypatch)

    def refuses():
        raise RuntimeError("no")

    monkeypatch.setattr(win, "begin_data_change", refuses)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "stored")
    finish_save(qtbot, win)
    assert not win.edit.saving and win.edit.stale
    assert "cannot be shown" in spy.problems[-1].title


def test_a_raise_while_looking_again_frees_the_draft(ed, qtbot, monkeypatch):
    win, spy, _ = ed
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "maybe stored")
    win.data.read_facts()
    from mef3io_studio.viewer import edit_mode

    real = edit_mode.asave.write_plan

    def write_then_lose_the_report(*args, **kw):
        real(*args, **kw)
        raise RuntimeError("the report was lost")

    monkeypatch.setattr(edit_mode.asave, "write_plan", write_then_lose_the_report)

    def refuses():
        raise RuntimeError("no")

    monkeypatch.setattr(win, "begin_data_change", refuses)
    win.edit.save()
    qtbot.waitUntil(lambda: bool(spy.problems), timeout=30_000)
    qtbot.waitUntil(lambda: not win.edit.saving, timeout=30_000)
    assert win.edit.stale and not win.edit.save_button.isEnabled()
    assert "open the recordings again" in spy.problems[0].text


# --- 8g. Esc on the field N opened from the normal view leaves edit mode as it was ----


def test_esc_after_n_from_the_normal_view_turns_edit_mode_off_again(ed):
    win, spy, _ = ed
    assert not win.edit.on
    at(win, T0 + 12 * SEC)
    key(win, Qt.Key.Key_N)
    assert win.edit.on and win.edit.inline.is_open
    press(win, Qt.Key.Key_Escape)
    assert not win.edit.on and not win.edit.draft.dirty and not win.edit_button.isChecked()
    assert not spy.asked  # nothing to save or discard: no question
    # a note that was kept keeps the mode on
    key(win, Qt.Key.Key_N)
    type_in(win, "kept")
    press(win, Qt.Key.Key_Return)
    assert win.edit.on and win.edit.draft.dirty
    # and a mode the person turned on is theirs: Esc leaves it on
    win.edit.draft.clear()
    win.edit.refresh()
    key(win, Qt.Key.Key_N)
    press(win, Qt.Key.Key_Escape)
    assert win.edit.on


def test_esc_after_n_keeps_the_mode_the_person_chose(ed):
    win, spy, _ = ed
    turn_on(win)
    at(win, T0 + 12 * SEC)
    key(win, Qt.Key.Key_N)
    press(win, Qt.Key.Key_Escape)
    assert win.edit.on
