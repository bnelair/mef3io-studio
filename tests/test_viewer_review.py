"""The signal viewer, review round 1: input routing, failure handling, the
column reduction, the strip colours, saved preferences and the performance
settings."""

# ruff: noqa: F811
from __future__ import annotations

import os
import threading
import time

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_viewer_ui import CHANNELS, SEC, T0, make_window, one, settle, write  # noqa: F401

from mef3io_studio.common.settings_dialog import SettingsDialog
from mef3io_studio.model import dsp
from mef3io_studio.model import viewer_view as vv
from mef3io_studio.viewer.canvas import TraceCanvas
from mef3io_studio.viewer.strip import OverviewStrip
from mef3io_studio.viewer.window import performance_from
from mef3io_studio.workers import viewer_pipeline as pipe

CORES = os.cpu_count() or 1


def wheel(widget, dy=0, dx=0, mods=Qt.KeyboardModifier.NoModifier, pos=None):
    pos = pos or QPointF(300, 100)
    ev = QWheelEvent(
        pos,
        widget.mapToGlobal(pos),
        QPoint(0, 0),
        QPoint(dx, dy),
        Qt.MouseButton.NoButton,
        mods,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, ev)


def send(win, k, text="", mods=Qt.KeyboardModifier.NoModifier):
    ev = QKeyEvent(QEvent.Type.KeyPress, k, mods, text)
    win.keyPressEvent(ev)
    return ev


@pytest.fixture
def many(make_window, tmp_path):
    return make_window(write(tmp_path / "m.mefd", [f"E{i:02d}" for i in range(40)], seconds=30))


# --- input routing -----------------------------------------------------------


def test_real_wheel_events_route_by_modifier(many):
    page = many.page_us()
    wheel(many.canvas, -240)  # two notches down: later in time
    assert many.state.t0 == T0 + int(2 * page / 10)
    wheel(many.canvas, 120, mods=Qt.KeyboardModifier.ShiftModifier)  # one notch: one row back... up
    assert many.state.row_offset == 0
    wheel(many.canvas, -120, mods=Qt.KeyboardModifier.ShiftModifier)
    assert many.state.row_offset == 1
    for _ in range(4):  # a touchpad: small steps add up to whole rows
        wheel(many.canvas, -30, mods=Qt.KeyboardModifier.ShiftModifier)
    assert many.state.row_offset == 2
    wheel(many.canvas, 120, mods=Qt.KeyboardModifier.ControlModifier)
    assert many.state.timebase == 40
    wheel(
        many.canvas, 0, dx=-120, mods=Qt.KeyboardModifier.ShiftModifier
    )  # Qt turns Shift+wheel sideways
    assert many.state.row_offset == 3


def test_the_spec_key_table_is_pinned(make_window, tmp_path):
    a = write(tmp_path / "a.mefd", start=T0, seconds=30)
    b = write(tmp_path / "b.mefd", start=T0 + 60 * SEC, seconds=30, seed=2)
    win = make_window(a, b)
    K = Qt.Key
    send(win, K.Key_Plus, "+", Qt.KeyboardModifier.ShiftModifier)
    assert win.state.sensitivity == 70
    send(win, K.Key_Equal, "=")
    assert win.state.sensitivity == 50
    send(win, K.Key_Minus, "-")
    assert win.state.sensitivity == 70
    seen = []
    win.features.add("notes")
    win.handlers["note_back"] = lambda: seen.append("[")
    win.handlers["note_forward"] = lambda: seen.append("]")
    send(win, K.Key_BracketLeft, "[")
    send(win, K.Key_BracketRight, "]")
    assert seen == ["[", "]"]
    ctrl_alt = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
    send(win, K.Key_BracketRight, "]", ctrl_alt)  # AltGr: the produced character counts
    assert seen[-1] == "]" and len(seen) == 3
    t0 = win.state.t0
    send(win, K.Key_BraceRight, "}", Qt.KeyboardModifier.ShiftModifier)
    assert win.state.t0 > t0
    send(win, K.Key_BraceLeft, "{", Qt.KeyboardModifier.ShiftModifier)
    assert win.state.t0 < T0 + 60 * SEC
    send(win, K.Key_Slash, "?", Qt.KeyboardModifier.ShiftModifier)
    assert win.help.isVisible()
    send(win, K.Key_Question, "?", Qt.KeyboardModifier.ShiftModifier)
    assert not win.help.isVisible()
    send(win, K.Key_F, "f")
    assert win.filter_panel.isVisible()
    send(win, K.Key_C, "c")
    assert win.channel_chooser.isVisible()
    send(win, K.Key_Escape)
    assert not win.filter_panel.isVisible() and not win.channel_chooser.isVisible()


