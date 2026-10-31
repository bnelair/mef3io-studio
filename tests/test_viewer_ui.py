"""The signal viewer window, offscreen (docs/design/viewer.md): opening on one
and two recordings, keys and wheel, the scales, montages, the filter panel and
the response plot, the help overlay, measuring, the overview and the entry
from Explore. Synthetic sessions in tmp_path only; the GUI thread never calls
mef3io while the viewer runs."""

from __future__ import annotations

import threading

import mef3io
import numpy as np
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mef3io_studio.common.theme import DARK, LIGHT
from mef3io_studio.model import dsp
from mef3io_studio.model import viewer_view as vv
from mef3io_studio.model.viewer_source import open_recordings
from mef3io_studio.viewer import jump as jumping
from mef3io_studio.viewer import keymap
from mef3io_studio.viewer.keymap import KEYMAP
from mef3io_studio.viewer.window import ViewerWindow

T0 = 946_684_800_000_000
SEC = 1_000_000
CHANNELS = ["Fp1", "Fp2", "LA1", "LA2", "LA3", "LA4", "Cz", "Pz", "ECG"]


def write(path, channels=CHANNELS, start=T0, seconds=60.0, fs=256.0, seed=1, amp=40.0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * fs)) / fs
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        for k, name in enumerate(channels):
            x = amp * np.sin(2 * np.pi * (6 + k) * t) + 5 * rng.normal(size=t.size)
            w.write(name, np.round(x, 2), start, fs)
    return path


def open_data(*paths):
    data = open_recordings([str(p) for p in paths])
    for ch in data.channels:  # what the opening task reads once
        data.rate(ch), data.units(ch), data.ownership(ch)
    return data


def settle(qtbot, win, timeout=30_000):
    qtbot.waitUntil(
        lambda: win.controller.wait_idle(0.05) and not win.controller.loading, timeout=timeout
    )
    qtbot.wait(30)


@pytest.fixture
def make_window(qtbot, qapp):
    made = []

    def make(*paths, reveal=None, theme=LIGHT, **kw):
        win = ViewerWindow(open_data(*paths), theme=theme, reveal=reveal, **kw)
        qtbot.addWidget(win)
        win.resize(1200, 700)
        win.show()
        qtbot.waitUntil(lambda: win._ready, timeout=10_000)
        settle(qtbot, win)
        made.append(win)
        return win

    yield make
    for win in made:
        win.close()


def key(win, k, mods=Qt.KeyboardModifier.NoModifier, text=""):
    QTest.keyClick(win.canvas, k, mods)


@pytest.fixture
def one(make_window, tmp_path):
    return make_window(write(tmp_path / "one.mefd"))


# --- opening ----------


def test_opens_on_one_recording(one):
    assert one.row_labels() == sorted(
        CHANNELS,
        key=lambda n: (
            [int(p) if p.isdigit() else p.lower() for p in __import__("re").split(r"(\d+)", n)]
        ),
    )
    assert one.canvas.drawn_points and all(n > 0 for n in one.canvas.drawn_points.values())
    assert not one.multi and one.windowTitle() == "Signal viewer"
    assert one.status_mode.text() == "As recorded; No filters"


def test_opens_on_two_recordings_and_names_follow_privacy(make_window, tmp_path):
    a = write(tmp_path / "Alpha_case.mefd", start=T0)
    b = write(tmp_path / "Beta_case.mefd", start=T0 + 120 * SEC, seed=2)
    shown = {0: False, 1: False}
    win = make_window(a, b, reveal=lambda i: shown[i])
    assert win.multi
    assert "Alpha" not in win.windowTitle() and "Beta" not in win.windowTitle()
    assert win.windowTitle() == "Signal viewer: recordings A, B"
    assert win.recording_label(1) == "Recording B"
    assert {r.tag for r in win.canvas.rows} == {""}  # recordings in turn: no tags
    shown[0] = shown[1] = True
    win.refresh_privacy()
    assert "Alpha_case" in win.windowTitle()
    assert win.recording_label(1) == "Recording B: Beta_case"
    win.go_to(T0 + 130 * SEC)
    settle_page = win.canvas.rows
    assert {r.tag for r in settle_page} == {""}


