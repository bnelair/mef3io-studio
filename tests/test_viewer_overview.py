# ruff: noqa: F811
"""The overview band above the strip (docs/user-guide.md, "The overview"):
the whole recording's highest and lowest values from the block index, in the
background, for the chosen channels. Synthetic sessions in tmp_path only.

Standing rule (network drives): the band reuses the viewer's open reader, and
reads each channel's index (.tidx) at most once per request and never again
once cached."""

from __future__ import annotations

import sys
import threading

import mef3io
import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from test_viewer_ui import SEC, T0, make_window, settle, write  # noqa: F401

from mef3io_studio.viewer.overview import (
    MAX_LANES,
    OVERVIEW_BINS,
    reduce_columns,
    summarise,
)

_opens: list[str] = []
_counting = threading.Event()


def _hook(event, args):
    if event == "open" and _counting.is_set():
        _opens.append(str(args[0]))


sys.addaudithook(_hook)


def wait_overview(qtbot, win, channels):
    qtbot.waitUntil(
        lambda: (
            all(c in win.controller.overview for c in channels) or win.controller.overview_failed
        ),
        timeout=30_000,
    )


@pytest.fixture
def one(make_window, tmp_path):
    return make_window(write(tmp_path / "one.mefd"))


# --- pure numpy ---


def test_summarise_keeps_gaps_and_own_range():
    t = 1_000_000
    e = {
        "start_uutc": np.array([0, 10 * t, 30 * t]),
        "end_uutc": np.array([10 * t, 20 * t, 40 * t]),
        "min": np.array([-50.0, -80.0, np.nan]),
        "max": np.array([60.0, 90.0, np.nan]),
    }
    mn, mx, scale = summarise(e, (0, 40 * t), bins=40)
    assert np.isfinite(mn[:20]).all() and np.isnan(mn[20:30]).all()  # the gap stays empty
    assert np.isnan(mn[30:]).all()  # unset extremes are not data
    assert 60 <= scale <= 90


def test_reduce_columns_takes_min_of_mins_and_max_of_maxes():
    mn = np.array([5.0, -3.0, np.nan, 2.0])
    mx = np.array([6.0, 4.0, np.nan, 9.0])
    lo, hi = reduce_columns(mn, mx, 2)
    assert list(lo) == [-3.0, 2.0] and list(hi) == [6.0, 9.0]
    lo, hi = reduce_columns(mn, mx, 8)  # wider than the data: columns repeat
    assert len(lo) == 8 and np.isnan(lo[4:6]).all()


# --- the window ---


def test_default_is_first_shown_channel_and_loads_in_background(one, qtbot):
    band = one.overview_band
    assert band.isVisible()
    assert one.overview_channels() == [one._channels[0]]
    wait_overview(qtbot, one, one.overview_channels())
    qtbot.waitUntil(lambda: one._channels[0] in band.data)
    mn, mx, _ = band.data[one._channels[0]]
    assert len(mn) == OVERVIEW_BINS and np.isfinite(mn).any()
    band.repaint()


def test_click_on_band_navigates_like_the_strip(one, qtbot):
    band = one.overview_band
    QTest.mouseClick(band, Qt.MouseButton.LeftButton, pos=QPoint(band.width() // 2, 8))
    mid = (one.data.timeline_start + one.data.timeline_end) / 2
    span = one.data.timeline_end - one.data.timeline_start
    assert abs(one.state.t0 + one.page_us() / 2 - mid) < 0.02 * span
    assert type(band).navigate is type(one.strip).navigate  # one shared implementation


def test_pick_channels_hidden_leaves_and_cache_is_instant(one, qtbot):
    a, b = one._channels[:2]
    one.pick_overview_channel(b, True)
    assert one.overview_channels() == [a, b]
    assert one.overview_band.height() > 22
    wait_overview(qtbot, one, [a, b])
    one.hide_channel(a)  # hidden channels leave the band
    assert one.overview_channels() == [b]
    one.pick_overview_channel(b, False)
    assert one.overview_channels() == [one._channels[1]]  # the default: first one still shown
    # back to a: cached, shown at once, nothing asked again
    one.set_hidden(frozenset())
    one.overview_pick = [a]
    asked = []
    real = one.controller._strip_pool.submit
    one.controller._strip_pool.submit = lambda *x, **k: asked.append(x) or real(*x, **k)
    one._sync_overview()
    assert not asked and a in one.overview_band.data


def test_no_more_than_max_lanes(one):
    for ch in one._channels[:6]:
        one.pick_overview_channel(ch, True)
    assert len(one.overview_channels()) <= MAX_LANES


def test_can_be_turned_off(one):
    one.set_overview_on(False)
    assert not one.overview_band.isVisible()
    one.set_overview_on(True)
    assert one.overview_band.isVisible()


def test_gaps_stay_gaps(make_window, tmp_path, qtbot):
    a = write(tmp_path / "a.mefd", start=T0, seconds=20)
    b = write(tmp_path / "b.mefd", start=T0 + 60 * SEC, seconds=20, seed=2)
    win = make_window(a, b)
    ch = win.overview_channels()[0]
    wait_overview(qtbot, win, [ch])
    mn, _, _ = win.controller.overview[ch]
    assert np.isnan(mn[OVERVIEW_BINS * 30 // 80 : OVERVIEW_BINS * 50 // 80]).all()
    assert np.isfinite(mn[: OVERVIEW_BINS * 15 // 80]).any()
    win.overview_band.grab()  # paints without error


# --- the standing rule: no file opened twice ---


def test_overview_adds_no_reopen_and_reads_each_index_once(
    make_window, tmp_path, qtbot, monkeypatch
):
    win = make_window(write(tmp_path / "one.mefd"))
    wait_overview(qtbot, win, win.overview_channels())
    settle(qtbot, win)
    made = []
    classes = (mef3io.Reader, mef3io.MultiReader)
    for name in ("Reader", "MultiReader"):
        real = getattr(mef3io, name)
        monkeypatch.setattr(
            mef3io, name, lambda *a, _r=real, _n=name, **k: made.append(_n) or _r(*a, **k)
        )
    calls = []
    for cls in classes:
        if hasattr(cls, "block_envelope"):
            real_env = cls.block_envelope
            monkeypatch.setattr(
                cls,
                "block_envelope",
                lambda self, ch, *a, _r=real_env, **k: calls.append(ch) or _r(self, ch, *a, **k),
            )
    # a fresh pair of channels, one request
    want = [c for c in win._channels if c not in win.controller.overview][:2]
    _opens.clear()
    _counting.set()
    try:
        for ch in want:
            win.pick_overview_channel(ch, True)
        wait_overview(qtbot, win, want)
        done = len(calls)
        win.pick_overview_channel(want[0], False)  # back and forth: cached
        win.pick_overview_channel(want[0], True)
        qtbot.wait(300)
        assert len(calls) == done  # nothing asked again for cached channels
    finally:
        _counting.clear()
    # the C++ side opens files unseen by the audit hook: count its calls too. The first channel
    # was asked alone (it was the only one picked then), the second alone: one call each, a list
    # of only the missing channels, never a channel twice.
    flat = [c for ch in calls for c in ([ch] if isinstance(ch, str) else ch)]
    assert sorted(flat) == sorted(set(flat)) == sorted(want)
    tidx = [p for p in _opens if p.endswith(".tidx")]
    assert len(tidx) == len(set(tidx)) <= len(want), tidx
    assert [p for p in _opens if p.endswith((".tdat", ".tmet", ".segd"))] == []
    assert made == []  # the viewer's own reader is reused
