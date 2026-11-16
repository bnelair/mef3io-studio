"""A page that loads never blanks the screen: each row keeps the picture it had,
drawn at the times it holds, until its new block arrives (docs/design/viewer.md,
"Drawing")."""

# ruff: noqa: F811
from __future__ import annotations

import threading

import numpy as np
from test_viewer_ui import SEC, T0, make_window, settle, write  # noqa: F401

from mef3io_studio.model import dsp

NAMES = [f"E{i}" for i in range(6)]


def slow(win, monkeypatch):
    """Hold every read until released, and forget what is cached."""
    gate = threading.Event()
    real = win.data.read_tile

    def held(*a, **k):
        gate.wait(20)
        return real(*a, **k)

    monkeypatch.setattr(win.data, "read_tile", held)
    ctl = win.controller
    ctl.cache.clear()
    ctl._held.clear()
    ctl._held_bytes = 0
    return gate


def ink_columns(win, rows=None) -> np.ndarray:
    """Per pixel column of the plot: does any trace pixel sit there? (Grid and
    gaps are the same in every picture of one window, so differences are ink.)"""
    img = win.canvas.grab_traces().toImage().convertToFormat(img_format())
    w, h = img.width(), img.height()
    px = np.frombuffer(img.constBits(), dtype=np.uint8).reshape(h, img.bytesPerLine() // 4, 4)
    return px[:, :w, :3].astype(int)


def img_format():
    from PySide6.QtGui import QImage

    return QImage.Format.Format_RGB32


def trace_mask(win) -> np.ndarray:
    """Pixels nearer the trace colour than the page background."""
    from PySide6.QtGui import QColor

    px = ink_columns(win)
    pal = win.canvas.palette_
    trace = np.array(QColor(pal.trace).getRgb()[:3][::-1])  # BGR in RGB32 little endian
    bg = np.array(QColor(pal.background).getRgb()[:3][::-1])
    near = np.linalg.norm(px - trace, axis=2) < np.linalg.norm(px - bg, axis=2) * 0.5
    plot = win.canvas.plot_rect()  # not the gutter's names, not the axis' times
    near[int(plot.bottom()) :, :] = False
    near[:, : int(plot.left())] = False
    return near


def _ink(win) -> np.ndarray:
    """How much each plot pixel differs from the same page with no row pictures."""
    import dataclasses

    rows = win.canvas.rows
    win.canvas.set_rows(
        [dataclasses.replace(r, piece=None, stale=None, failed=False) for r in rows],
        win.canvas.overview,
        win.canvas.multi,
    )
    base = ink_columns(win)
    win.canvas.set_rows(rows, win.canvas.overview, win.canvas.multi)
    diff = np.abs(ink_columns(win) - base).sum(axis=2)
    plot = win.canvas.plot_rect()
    diff[int(plot.bottom()) :, :] = 0
    diff[:, : int(plot.left())] = 0
    return diff


def faint_mask(win, least: int = 30) -> np.ndarray:
    """Every pixel the rows' pictures changed (old pictures drawn paler too)."""
    return _ink(win) >= least


def strength(win) -> float:
    """How strongly the rows' traces differ from the empty page, on average."""
    diff = _ink(win)
    return float(diff[diff > 0].mean()) if (diff > 0).any() else 0.0


def plot_columns(win) -> tuple[int, int]:
    plot = win.canvas.plot_rect()
    return int(plot.left()) + 2, int(plot.right()) - 2


def test_a_page_forward_keeps_the_old_traces_where_the_pages_overlap(
    make_window, tmp_path, qtbot, monkeypatch
):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=120))
    win.set_state(t0=T0 + 20 * SEC)
    settle(qtbot, win)
    page = win.page_us()
    gate = slow(win, monkeypatch)
    win.page(0.5)  # half a page: the left half of the new page is the old right half
    assert win.controller.loading and all(r.piece is None for r in win.canvas.rows)
    assert all(r.stale is not None for r in win.canvas.rows)
    mask = faint_mask(win)  # the old picture is drawn paler: not "trace coloured"
    lo, hi = plot_columns(win)
    mid = (lo + hi) // 2
    inked = mask.any(axis=0)
    assert inked[lo + 10 : mid - 10].mean() > 0.95, "the overlapping half keeps its traces"
    stale_picture = mask.copy()
    assert page > 0
    # the picture shown is the one the complete page shows there
    gate.set()
    settle(qtbot, win)
    assert all(r.piece is not None and r.stale is None for r in win.canvas.rows)
    final = trace_mask(win)
    left = slice(lo + 10, mid - 10)

    def agreement(a, shift=0):
        a = np.roll(a, shift, axis=1)
        return (a[:, left] & final[:, left]).sum() / max(1, final[:, left].sum())

    # the old traces sit where the new page has them (a shift of 40 px would not)
    assert agreement(stale_picture) > 0.6
    assert agreement(stale_picture, 40) < agreement(stale_picture) - 0.25
    assert win.canvas.spinner_shown is False