# --- navigation ----------


def test_keys_move_by_the_documented_amounts(one, qtbot):
    page = one.page_us()
    assert 9 * SEC < page < 12 * SEC  # a physical page: plot width / timebase
    t0 = one.state.t0
    key(one, Qt.Key.Key_Right)
    assert one.state.t0 == t0 + page
    key(one, Qt.Key.Key_Left)
    assert one.state.t0 == t0
    key(one, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    assert one.state.t0 == t0 + page // 2
    key(one, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)
    assert one.state.t0 == t0 + page // 2 + SEC
    key(one, Qt.Key.Key_End)
    assert one.state.t0 == one.data.timeline_end - page
    key(one, Qt.Key.Key_Home)
    assert one.state.t0 == one.data.timeline_start
    settle(qtbot, one)


def test_wheel_scrolls_time_rows_and_zooms(make_window, tmp_path, qtbot):
    win = make_window(write(tmp_path / "many.mefd", [f"E{i:02d}" for i in range(40)], seconds=30))
    page = win.page_us()
    win.canvas.wheel_time.emit(-2.0)
    assert win.state.t0 == T0 + int(2 * page / 10)
    win.canvas.wheel_rows.emit(-1.0)
    assert win.state.row_offset == 1
    win.canvas.wheel_rows.emit(1.0)
    assert win.state.row_offset == 0
    centre = win.state.t0 + page // 2
    win.canvas.wheel_zoom.emit(1.0, 0.5)  # zoom in: more millimetres per second
    assert win.state.timebase == 40
    assert abs(win.state.t0 + win.page_us() / 2 - centre) < 2  # the pointer's time stays
    key(win, Qt.Key.Key_PageDown)
    assert win.state.row_offset == win.canvas.visible_rows()
    key(win, Qt.Key.Key_PageUp)
    assert win.state.row_offset == 0
    settle(qtbot, win)


def test_sensitivity_timebase_and_spacing_steps(one):
    assert (one.state.sensitivity, one.state.timebase, one.state.spacing) == (100, 30, 7)
    key(one, Qt.Key.Key_Up)  # larger signals = fewer µV per mm
    assert one.state.sensitivity == 70
    key(one, Qt.Key.Key_Down)
    key(one, Qt.Key.Key_Down)
    assert one.state.sensitivity == 150
    assert one.sens_box.currentData() == 150
    for k, want in (
        (Qt.Key.Key_1, 10),
        (Qt.Key.Key_2, 15),
        (Qt.Key.Key_3, 30),
        (Qt.Key.Key_4, 60),
        (Qt.Key.Key_5, 120),
    ):
        key(one, k, Qt.KeyboardModifier.AltModifier)
        assert one.state.timebase == want
    # Row spacing is Ctrl+Up/Down, Alt+Up/Down on macOS (keymap.MAC).
    mod = Qt.KeyboardModifier.AltModifier if keymap.MAC else Qt.KeyboardModifier.ControlModifier
    key(one, Qt.Key.Key_Up, mod)
    assert one.state.spacing == 8
    key(one, Qt.Key.Key_Down, mod)
    key(one, Qt.Key.Key_Down, mod)
    assert one.state.spacing == 6
    assert vv.SENS_STEPS[0] == 1 and vv.SENS_STEPS[-1] == 5000
    assert vv.TIMEBASE_CLINICAL[0] == 3 and vv.TIMEBASE_CLINICAL[-1] == 240
    assert vv.SPACING_STEPS[0] == 2 and vv.SPACING_STEPS[-1] == 30


def test_held_keys_repeat_navigation_only(one):
    def press(k, repeat):
        ev = QKeyEvent(QEvent.Type.KeyPress, k, Qt.KeyboardModifier.NoModifier, "", repeat, 1)
        one.keyPressEvent(ev)

    before = one.state.montage
    press(Qt.Key.Key_B, True)  # a held B must not cycle montages again and again
    assert one.state.montage == before
    press(Qt.Key.Key_B, False)
    assert one.state.montage != before
    t0 = one.state.t0
    press(Qt.Key.Key_Right, True)
    assert one.state.t0 > t0


def test_strip_click_and_double_click_centre_the_page(one, qtbot):
    strip = one.strip
    QTest.mouseClick(strip, Qt.MouseButton.LeftButton, pos=QPoint(strip.width() // 2, 8))
    mid = (one.data.timeline_start + one.data.timeline_end) / 2
    assert abs(one.state.t0 + one.page_us() / 2 - mid) < 0.02 * (
        one.data.timeline_end - one.data.timeline_start
    )
    one.canvas.centre_requested.emit(T0 + 20 * SEC)
    assert abs(one.state.t0 + one.page_us() / 2 - (T0 + 20 * SEC)) < 2


def test_boundary_keys(make_window, tmp_path):
    a = write(tmp_path / "a.mefd", start=T0, seconds=30)
    b = write(tmp_path / "b.mefd", start=T0 + 60 * SEC, seconds=30, seed=2)
    win = make_window(a, b)
    win.press("boundary_forward")
    centre = win.state.t0 + win.page_us() / 2
    assert abs(centre - (T0 + 60 * SEC)) < 2 or abs(centre - (T0 + 30 * SEC)) < 2


# --- montages ----------


def test_montage_switch_changes_the_row_labels(one, qtbot):
    one.use_as_reference("Cz")
    settle(qtbot, one)
    labels = one.row_labels()
    assert "LA3 − Cz" in labels and "Cz" not in labels
    assert one.status_mode.text().startswith("Referenced to Cz")
    one.montage_bar.set_state("bipolar", None)
    one.set_state(montage="bipolar", reference=None)
    settle(qtbot, one)
    assert "LA1 − LA2" in one.row_labels() and "LA3 − LA4" in one.row_labels()
    key(one, Qt.Key.Key_B)  # next: as recorded
    assert one.state.montage == "recorded"


def test_referenced_values_are_the_difference(one, qtbot):
    one.set_timebase(120)  # a short page: every sample is kept (no min/max reduction)
    one.use_as_reference("Cz")
    settle(qtbot, one)
    row = next(r for r in one.canvas.rows if r.label == "LA3 − Cz")
    ref = next(r for r in one.canvas.rows if r.label == "Pz − Cz")
    assert row.piece is not None and ref.piece is not None
    cz = one.data._combined.read(
        ["LA3", "Cz"],
        int(row.piece.t_first_us),
        int(row.piece.t_first_us) + 200_000,
        dtype="float32",
    )
    n = 40
    assert np.allclose(row.piece.values[row.local, :n], cz[0, :n] - cz[1, :n], atol=1e-3)


def test_channel_menu_actions_hide_and_show(one, qtbot):
    one.hide_channel("Pz")
    assert "Pz" not in one.row_labels()
    one.hide_lead("LA2")
    assert [n for n in one.row_labels() if n.startswith("LA")] == []
    one.show_only_lead("Fp1")
    assert one.row_labels() == ["Fp1", "Fp2"]
    chooser = one.channel_chooser
    chooser.show_all.click()
    assert len(one.row_labels()) == len(CHANNELS)
    chooser.search.setText("la")
    visible = [
        chooser.list.item(i).text()
        for i in range(chooser.list.count())
        if not chooser.list.item(i).isHidden()
    ]
    assert visible == ["LA1", "LA2", "LA3", "LA4"]


# --- filters ----------


def test_filter_panel_values_reach_the_filter_settings(one, qtbot):
    panel = one.filter_panel
    panel.hp.choose(0.5)
    panel.lp.choose(70)
    panel.mains.setCurrentText("60 Hz")
    panel.mains_width.setCurrentText("Wide")
    panel.harmonics.setChecked(True)
    panel.steepness_buttons["Sharp"].setChecked(True)
    panel.as_recorder.setChecked(True)
    want = dsp.FilterSettings(0.5, 70.0, 60.0, "wide", True, 8, False)
    assert panel.build() == want
    assert one.state.filters == want
    assert one.filters_button.text() == "0.5–70 Hz, 60 Hz notch"
    settle(qtbot, one)
    assert one.canvas.drawn_points
    # the slider and the number field agree
    panel.lp.slider.setValue(panel.lp.slider.maximum())
    assert panel.lp.value.value() <= dsp.NYQUIST_FRACTION * 128 + 1e-6
    assert panel.lp.f_max == dsp.NYQUIST_FRACTION * 128


def test_filter_panel_rejects_highpass_at_or_above_lowpass(one):
    panel = one.filter_panel
    panel.lp.choose(30)
    good = one.state.filters
    panel.hp.choose(40)
    assert panel.message.text() == "The high-pass cutoff must be below the low-pass cutoff."
    assert one.state.filters == good  # nothing was applied
    panel.hp.choose(1)
    assert panel.message.text() == ""
    assert one.state.filters.highpass_hz == 1


def test_response_dialog_matches_the_applied_response(one):
    s = dsp.FilterSettings(0.5, 70.0, 60.0, "normal", False, 4, True)
    one.filter_panel.set_settings(s, emit=True)
    one.show_response()
    dlg = one.response_dialog
    plot = dlg.plot
    assert plot.fs == 256.0 and not dlg.rate_box.isVisible()
    for f in (0.1, 2.0, 60.0):
        h = abs(dsp.applied_response(s, 256.0, [f])[0])
        text = plot.readout(f)
        assert text.startswith(f"At {f:.1f} Hz" if f >= 1 else f"At {f:g} Hz")
        assert f"{round(h * 100)}%" in text or "less than 0.1%" in text or f"{h * 100:.1f}%" in text
    assert plot.readout(2.0) == "At 2.0 Hz: 98% kept (−0.2 dB)" or "kept" in plot.readout(2.0)
    freqs, y = plot.curve()
    assert np.allclose(y, np.abs(dsp.applied_response(s, 256.0, freqs)) * 100)
    plot.mode = plot.DB
    assert plot.curve()[1].min() >= -120.0 and plot.curve()[1].max() <= 0.1
    assert any("60 Hz mains" in line for line in dsp.describe(s, 256.0))
    assert "60 Hz mains" in dlg.words.text()
    assert dlg.tabs.isTabVisible(dlg.tab_delay) is False  # zero-phase: no time shift
    causal = dsp.FilterSettings(0.5, 70.0, None, "normal", False, 4, False)
    dlg.refresh(causal)
    assert dlg.tabs.isTabVisible(dlg.tab_delay)
    dlg.tabs.setCurrentIndex(dlg.tab_delay)
    assert plot.curve()[1].shape[0] == 500
    pix = dlg.grab()
    assert not pix.isNull()


def test_response_dialog_has_a_rate_picker_when_rates_differ(make_window, tmp_path):
    p = tmp_path / "mixed.mefd"
    with mef3io.Writer(str(p), overwrite=True, units="uV") as w:
        w.write("slow", np.zeros(512), T0, 256.0)
        w.write("fast", np.zeros(4096), T0, 2048.0)
    win = make_window(p)
    win.filter_panel.set_settings(dsp.FilterSettings(0.5, 70.0), emit=True)
    win.show_response()
    dlg = win.response_dialog
    assert [dlg.rate_box.itemData(i) for i in range(dlg.rate_box.count())] == [256.0, 2048.0]
    assert dlg.rate_box.isVisibleTo(dlg)
    dlg.rate_box.setCurrentIndex(0)
    assert dlg.plot.fs == 256.0
    assert dlg.plot.f_max < 128


# --- help, jump, measure --------------------------------------------------------------


def test_help_overlay_lists_every_key_entry(one):
    one.toggle_help()
    assert one.help.isVisible()
    assert len(one.help.rows) == len(KEYMAP)
    texts = [t for _, t in one.help.rows]
    for entry in KEYMAP:
        assert any(t.startswith(entry.text) for t in texts), entry.action
    assert all(keys for keys, _ in one.help.rows)
    one.press("escape")
    assert not one.help.isVisible()


def test_jump_dialog_and_toolbar_go(make_window, tmp_path):
    win = make_window(write(tmp_path / "j.mefd", seconds=120))
    win.press("jump")
    win.jump_dialog.edit.setText("+40s")
    win.jump_dialog.go.click()
    assert win.state.t0 == T0 + 40 * SEC
    win.jump_dialog.edit.setText("nonsense")
    win.jump_dialog.go.click()
    assert win.jump_dialog.error.text().startswith("That could not be understood")
    win.day_box.setValue(1)
    win.time_edit.setText("00:01:30")
    win.go_button.click()
    assert win.state.t0 == T0 + 90 * SEC
    win.set_zone(__import__("zoneinfo").ZoneInfo("America/Chicago"))
    assert win.time_edit.text() == "18:01:30"


def test_shift_drag_measures_time_frequency_and_amplitude(one, qtbot):
    c = one.canvas
    y = int(c.row_y(0))
    x0 = int(c.gutter + 100)
    QTest.mousePress(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, QPoint(x0, y))
    QTest.mouseMove(c, QPoint(x0 + 100, y))
    text = c.measurement()
    dt_ms = 100 / c.px_per_mm / 30 * 1000
    assert f"{dt_ms:.1f} ms" in text
    assert f"{1000 / dt_ms:.2f} Hz" in text
    assert "µV" in text and "to" in text
    QTest.mouseRelease(
        c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, QPoint(x0 + 100, y)
    )
    assert c.measure_text == ""


def test_pointer_status_and_overlay_do_not_redraw_the_traces(one):
    c = one.canvas
    pm = c.grab_traces()
    QTest.mouseMove(c, QPoint(c.gutter + 50, 30))
    assert one.status_time.text().startswith("2000-01-01 00:00:0")
    assert c.grab_traces() is pm  # same cached layer: the pointer repaints the overlay only


def test_overview_beyond_five_minutes(make_window, tmp_path, qtbot):
    win = make_window(write(tmp_path / "long.mefd", ["a", "b"], seconds=1200, fs=64.0))
    win.set_timebase(0.5)  # a page of ~10 minutes
    assert win.overview()
    settle(qtbot, win)
    assert win.status_mode.text() == "Overview: as recorded, no filters"
    assert win.filters_button.text() == "Overview: as recorded, no filters"
    assert all(r.piece is not None for r in win.canvas.rows)
    assert win.canvas.overview
    assert win.state.filters.is_off  # the choice is kept, only not applied
    win.set_timebase(30)
    assert not win.overview()


def test_pages_beyond_the_first_tile_are_seamless(make_window, tmp_path, qtbot):
    win = make_window(write(tmp_path / "t.mefd", ["c"], seconds=700, fs=256.0))
    win.set_timebase(120)
    page = win.page_us()
    boundary = T0 + int(65536 / 256.0 * SEC)
    win.go_to(boundary - page // 2)
    settle(qtbot, win)
    row = win.canvas.rows[0]
    piece = row.piece
    ref = win.data._combined.read(["c"], T0, T0 + 700 * SEC, dtype="float32")[0]
    i0 = round((piece.t_first_us - T0) * 256 / 1e6)
    n = piece.values.shape[1]
    assert np.array_equal(piece.values[0, :n], ref[i0 : i0 + n])


# --- jump grammar (pure) ----------------------------------------------------------------------


def test_jump_grammar():
    from datetime import UTC
    from zoneinfo import ZoneInfo

    tl = (T0, T0 + 3 * 86_400 * SEC)
    cur = T0 + 8 * 3600 * SEC  # 08:00 on day 1 (UTC)

    def p(text, **kw):
        zone = kw.pop("zone", UTC)
        return jumping.parse(text, current_us=cur, timeline=tl, zone=zone, **kw)

    assert p("09:30") == T0 + (9 * 3600 + 1800) * SEC
    assert p("07:00") == T0 + 86_400 * SEC + 7 * 3600 * SEC  # the next occurrence is tomorrow
    assert p("07:00", previous=True) == T0 + 7 * 3600 * SEC
    assert p("07:00:30") == T0 + 86_400 * SEC + (7 * 3600 + 30) * SEC
    assert p("day 2 08:00") == T0 + 86_400 * SEC + 8 * 3600 * SEC
    assert p("2000-01-02 01:02:03") == T0 + 86_400 * SEC + 3723 * SEC
    assert p("+10m") == cur + 600 * SEC
    assert p("-90s") == cur - 90 * SEC
    assert p("+2h") == cur + 7200 * SEC
    letters = {"A": T0, "B": T0 + 86_400 * SEC}
    assert p("b", letters=letters) == T0 + 86_400 * SEC
    prague = ZoneInfo("Europe/Prague")  # UTC+1 in January
    assert p("day 1 09:00", zone=prague) == T0 + 8 * 3600 * SEC
    for bad in (
        "",
        "25:99",
        "day 0 08:00",
        "+10x",
        "C",
        "2000-13-01 00:00",
        "+400d",
        "hello there",
    ):
        with pytest.raises(jumping.JumpError):
            p(bad, letters=letters)


# --- data flow: the GUI thread never calls mef3io ---------------------------------------


def test_gui_thread_never_calls_mef3io(window, qtbot, tmp_path, monkeypatch):
    calls = []
    main = threading.main_thread()

    def guard(name, real):
        def wrapped(*a, **k):
            calls.append((name, threading.current_thread() is main))
            return real(*a, **k)

        return wrapped

    for cls in (mef3io.Reader, mef3io.MultiReader):
        for name in ("read", "block_envelope", "info", "segments", "records", "close", "toc"):
            if hasattr(cls, name):
                monkeypatch.setattr(cls, name, guard(name, getattr(cls, name)))
    for name in ("Reader", "MultiReader"):
        monkeypatch.setattr(mef3io, name, guard(name, getattr(mef3io, name)))
    paths = [
        write(tmp_path / "x.mefd", start=T0, seconds=30),
        write(tmp_path / "y.mefd", start=T0 + 60 * SEC, seconds=30, seed=3),
    ]
    from mef3io_studio.explorer.page import ExplorerPage  # noqa: F401

    window._closing = True
    window.show()
    page = window.explorer
    page.open_paths([str(p) for p in paths], interactive=False)
    qtbot.waitUntil(
        lambda: len(page.entries()) == 2 and all(not e.busy for e in page.entries()), timeout=60_000
    )
    calls.clear()
    page.table.selectAll()
    assert page.view_button.isEnabled()
    page.view_button.click()
    qtbot.waitUntil(lambda: bool(window.viewers), timeout=60_000)
    win = window.viewers[0]
    qtbot.waitUntil(lambda: win._ready, timeout=10_000)
    settle(qtbot, win)
    win.go_to(T0 + 70 * SEC)
    win.set_state(filters=dsp.FilterSettings(0.5, 70.0), montage="bipolar")
    key(win, Qt.Key.Key_End)
    settle(qtbot, win)
    win.set_timebase(0.2)
    settle(qtbot, win)
    names = {n for n, on_main in calls if on_main}
    assert not names, names
    assert {"read", "block_envelope"} <= {n for n, _ in calls}
    assert win.windowTitle().startswith("Signal viewer: recordings A, B")
    win.close()
    qtbot.waitUntil(lambda: not window.viewers, timeout=5000)


def test_a_refused_pair_is_explained_plainly(window, qtbot, tmp_path):
    a = write(tmp_path / "p.mefd", start=T0, seconds=30)
    b = write(
        tmp_path / "q.mefd", start=T0 + 10 * SEC, seconds=30, seed=2
    )  # same channels, overlapping
    window._closing = True
    window.show()
    page = window.explorer
    page.open_paths([str(a), str(b)], interactive=False)
    qtbot.waitUntil(
        lambda: len(page.entries()) == 2 and all(not e.busy for e in page.entries()), timeout=60_000
    )
    page.table.selectAll()
    page.view_button.click()
    qtbot.waitUntil(
        lambda: window._last_dialog is not None if hasattr(window, "_last_dialog") else False,
        timeout=30_000,
    )
    dialog = window._last_dialog
    assert "cannot be shown together" in dialog.windowTitle()
    assert not window.viewers
    assert "p" not in dialog.windowTitle()


def test_dark_theme_and_profiles_follow_the_app(make_window, tmp_path):
    win = make_window(write(tmp_path / "d.mefd"), theme=DARK)
    assert win.canvas.palette_.name == "dark"
    win.set_state(profile="green")
    assert win.canvas.palette_.name == "green" and win.canvas.palette_.grid_minor
    win.set_state(profile="auto")
    win.set_theme(LIGHT)
    assert win.canvas.palette_.name == "white"
    pix = win.canvas.grab_traces()
    assert pix.width() > 100
    assert QApplication.instance() is not None and QPointF(0, 0).x() == 0
