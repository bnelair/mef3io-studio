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
