"""How much time is on screen, beside the timebase (docs/design/viewer.md,
"Layout"): the plain-words formatter and the label that follows the page."""

# ruff: noqa: F811
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from test_viewer_ui import SEC, key, make_window, one, settle, write  # noqa: F401

from mef3io_studio.model import viewer_view as vv

MIN, HOUR, DAY = 60 * SEC, 3600 * SEC, 86_400 * SEC


@pytest.mark.parametrize(
    ("us", "text"),
    [
        # under a second: milliseconds
        (0, "1 ms"),
        (400, "1 ms"),
        (250_000, "250 ms"),
        (999_400, "999 ms"),
        (999_600, "1 s"),  # rounds up into the next unit
        # seconds: a tenth under ten, whole after
        (SEC, "1 s"),
        (2_500_000, "2.5 s"),
        (9 * SEC, "9 s"),
        (9_940_000, "9.9 s"),
        (9_960_000, "10 s"),
        (10 * SEC, "10 s"),
        (10_400_000, "10 s"),
        (29_600_000, "30 s"),
        (59_400_000, "59 s"),
        (59_600_000, "1 min"),
        # minutes: with seconds under ten minutes, whole minutes after
        (MIN, "1 min"),
        (90 * SEC, "1 min 30 s"),
        (4 * MIN, "4 min"),
        (4 * MIN + 30 * SEC, "4 min 30 s"),
        (599 * SEC + 400_000, "9 min 59 s"),
        (599_600_000, "10 min"),
        (10 * MIN + 29 * SEC, "10 min"),
        (12 * MIN, "12 min"),
        (59 * MIN + 29 * SEC, "59 min"),
        (59 * MIN + 31 * SEC, "1 h"),
        # hours: with minutes
        (HOUR, "1 h"),
        (2 * HOUR + 10 * MIN, "2 h 10 min"),
        (2 * HOUR + 10 * MIN + 29 * SEC, "2 h 10 min"),
        (2 * HOUR + 10 * MIN + 31 * SEC, "2 h 11 min"),
        (23 * HOUR + 59 * MIN + 40 * SEC, "1 day"),
        # days: with hours
        (DAY, "1 day"),
        (DAY + 6 * HOUR, "1 day 6 h"),
        (2 * DAY, "2 days"),
        (2 * DAY + 3 * HOUR + 20 * MIN, "2 days 3 h"),
        (10 * DAY, "10 days"),
    ],
)
def test_lengths_are_written_in_plain_words(us, text):
    assert vv.format_length(us) == text


def test_the_formatter_never_writes_a_zero_or_a_negative_length():
    assert vv.format_length(-5 * SEC) == "1 ms"
    for us in range(0, 40 * DAY, 997 * MIN + 13 * SEC):
        text = vv.format_length(us)
        assert text and not text.startswith(("0 ", "-"))


def test_the_sentence_for_short_stretches_uses_the_same_words():
    text = vv.short_stretch_text(12_300_000)
    assert "too short to filter" in text and "shorter than 12 s" in text
    assert "shown as recorded" in text and "paler" in text.lower()


def test_the_label_sits_beside_the_timebase_and_says_what_it_is(one):
    win = one
    assert win.length_label.text() == f"= {vv.format_length(win.page_us())}"
    toolbar = win.timebase_box.parentWidget()
    items = [toolbar.widgetForAction(a) for a in toolbar.actions()]
    assert items.index(win.length_label) == items.index(win.timebase_box) + 1
    tip = win.length_label.toolTip()
    assert "time fits on the screen" in tip and "wider window shows more time" in tip
    assert "same timebase" in tip
    assert win.length_label.accessibleName()


