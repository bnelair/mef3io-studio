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

# --- 1. N after paging by keyboard ------------------------------------------------------------


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


# --- 2. the lead rule on a 10-10 cap ----------------------------------------------------------

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


# --- 3. hidden details: no typed text offered ----------------------------------------------------


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


# --- 5. the held-centre estimate cannot lock to a period ---------------------------------------


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


# --- 6. the cost of a save: no inspect after the write ---------------------------------------


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
    assert before and not any(k.endswith((".rdat", ".ridx")) for k in before)
    assert any(k.endswith(".tdat") for k in before)
    assert asave.signal_snapshot(str(path)) == before
    tdat = next(path.rglob("*.tdat"))
    st = tdat.stat()
    os.utime(tdat, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert asave.signal_snapshot(str(path)) != before


# --- 7. a warm page turn does not ask the same thing of every row ----------------------------------


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