def test_a_far_jump_shows_none_of_the_old_page_and_the_spinner_comes_late(
    make_window, tmp_path, qtbot, monkeypatch
):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=240))
    win.set_state(t0=T0 + 10 * SEC)
    settle(qtbot, win)
    gate = slow(win, monkeypatch)
    win.go_to(T0 + 200 * SEC)
    assert win.controller.loading
    assert all(r.piece is None for r in win.canvas.rows)
    win.canvas.grab_traces()
    assert not trace_mask(win).any(), "no old trace under the new time axis"
    assert all(v == 0 for v in win.canvas.drawn_points.values())
    assert not win.canvas.spinner_shown  # a quick page never flashes it
    qtbot.waitUntil(lambda: win.canvas.spinner_shown, timeout=3000)
    gate.set()
    settle(qtbot, win)
    assert not win.canvas.spinner_shown
    assert trace_mask(win).any(axis=0)[plot_columns(win)[0] + 5]


def test_rows_of_another_channel_or_filter_keep_nothing(make_window, tmp_path, qtbot, monkeypatch):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=120))
    win.set_state(t0=T0 + 20 * SEC)
    settle(qtbot, win)
    gate = slow(win, monkeypatch)
    win.set_state(filters=dsp.FilterSettings(0.5, 40.0))  # same page, other values
    assert all(r.piece is None and r.stale is None for r in win.canvas.rows)
    gate.set()
    settle(qtbot, win)
    win.set_state(hidden=frozenset({"E1"}))
    settle(qtbot, win)
    assert "E1" not in [r.channel for r in win.canvas.rows]


def test_an_old_picture_is_drawn_paler_from_the_first_frame(
    make_window, tmp_path, qtbot, monkeypatch
):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=120))
    win.set_state(t0=T0 + 20 * SEC)
    settle(qtbot, win)
    current = strength(win)
    gate = slow(win, monkeypatch)
    win.page(0.5)
    assert all(r.piece is None and r.stale is not None for r in win.canvas.rows)
    assert not win.canvas.spinner_shown  # the first frame, long before the spinner
    old = strength(win)
    assert 0 < old < 0.6 * current, (old, current)  # there, and clearly not the real thing
    gate.set()
    settle(qtbot, win)
    assert strength(win) > 0.9 * current


def test_a_new_zoom_keeps_no_old_picture(make_window, tmp_path, qtbot, monkeypatch):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=120))
    win.set_state(t0=T0 + 20 * SEC)
    settle(qtbot, win)
    gate = slow(win, monkeypatch)
    win.set_state(timebase=win.state.timebase * 2)  # the old bins would be drawn blocky
    assert all(r.piece is None and r.stale is None for r in win.canvas.rows)
    assert not faint_mask(win).any()
    gate.set()
    settle(qtbot, win)
    assert all(r.piece is not None for r in win.canvas.rows)


def test_a_row_that_could_not_be_read_says_so_and_shows_no_old_trace(
    make_window, tmp_path, qtbot, monkeypatch
):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=120))
    win.set_state(t0=T0 + 20 * SEC)
    settle(qtbot, win)

    def broken(*a, **k):
        raise OSError("the drive went away")

    ctl = win.controller
    ctl.cache.clear()
    ctl._held.clear()
    ctl._held_bytes = 0
    monkeypatch.setattr(win.data, "read_tile", broken)
    win.page(0.5)  # half of the old picture would still fit
    qtbot.waitUntil(lambda: all(r.failed for r in win.canvas.rows), timeout=10_000)
    assert all(r.piece is None and r.stale is None for r in win.canvas.rows)
    win.canvas.grab_traces()  # drawn now, whatever the last paint was
    assert all(v == 0 for v in win.canvas.drawn_points.values())
    assert "could not be read" in win.status_message.text()
