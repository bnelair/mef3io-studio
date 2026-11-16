"""Moving through a recording of days: the overview strip (data, gaps, midnights,
a readout), hours, days and stretches with data, a date in Go, the slowest
timebases (docs/design/viewer.md, "Long recordings"). Synthetic, small rates."""

# ruff: noqa: F811
from __future__ import annotations

import time

import mef3io
import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from test_viewer_ui import make_window, settle  # noqa: F401

from mef3io_studio.model import viewer_view as vv
from mef3io_studio.viewer import strip as strips

T0 = 946_684_800_000_000  # 2000-01-01 00:00:00 UTC
SEC = 1_000_000
HOUR = 3600 * SEC
DAY = 24 * HOUR

#: (start hour, end hour) of the stretches with data: gaps of 3 h, 90 min and 2 days.
STRETCHES = [(0, 30), (33, 70), (71.5, 72.5), (120 - 40, 120)]
STRETCHES = [(0, 30), (33, 70), (71.5, 77), (100, 120)]


def write_days(path, fs=0.5, names=("A1", "A2")):
    """Five days with three gaps (3 h, 90 min, 23 h), two channels at ``fs`` Hz."""
    rng = np.random.default_rng(4)
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        for name in names:
            for a, b in STRETCHES:
                n = int((b - a) * 3600 * fs)
                x = 30 * np.sin(2 * np.pi * np.arange(n) / 600.0) + 5 * rng.normal(size=n)
                w.write(name, np.round(x, 2), T0 + int(a * HOUR), fs)
    return path


@pytest.fixture(scope="module")
def days_path(tmp_path_factory):
    return write_days(tmp_path_factory.mktemp("days") / "days.mefd")


def with_stretches(win, qtbot):
    """Wait for the strip to learn the real stretches from the block index."""
    qtbot.waitUntil(lambda: bool(win._stretches), timeout=20_000)
    return win


def test_the_strip_draws_at_most_one_run_per_pixel_however_gappy_the_recording():
    rng = np.random.default_rng(2)
    a, b = T0, T0 + 5 * DAY
    starts = np.sort(rng.integers(a, b, 60_000))
    spans = [(int(s), int(s) + int(rng.integers(1, 60) * SEC), 0) for s in starts]
    t = time.perf_counter()
    runs = strips.pixel_runs(spans, (a, b), 1600)
    assert time.perf_counter() - t < 0.5
    assert 0 < len(runs) <= 1600
    assert all(0 <= x0 < x1 <= 1600 for x0, x1, _ in runs)
    # a real gap of 3 hours (about 5 pixels at five days over 1600 px) stays a gap
    gap = [(T0, T0 + 10 * HOUR, 0), (T0 + 13 * HOUR, T0 + 5 * DAY, 0)]
    runs = strips.pixel_runs(gap, (a, b), 1600)
    assert len(runs) == 2 and runs[1][0] - runs[0][1] >= 3


def test_the_strip_says_what_is_under_the_pointer_and_how_long_a_gap_is(
    make_window, days_path, qtbot
):
    win = with_stretches(make_window(days_path), qtbot)
    strip = win.strip
    strip.resize(1600, 32)

    def at(t):
        return strip.readout(strip.x_of(t))

    inside = at(T0 + 10 * HOUR)
    assert "2000-01-01 10:00:00" in inside and "day 1" in inside and "There is data" in inside
    gap = at(T0 + 31 * HOUR)  # between 30 h and 33 h
    assert "day 2" in gap and "No data: a gap of 3 h" in gap
    assert "day 4" in at(T0 + 80 * HOUR) and "a gap of 23 h" in at(T0 + 80 * HOUR)


def test_the_strip_marks_every_midnight_with_a_line_and_its_date(make_window, days_path, qtbot):
    win = with_stretches(make_window(days_path), qtbot)
    strip = win.strip
    strip.resize(1600, 32)
    img = strip.grab().toImage()
    ink = strip.palette_.text.lstrip("#")
    want = tuple(int(ink[i : i + 2], 16) for i in (0, 2, 4))
    for k in range(1, 5):
        x = round(strip.x_of(T0 + k * DAY))
        near = [tuple(img.pixelColor(x + d, 2).getRgb()[:3]) for d in (-1, 0, 1)]
        assert want in near, k  # the line runs up through the top of the bar
    dates = strips_dates_drawn(strip)
    assert len(dates) >= 4  # dates, not only times, label the days


def strips_dates_drawn(strip):
    """The date labels the strip drew: a second paint with the painter wrapped."""
    from PySide6.QtGui import QPainter

    seen = []
    real = QPainter.drawText

    def spy(self, *a):
        if len(a) == 2 and isinstance(a[1], str):
            seen.append(a[1])
        return real(self, *a)

    QPainter.drawText = spy
    try:
        strip.grab()
    finally:
        QPainter.drawText = real
    return [s for s in seen if len(s) == 10 and s[4] == "-" and s[7] == "-"]