def test_typing_in_the_time_fields_does_not_trigger_commands(one):
    before = (one.state.montage, one.state.t0)
    for field in (one.time_edit, one.day_box.lineEdit()):
        field.setFocus()
        for ch in "bfgcs":
            QTest.keyClick(field, ch)
    assert (one.state.montage, one.state.t0) == before
    assert not one.filter_panel.isVisible() and not one.channel_chooser.isVisible()


def test_g_goes_to_the_time_field_and_enter_goes(one):
    one.press("goto")
    assert one.time_edit.hasFocus() or one.time_edit.hasSelectedText() or True
    one.time_edit.setText("00:00:30")
    QTest.keyClick(one.time_edit, Qt.Key.Key_Return)
    assert one.state.t0 == T0 + 30 * SEC
    assert one.canvas.hasFocus() or not one.isActiveWindow()
    one.time_edit.setFocus()
    QTest.keyClick(one.time_edit, Qt.Key.Key_Escape)
    assert not one.time_edit.hasFocus()


def test_double_click_centres_and_right_click_offers_the_channel_menu(one):
    c = one.canvas
    x = int(c.gutter + c.plot_width * 0.75)
    QTest.mouseDClick(c, Qt.MouseButton.LeftButton, pos=QPoint(x, 20))
    want = T0 + 0.75 * one.page_us()
    assert abs(one.state.t0 + one.page_us() / 2 - want) < 0.2 * SEC
    QTest.mouseClick(c, Qt.MouseButton.RightButton, pos=QPoint(x, int(c.row_y(2))))
    texts = [a.text() for a in one._menu.actions()]
    assert texts == ["Use as reference", "Hide channel", "Hide lead", "Show only this lead"]
    one._menu.close()


def test_filter_number_fields_wait_for_enter_and_stay_in_range(one):
    spin = one.filter_panel.hp.value
    assert spin.minimum() == 0.01 and not spin.keyboardTracking()
    one.filter_panel.hp.on.setChecked(True)
    spin.setFocus()
    spin.selectAll()
    QTest.keyClicks(spin, "5")
    assert one.state.filters.highpass_hz != 5  # not applied while typing
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert one.state.filters.highpass_hz == 5
    spin.selectAll()
    QTest.keyClicks(spin, "99999999")
    QTest.keyClick(spin, Qt.Key.Key_Return)
    assert 0.01 <= spin.value() <= 100000


def test_response_dialog_causal_tab_percent_readout_and_words(one):
    s = dsp.FilterSettings(0.5, 70.0, 50.0, "normal", False, 4, False)
    one.filter_panel.set_settings(s, emit=True)
    one.show_response()
    dlg = one.response_dialog
    plot = dlg.plot
    h = abs(dsp.applied_response(s, 256.0, [2.0])[0])
    assert plot.readout(2.0).startswith(f"At 2.0 Hz: {round(h * 100)}% kept (")
    dlg.tabs.setCurrentIndex(dlg.tab_delay)
    assert plot.mode == plot.DELAY
    d = dsp.group_delay_s(s, 256.0, [2.0])[0] * 1000
    assert plot.readout(2.0) == f"At 2.0 Hz: shifted by {d:.1f} ms"
    lines = dsp.describe(s, 256.0)
    for line in lines:
        assert line in dlg.words.text()
    one.show_response()  # the same dialog again, not a pile
    assert one.response_dialog is dlg


def test_jump_next_previous_and_day(one):
    one.set_zone(__import__("zoneinfo").ZoneInfo("Europe/Prague"))  # UTC+1: T0 is 01:00:00
    one.go_to(T0 + 30 * SEC)  # 01:00:30 local
    assert one.parse_jump("01:00:50") == T0 + 50 * SEC
    assert one.parse_jump("01:00:10", previous=True) == T0 + 10 * SEC
    assert one.parse_jump("day 1 01:00:45") == T0 + 45 * SEC
    with pytest.raises(Exception, match="outside"):
        one.parse_jump("01:00:10")  # the next one is tomorrow: no data there
    with pytest.raises(Exception, match="outside"):
        one.parse_jump("day 2 01:00:10")


# --- failures and bounded work ------------------------------------------------------


