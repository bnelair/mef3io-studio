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