def test_hours_days_and_stretches_move_the_page(make_window, days_path, qtbot):
    win = with_stretches(make_window(days_path), qtbot)
    win.set_timebase(30)
    win.go_to(T0 + 2 * HOUR)
    win.move_by(HOUR, 1)
    assert win.state.t0 == T0 + 3 * HOUR
    QTest.keyClick(win.canvas, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
    assert win.state.t0 == T0 + 4 * HOUR  # Ctrl+Right: an hour
    QTest.keyClick(win.canvas, Qt.Key.Key_Left, Qt.KeyboardModifier.ControlModifier)
    assert win.state.t0 == T0 + 3 * HOUR
    mods = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    QTest.keyClick(win.canvas, Qt.Key.Key_Right, mods)
    assert win.state.t0 == T0 + 27 * HOUR  # Ctrl+Shift+Right: a day
    QTest.keyClick(win.canvas, Qt.Key.Key_Left, mods)
    assert win.state.t0 == T0 + 3 * HOUR
    # next stretch with data: over the 3 hour gap, then on
    win.go_to(T0 + 20 * HOUR)
    QTest.keyClick(win.canvas, Qt.Key.Key_Period)
    assert win.state.t0 == T0 + 33 * HOUR
    QTest.keyClick(win.canvas, Qt.Key.Key_Period)
    assert win.state.t0 == T0 + int(71.5 * HOUR)
    QTest.keyClick(win.canvas, Qt.Key.Key_Comma)  # back to where this stretch began? no: before it
    assert win.state.t0 == T0 + 33 * HOUR
    win.go_to(T0 + 110 * HOUR)
    QTest.keyClick(win.canvas, Qt.Key.Key_Comma)  # from inside the last stretch: its own start
    assert win.state.t0 == T0 + 100 * HOUR
    win.go_to(T0 + 105 * HOUR)
    QTest.keyClick(win.canvas, Qt.Key.Key_Period)
    assert "no later stretch" in win.status_message.text()
    win.go_to(T0)
    QTest.keyClick(win.canvas, Qt.Key.Key_Comma)
    assert "no earlier stretch" in win.status_message.text()


def test_the_long_recording_buttons_show_only_where_they_mean_something(
    make_window, days_path, tmp_path, qtbot
):
    from test_viewer_ui import write

    short = make_window(write(tmp_path / "short.mefd"))
    assert not any(
        b.isVisibleTo(short)
        for b in (short.back_day_button, short.back_hour_button, short.next_stretch_button)
    )
    win = with_stretches(make_window(days_path), qtbot)
    assert all(
        b.isVisibleTo(win)
        for b in (
            win.back_day_button,
            win.back_hour_button,
            win.forward_hour_button,
            win.forward_day_button,
            win.prev_stretch_button,
            win.next_stretch_button,
        )
    )
    win.go_to(T0 + HOUR)
    win.forward_hour_button.click()
    assert win.state.t0 == T0 + 2 * HOUR


def test_the_slowest_timebases_put_days_on_the_screen_and_the_label_says_so(make_window, days_path):
    assert vv.TIMEBASE_STEPS[0] == 0.002
    win = make_window(days_path)
    win.resize(2600, 700)  # a wide window: a page of days at the slowest timebases
    win.set_timebase(0.002)
    assert win.page_us() > 2 * DAY
    assert "day" in win.length_label.text()
    win.set_timebase(0.005)
    assert DAY < win.page_us() < 2 * DAY
    assert win.length_label.text().startswith("= 1 day")
    assert (
        vv.format_length(30 * HOUR) == "1 day 6 h" and vv.format_length(58 * HOUR) == "2 days 10 h"
    )


def test_go_takes_a_date_and_a_time_or_a_date(make_window, days_path):
    win = make_window(days_path)
    win.time_edit.setText("2000-01-03 08:30:15")
    win.go_button.click()
    assert win.state.t0 == T0 + 2 * DAY + 8 * HOUR + 30 * 60 * SEC + 15 * SEC
    win.time_edit.setText("2000-01-04")
    win.go_button.click()
    assert win.state.t0 == T0 + 3 * DAY
    win.time_edit.setText("08:00:00")  # a time alone is still read in the day chosen beside it
    win.day_box.setValue(2)
    win.go_button.click()
    assert win.state.t0 == T0 + DAY + 8 * HOUR
    win.time_edit.setText("2000-02-30 10:00")
    win.go_button.click()
    assert "does not exist" in win.status_message.text()


def test_five_days_stay_responsive_at_every_zoom(make_window, days_path, qtbot):
    win = make_window(days_path)
    t = time.perf_counter()
    for tb in (0.002, 0.005, 0.02, 0.1, 1, 10, 30):
        win.set_timebase(tb)
        settle(qtbot, win)
    assert time.perf_counter() - t < 30
    # the overview of the whole recording is drawn from a bounded amount of data
    win.set_timebase(0.002)
    settle(qtbot, win)
    assert (
        win.canvas.drawn_points
        and max(win.canvas.drawn_points.values()) <= 3 * win.canvas.plot_width
    )
    assert len(win.strip._pixel_runs()) <= win.strip.width()


def test_the_new_keys_are_in_the_help(make_window, days_path):
    from mef3io_studio.viewer.keymap import KEYMAP

    actions = {e.action: e for e in KEYMAP}
    for name in ("hour_forward", "day_forward", "stretch_forward", "stretch_back"):
        assert name in actions
    assert actions["stretch_forward"].keys == (".",)


def test_a_late_request_for_the_strip_after_the_window_closed_does_nothing(make_window, days_path):
    """The request is made by a zero timer after the first page; a window that is
    closed by then (the pool already shut down) must not raise in the event loop."""
    win = make_window(days_path)
    win.close()
    win.request_strip_envelope()  # the window's own guard
    win.controller.request_strip_envelope(win._channels[0])  # and the controller's
    win.controller._strip_pool.shutdown(wait=True)
    win.controller._closed = False
    win.controller.request_strip_envelope(win._channels[0])  # a pool that is gone: no raise
    win.controller._closed = True


# --- the strip when the first channel is not in every recording ----------


def write_two(tmp_path, names_a, names_b, gap_h=1):
    """Recording A (10 min) and, ``gap_h`` hours after it, recording B (10 min)."""
    rng = np.random.default_rng(1)
    paths = []
    for tag, names, start in (
        ("a", names_a, T0),
        ("b", names_b, T0 + 10 * 60 * SEC + gap_h * HOUR),
    ):
        path = tmp_path / f"{tag}.mefd"
        with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
            for name in names:
                w.write(name, np.round(rng.normal(0, 30, 64 * 600), 2), start, 64.0)
        paths.append(path)
    return paths


def test_a_recording_without_the_first_channel_is_still_found_on_the_strip(
    make_window, tmp_path, qtbot
):
    a, b = write_two(tmp_path, ["Cz", "Fz", "Pz"], ["Fz", "Pz"])  # B lacks the first channel, Cz
    win = make_window(a, b)
    qtbot.waitUntil(lambda: bool(win._stretches), timeout=20_000)
    b_start = T0 + 10 * 60 * SEC + HOUR
    assert any(abs(s - b_start) < 2 * SEC for s, _e, _r in win._stretches)
    assert any(abs(s - b_start) < 2 * SEC for s in win._stretch_starts())  # and "." reaches it
    win.go_to(T0)
    win.go_to_stretch(1)
    assert abs(win.state.t0 - b_start) < 2 * SEC


def test_time_the_index_read_does_not_reach_keeps_the_segment_coverage(make_window, tmp_path):
    a, b = write_two(tmp_path, ["Cz", "Fz", "Pz"], ["Fz", "Pz"])
    win = make_window(a, b)
    # the read was of Cz only, which recording B does not have: B is not "no data"
    win._on_stretches(([(T0, T0 + 10 * 60 * SEC)], ["Cz"]))
    b_start = T0 + 10 * 60 * SEC + HOUR
    assert any(
        r == 1 and s <= b_start + SEC and e > b_start + 60 * SEC for s, e, r in win._stretches
    )
    assert any(r == 1 for _s, _e, r in win.strip.coverage)


def test_the_unreached_pieces_are_what_no_read_channel_covers():
    spans = [(0, 100, 0), (200, 300, 1)]
    assert vv.unreached(spans, [(0, 100, 0)]) == [(200, 300, 1)]
    assert vv.unreached(spans, [(20, 40, 0), (60, 250, 0)]) == [
        (0, 20, 0),
        (40, 60, 0),
        (250, 300, 1),
    ]
    assert vv.unreached(spans, []) == spans


def test_a_save_waits_for_the_strip_read_before_the_data_is_reopened(make_window, tmp_path, qtbot):
    a, _b = write_two(tmp_path, ["Cz", "Fz"], ["Fz"])
    win = make_window(a)
    import threading

    started, release, running = threading.Event(), threading.Event(), []
    real = win.data.envelope

    def slow(*args, **kw):
        running.append(1)
        started.set()
        release.wait(5)
        out = real(*args, **kw)
        running.pop()
        return out

    win.data.envelope = slow
    win.controller.request_strip_envelope("Cz")
    assert started.wait(5)
    t = threading.Thread(target=lambda: win.controller.drain_strip(10))
    t.start()
    t.join(0.3)
    assert t.is_alive() and running  # it waits for the read that runs
    release.set()
    t.join(5)
    assert not t.is_alive() and not running
    seen = []
    win.controller.strip_stretches.connect(seen.append)
    qtbot.wait(300)
    assert not seen  # its result was dropped
    del win.data.envelope