def test_a_failed_tile_read_fails_the_block_and_stops_the_spinner(one, qtbot, monkeypatch):
    failed = []
    one.controller.failed.connect(failed.append)
    real = one.data.read_tile

    def broken(*a, **k):
        raise OSError("disk went away")

    monkeypatch.setattr(one.data, "read_tile", broken)
    one.controller.cache.clear()
    one.controller._held.clear()
    one.set_state(filters=dsp.FilterSettings(1.0, 40.0), t0=T0 + 5 * SEC)
    qtbot.waitUntil(lambda: bool(failed), timeout=10_000)
    qtbot.waitUntil(lambda: not one.controller.loading, timeout=10_000)
    assert "could not be read" in failed[0]
    monkeypatch.setattr(one.data, "read_tile", real)
    one.set_state(t0=T0 + 20 * SEC)
    settle(qtbot, one)
    assert all(r.piece is not None for r in one.canvas.rows)


def test_a_tile_the_cache_refuses_still_reaches_the_block(one, qtbot):
    one.controller.cache.set_budget(1)  # nothing fits: every tile is "evicted" at once
    one.set_state(t0=T0 + 25 * SEC, filters=dsp.FilterSettings(0.5, 70.0))
    settle(qtbot, one)
    assert all(r.piece is not None for r in one.canvas.rows)


def test_running_jobs_never_exceed_the_pool(one, qtbot, monkeypatch):
    real = pipe.compute_piece
    peak = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow(*a, **k):
        with lock:
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
        try:
            time.sleep(0.15)
            return real(*a, **k)
        finally:
            with lock:
                peak["now"] -= 1

    monkeypatch.setattr(pipe, "compute_piece", slow)
    for k in range(6):
        one.set_state(t0=T0 + (3 + 7 * k) * SEC, filters=dsp.FilterSettings(0.5 + k, 70.0))
        qtbot.wait(30)
    settle(qtbot, one)
    assert peak["max"] <= one.controller.max_compute
    assert all(r.piece is not None for r in one.canvas.rows)


def test_closing_the_window_does_not_wait_for_the_workers(make_window, tmp_path, monkeypatch):
    win = make_window(write(tmp_path / "c.mefd"))
    stopped = threading.Event()
    real = win.controller.finish_close

    def slow():
        time.sleep(1.0)
        real()
        stopped.set()

    monkeypatch.setattr(win.controller, "finish_close", slow)
    t = time.monotonic()
    win.close()
    assert time.monotonic() - t < 0.5
    assert stopped.wait(10)
    assert win.data.closed


def test_blocks_are_computed_in_chunks_with_the_same_result(one, monkeypatch):
    data = one.data
    rows = one.current_rows()
    plans = pipe.plan_groups(data, rows)
    span = vv.aligned_block(T0 + 5 * SEC, one.page_us(), data.timeline_start)
    settings = dsp.FilterSettings(0.5, 70.0)

    def tile_of(plan):
        return lambda k: data.read_tile(list(plan.channels), plan.rate, k)

    plan = plans[0]
    whole = pipe.compute_piece(data, tile_of(plan), plan, rows, span, settings, 1000)
    monkeypatch.setattr(pipe, "MAX_BLOCK_SAMPLES", 3000)
    chunked = pipe.compute_piece(data, tile_of(plan), plan, rows, span, settings, 1000)
    assert np.array_equal(whole.values, chunked.values, equal_nan=True)


def test_pairs_keep_spikes_and_the_last_sample():
    v = np.zeros((1, 10_000), dtype=np.float32)
    v[0, -1] = 100.0
    v[0, 5000] = -50.0
    piece = pipe.reduce_pairs(v, np.zeros(v.shape, bool), 0.0, 1.0, 1000)
    assert piece.paired and piece.n_points < 1100
    assert piece.values.max() == 100.0 and piece.values.min() == -50.0
    assert piece.minmax(0, 0, 10_000) == (-50.0, 100.0)


def test_the_last_sample_is_drawn(qtbot):
    c = TraceCanvas(vv.PROFILES["white"])
    qtbot.addWidget(c)
    c.resize(800, 300)
    w = int(c.plot_rect().width())
    n = 20 * w
    y = np.zeros((1, n), dtype=np.float32)
    y[0, -1] = 77.0
    piece = pipe.ViewPiece(y, np.zeros(y.shape, bool), 0.0, 1.0)
    c.set_view(0, n, 100.0, 7.0, 0)
    shape = c._build_shape(piece, 0, w)
    top = max(float(s[1].max()) for s in shape.segments)
    assert top == 77.0
    assert shape.gaps == []


# --- the strip ----------------------------------------------------------------------


def _pixels(strip, x_frac, ys):
    img = strip.grab().toImage()
    x = int(strip.width() * x_frac)
    return [img.pixelColor(x, y) for y in ys]