@pytest.mark.parametrize(("tb", "page_s"), [(30, 10), (15, 20), (60, 5), (120, 2.5)])
def test_the_label_follows_the_timebase(one, qtbot, tb, page_s):
    one.set_timebase(tb)
    settle(qtbot, one)
    want = one.canvas.plot_width / one.canvas.px_per_mm / tb
    assert one.length_label.text() == f"= {vv.format_length(want * 1e6)}"
    assert abs(one.page_us() / 1e6 - want) < 0.01
    # the figures are those of a physical page (a 1200 px window: about 10 s at 30 mm/s)
    assert page_s * 0.5 < want < page_s * 1.5


def test_the_label_follows_the_alt_number_keys_and_zoom_by_wheel(one, qtbot):
    seen = {}
    for k, tb in (
        (Qt.Key.Key_1, 10),
        (Qt.Key.Key_2, 15),
        (Qt.Key.Key_3, 30),
        (Qt.Key.Key_4, 60),
        (Qt.Key.Key_5, 120),
    ):
        key(one, k, Qt.KeyboardModifier.AltModifier)
        assert one.state.timebase == tb
        assert one.length_label.text() == f"= {vv.format_length(one.page_us())}"
        seen[tb] = one.length_label.text()
    assert len(set(seen.values())) == 5  # every timebase says something else
    one.canvas.wheel_zoom.emit(1.0, 0.5)  # Ctrl+wheel zooms in one step
    assert one.state.timebase == 180
    assert one.length_label.text() == f"= {vv.format_length(one.page_us())}"
    one.canvas.wheel_zoom.emit(-1.0, 0.5)
    one.canvas.wheel_zoom.emit(-1.0, 0.5)
    assert one.state.timebase == 90
    assert one.length_label.text() == f"= {vv.format_length(one.page_us())}"


def test_the_label_follows_a_resize_before_the_page_is_asked_for_again(one, qtbot):
    before = one.length_label.text()
    one.resize(one.width() + 400, one.height())
    qtbot.waitUntil(lambda: one.canvas.plot_width > 1300, timeout=5000)
    # at once from the resize, not only after the page refresh's delay
    assert one.length_label.text() == f"= {vv.format_length(one.page_us())}"
    assert one.length_label.text() != before
    one.resize(one.width() - 600, one.height())
    qtbot.wait(300)
    assert one.length_label.text() == f"= {vv.format_length(one.page_us())}"
    assert one.canvas.t1 - one.canvas.t0 == one.page_us()


def test_the_label_follows_a_display_with_another_resolution(one, qtbot, monkeypatch):
    # (four toolbar rows leave less height: the row scroll bar, which narrows the plot, shows
    # up after the first layout; the page it gives is the one to come back to)
    one.resize(1200, 900)
    qtbot.wait(150)
    before = one.page_us()
    dpi = one.canvas.logicalDpiX()
    monkeypatch.setattr(one.canvas, "logicalDpiX", lambda: dpi * 2)  # twice the pixels per mm
    one._screen_changed()  # what the window's screen change calls
    assert one.page_us() < before * 0.6
    assert one.length_label.text() == f"= {vv.format_length(one.page_us())}"
    qtbot.wait(300)  # the page itself is then asked for again, with the new length
    assert abs((one.canvas.t1 - one.canvas.t0) - one.page_us()) <= 1
    monkeypatch.setattr(one.canvas, "logicalDpiX", lambda: dpi)
    one._screen_changed()
    assert one.page_us() == before
    assert one.length_label.text() == f"= {vv.format_length(before)}"


def test_the_window_listens_for_a_change_of_screen(one):
    assert one._screen_hooked  # the window handle exists once the window is shown


def test_the_label_is_there_in_the_overview_too(make_window, tmp_path, qtbot):
    win = make_window(write(tmp_path / "long.mefd", ["a", "b"], seconds=1200, fs=64.0))
    win.set_timebase(0.5)
    assert win.overview()
    settle(qtbot, win)
    assert win.length_label.text() == f"= {vv.format_length(win.page_us())}"
    assert " min" in win.length_label.text()  # about ten minutes
    win.set_timebase(30)
    assert not win.overview()
    assert win.length_label.text().endswith(" s")