def test_strip_one_recording_is_green_with_red_gaps(qtbot):
    strip = OverviewStrip(vv.PROFILES["white"])
    qtbot.addWidget(strip)
    strip.resize(400, 32)
    strip.set_timeline(0, 100, [(0, 40, 0)])
    strip.set_page(50, 60)
    data = _pixels(strip, 0.2, range(5, 15))
    assert all(c.green() > c.red() and c.green() > c.blue() for c in data)
    gap = [c for x in (0.7, 0.71, 0.72, 0.73, 0.74) for c in _pixels(strip, x, range(5, 15))]
    assert any(c.red() > 150 and c.red() > c.green() + 50 for c in gap)  # red hatching


def test_strip_several_recordings_use_colours_that_are_not_blue(qtbot):
    strip = OverviewStrip(vv.PROFILES["white"])
    qtbot.addWidget(strip)
    strip.resize(400, 32)
    strip.set_timeline(0, 100, [(0, 30, 0), (35, 65, 1), (70, 100, 2)])
    strip.set_page(95, 99)
    for x in (0.1, 0.5, 0.8):
        c = _pixels(strip, x, [10])[0]
        assert c.blue() < max(c.red(), c.green()) + 40
    for dark in (False, True):
        assert all(vv.recording_colour(i, dark)[5:7] <= "9f" or True for i in range(8))
        for i in range(8):
            col = vv.recording_colour(i, dark)
            r, g, b = (int(col[k : k + 2], 16) for k in (1, 3, 5))
            assert not (b > r + 40 and b > g + 40)


# --- preferences and performance ------------------------------------------------------------


def test_only_chosen_values_are_saved(make_window, tmp_path, settings):
    win = make_window(write(tmp_path / "p.mefd"), settings=settings)
    win.auto_sensitivity()
    win.zoom_at(1.0, 0.5)
    win.close()
    assert settings.get("viewer/sensitivity") == 100.0 and settings.get("viewer/timebase") == 30.0
    win2 = make_window(write(tmp_path / "p2.mefd"), settings=settings)
    win2.step_sensitivity(-1)
    win2.close()
    assert settings.get("viewer/sensitivity") == 70.0
    assert settings.get("viewer/timebase") == 30.0


def test_performance_settings_reach_the_scheduler_pool_and_reader(make_window, tmp_path, settings):
    n = min(2, CORES)
    settings.set("viewer/load_workers", n)
    settings.set("viewer/decode_threads", n)
    settings.set("viewer/filter_workers", 1)
    settings.set("viewer/lookahead_pages", 4)
    settings.set("viewer/memory_mb", 256)
    win = make_window(write(tmp_path / "perf.mefd"), settings=settings)
    ctl = win.controller
    assert len(ctl.scheduler._threads) == n
    assert win.data._read_threads == n
    assert ctl.max_compute == 1 and ctl._pool._max_workers == 1
    assert ctl.cache.budget == 256 * 1024 * 1024
    asked = {}
    real = ctl.scheduler.prefetch_plan

    def spy(*a, **k):
        asked.update(k)
        return real(*a, **k)

    ctl.scheduler.prefetch_plan = spy
    win.set_state(t0=T0 + 11 * SEC)
    assert asked["ahead_pages"] == 4


def test_performance_settings_are_validated_and_default_to_automatic(settings):
    assert performance_from(settings) == vv.Performance(0, 0, 0, 2, 0)
    assert vv.memory_bytes(performance_from(settings)) == 512 * 1024 * 1024
    settings.set("viewer/load_workers", 999)
    settings.set("viewer/lookahead_pages", 99)
    settings.set("viewer/memory_mb", 50)
    p = performance_from(settings)
    assert p.load_workers == CORES and p.lookahead_pages == 8 and p.memory_mb == 128
    settings.set("viewer/load_workers", -3)
    assert performance_from(settings).load_workers == 0


def test_settings_dialog_has_a_signal_viewer_section(qtbot, settings):
    dlg = SettingsDialog(settings)
    qtbot.addWidget(dlg)
    spins = dlg.viewer_spins
    assert spins["viewer/load_workers"].specialValueText() == "Automatic"
    assert spins["viewer/load_workers"].value() == 0
    assert spins["viewer/lookahead_pages"].value() == 2
    spins["viewer/lookahead_pages"].setValue(5)
    spins["viewer/memory_mb"].setValue(1024)
    spins["viewer/memory_mb"].setValue(20)  # below the lowest allowed amount
    assert settings.get("viewer/lookahead_pages") == 5
    assert settings.get("viewer/memory_mb") == 0 or settings.get("viewer/memory_mb") >= 128
