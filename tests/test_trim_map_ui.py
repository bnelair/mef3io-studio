"""The map on "Join or trim -> What to keep": Explore's timeline with a
draggable Start and End line, their date and number fields kept in step, and
the page using one window for every recording. Synthetic data only."""

from __future__ import annotations

import mef3io
import numpy as np
import pytest
from explorer_sessions import metadata
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from test_anonymizer_ui import open_in_explorer
from test_joiner_ui import choose_all, write

from mef3io_studio.joiner.page import (
    LEFT_OUT_TEXT,
    MODE_EACH,
    MODE_WINDOW,
    NO_DATA_IN_WINDOW,
    STEP_KEEP,
    STEP_PRIVACY,
)
from mef3io_studio.joiner.trimmap import END, START, TrimMapWidget
from mef3io_studio.model.coverage import RecordingInput, combine
from mef3io_studio.model.join import KEEP_FIRST, KEEP_WHOLE
from mef3io_studio.model.recording import ChannelInfo, Part, RecordingDetails
from mef3io_studio.model.timefmt import format_utc_us, parse_utc_text, parse_whole_number

T0 = 946_684_800_000_000
SECOND = 1_000_000
HOUR = 3600 * SECOND


def details(name, spans, fs=256.0):
    parts = tuple(Part(i, a, b, int((b - a) / SECOND * fs), 1) for i, (a, b) in enumerate(spans))
    chan = ChannelInfo("c", fs, "uV", 0, parts[0].start, parts[-1].end, parts)
    return RecordingDetails(f"/made/up/{name}.mefd", (chan,))


@pytest.fixture
def window_page(window):
    window._closing = True
    window.show()
    window.module_list.setCurrentRow(window.module_keys.index("joiner"))
    return window, window.joiner


@pytest.fixture
def trim_map(qtbot):
    # A: one hour. B: starts two hours later, two parts with a gap inside.
    items = [
        RecordingInput("A", "A", details("A", [(T0, T0 + HOUR)])),
        RecordingInput(
            "B",
            "B",
            details("B", [(T0 + 3 * HOUR, T0 + 4 * HOUR), (T0 + 5 * HOUR, T0 + 6 * HOUR)]),
        ),
    ]
    layout, _ = combine(items)
    widget = TrimMapWidget()
    qtbot.addWidget(widget)
    widget.resize(900, 420)
    widget.set_bounds(T0, T0 + 6 * HOUR)
    widget.set_layout(layout, refit=True)
    widget.set_window(T0, T0 + 6 * HOUR)
    widget.set_lines_enabled(True)
    widget.show()
    return widget


def click_pos(widget, t):
    return QPoint(round(widget.x_of(t)), round(widget._metrics()[3] + 30))


def test_dragging_a_line_moves_it_and_says_so(trim_map, qtbot):
    moved = []
    trim_map.window_moved.connect(lambda s, e: moved.append((s, e)))
    target = T0 + 2 * HOUR
    QTest.mousePress(trim_map, Qt.MouseButton.LeftButton, pos=click_pos(trim_map, T0))
    QTest.mouseMove(trim_map, click_pos(trim_map, T0 + HOUR))
    QTest.mouseMove(trim_map, click_pos(trim_map, target))
    QTest.mouseRelease(trim_map, Qt.MouseButton.LeftButton, pos=click_pos(trim_map, target))
    assert moved and moved[-1][1] == T0 + 6 * HOUR
    # Within a pixel of where the pointer ended.
    per_pixel = (trim_map.v1 - trim_map.v0) / trim_map.plot_rect().width()
    assert abs(trim_map.start - target) <= per_pixel * 1.5
    assert trim_map.active == START
    assert trim_map.end == T0 + 6 * HOUR


def test_a_drag_that_misses_the_lines_pans_instead(trim_map):
    before = (trim_map.start, trim_map.end, trim_map.v0)
    middle = click_pos(trim_map, T0 + 3 * HOUR)
    QTest.mousePress(trim_map, Qt.MouseButton.LeftButton, pos=middle)
    QTest.mouseMove(trim_map, middle + QPoint(60, 0))
    QTest.mouseRelease(trim_map, Qt.MouseButton.LeftButton, pos=middle + QPoint(60, 0))
    assert (trim_map.start, trim_map.end) == before[:2]
    assert trim_map.v0 != before[2]


def test_the_lines_stay_inside_the_span_and_in_order(trim_map):
    trim_map.move_line(START, T0 - 5 * HOUR)
    assert trim_map.start == T0
    trim_map.move_line(END, T0 + 99 * HOUR)
    assert trim_map.end == T0 + 6 * HOUR
    trim_map.move_line(END, T0 + 3 * HOUR)
    trim_map.move_line(START, T0 + 5 * HOUR)  # past End: stops just before it
    assert trim_map.start == T0 + 3 * HOUR - 1 and trim_map.end == T0 + 3 * HOUR
    trim_map.move_line(END, T0)  # before Start: stops just after it
    assert trim_map.end == trim_map.start + 1


def test_a_dragged_line_sticks_to_the_first_and_last_moment(trim_map):
    trim_map.set_window(T0 + HOUR, T0 + 2 * HOUR)
    x = trim_map.x_of(T0) + 2  # two pixels short of the first moment
    QTest.mousePress(trim_map, Qt.MouseButton.LeftButton, pos=click_pos(trim_map, T0 + HOUR))
    QTest.mouseMove(trim_map, QPoint(round(x), 200))
    QTest.mouseRelease(trim_map, Qt.MouseButton.LeftButton, pos=QPoint(round(x), 200))
    assert trim_map.start == T0


def test_keyboard_picks_a_line_and_nudges_it(trim_map, qtbot):
    trim_map.setFocus()
    assert trim_map.active is None
    QTest.keyClick(trim_map, Qt.Key.Key_E)
    assert trim_map.active == END
    before = trim_map.end
    QTest.keyClick(trim_map, Qt.Key.Key_Left)
    one = before - trim_map.end
    assert one > 0
    QTest.keyClick(trim_map, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier)
    assert before - trim_map.end == one * 11
    QTest.keyClick(trim_map, Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
    assert before - trim_map.end == one * 11 - SECOND
    QTest.keyClick(trim_map, Qt.Key.Key_Home)
    assert trim_map.end == trim_map.start + 1  # as far left as Start allows
    QTest.keyClick(trim_map, Qt.Key.Key_S)
    assert trim_map.active == START
    QTest.keyClick(trim_map, Qt.Key.Key_Escape)
    assert trim_map.active is None
    # With no line picked the arrow keys move the view, as in Explore.
    v0 = trim_map.v0
    QTest.keyClick(trim_map, Qt.Key.Key_Left)
    assert trim_map.v0 != v0
    assert "arrow" in trim_map.accessibleName()


def test_zoom_still_works_and_a_line_can_be_placed_finely(trim_map):
    trim_map.zoom(20, trim_map.x_of(T0 + HOUR))
    span = trim_map.v1 - trim_map.v0
    assert span < 6 * HOUR / 10
    trim_map.fit_all()
    assert trim_map.v1 - trim_map.v0 >= 6 * HOUR


def test_kept_and_left_out_rows_are_painted_without_error(trim_map):
    trim_map.set_kept({"A": (T0, T0 + HOUR // 2)}, {"B": LEFT_OUT_TEXT})
    image = trim_map.grab().toImage()
    assert not image.isNull()
    trim_map.set_lines_enabled(False)
    trim_map.grab()


# -- the text helpers ---------------------------------------------------------------------


def test_utc_text_round_trips_to_the_microsecond():
    t = 1_793_883_922_123_456  # 2026-11-05 ...
    assert parse_utc_text(format_utc_us(t)) == t
    assert format_utc_us(T0) == "2000-01-01 00:00:00.000000"
    assert parse_utc_text("2026-11-04 13:05:22.123456") == parse_utc_text(
        "2026-11-04T13:05:22.123456 UTC"
    )
    assert parse_utc_text("2026-11-04 13:05") == parse_utc_text("2026-11-04 13:05:00.000000Z")
    assert (
        parse_utc_text("2026-11-04 13:05:22.5") - parse_utc_text("2026-11-04 13:05:22") == 500_000
    )
    for bad in (
        "",
        "tomorrow",
        "2026-13-01 00:00:00",
        "2026-02-30",
        "2026-11-04 25:00:00",
        "1604495122",
    ):
        assert parse_utc_text(bad) is None
    assert parse_whole_number("1 604 495 122 123 456") == 1604495122123456
    assert parse_whole_number("1,604,495") == 1604495
    for bad in ("", "-5", "1.5", "12ab", "٣"):
        assert parse_whole_number(bad) is None


# -- the page ---------------------------------------------------------------------------


def two_recordings(qtbot, window, tmp_path):
    a = write(tmp_path / "A.mefd", T0)
    b = write(tmp_path / "B.mefd", T0 + 3 * HOUR)
    open_in_explorer(qtbot, window, [a, b])
    return a, b


def settle_map(qtbot, keep):
    qtbot.waitUntil(lambda: not keep._layout_runner.busy and keep.map.layout_data is not None)


def type_into(edit, text):
    edit.setFocus()
    edit.selectAll()
    edit.setText(text)
    edit.editingFinished.emit()


def test_the_step_shows_the_map_with_a_window_for_all_by_default(window_page, qtbot, tmp_path):
    window, page = window_page
    two_recordings(qtbot, window, tmp_path)
    choose_all(page)
    keep = page.keep
    settle_map(qtbot, keep)
    assert keep.mode == MODE_WINDOW and keep.mode_window.isChecked()
    assert keep.window == keep.bounds == (T0, T0 + 5 * HOUR)
    assert keep.map.lines_enabled and keep.map.layout_data is not None
    assert [r.key for r in keep.map.layout_data.rows] == [r.entry.key for r in keep.rows]
    assert keep.date_edit[START].text() == "2000-01-01 00:00:00.000000"
    assert keep.number_edit[END].text() == str(T0 + 5 * HOUR)
    assert keep.ok
    assert all(r.keep().how == KEEP_WHOLE for r in keep.rows)  # nothing chosen per recording
    # Whole span: nothing trimmed, so the table says all of it.
    assert keep.table.item(0, 4).text() == "All of it"


def test_one_recording_starts_as_before_choose_for_each(window_page, qtbot, tmp_path):
    window, page = window_page
    open_in_explorer(qtbot, window, [write(tmp_path / "A.mefd", T0)])
    choose_all(page)
    assert page.keep.mode == MODE_EACH and not page.keep.map.lines_enabled
    assert page.keep.window_box.isHidden()


def test_dragging_updates_both_fields_and_the_kept_part(window_page, qtbot, tmp_path):
    window, page = window_page
    two_recordings(qtbot, window, tmp_path)
    choose_all(page)
    keep = page.keep
    settle_map(qtbot, keep)
    keep.resize(1100, 900)
    target = T0 + 90 * 60 * SECOND
    pos = click_pos(keep.map, T0)
    QTest.mousePress(keep.map, Qt.MouseButton.LeftButton, pos=pos)
    QTest.mouseMove(keep.map, click_pos(keep.map, target))
    QTest.mouseRelease(keep.map, Qt.MouseButton.LeftButton, pos=click_pos(keep.map, target))
    start = keep.window[0]
    assert start > T0
    assert keep.number_edit[START].text() == str(start)
    assert keep.date_edit[START].text() == format_utc_us(start)
    # A's kept part now starts at the window's start, to the microsecond.
    span = keep.plan.spans[keep.rows[0].entry.key]
    assert span.start == start and span.end == T0 + 2 * HOUR


def test_typing_a_date_or_a_number_moves_the_line_and_the_other_field(window_page, qtbot, tmp_path):
    window, page = window_page
    two_recordings(qtbot, window, tmp_path)
    choose_all(page)
    keep = page.keep
    settle_map(qtbot, keep)
    type_into(keep.date_edit[START], "2000-01-01 00:30:00.250001 UTC")
    want = T0 + 1800 * SECOND + 250_001
    assert keep.window[0] == want and keep.map.start == want
    assert keep.number_edit[START].text() == str(want)
    assert keep.date_edit[START].text() == "2000-01-01 00:30:00.250001"
    type_into(keep.number_edit[END], str(T0 + 4 * HOUR + 7))
    assert keep.window[1] == T0 + 4 * HOUR + 7 and keep.map.end == T0 + 4 * HOUR + 7
    assert keep.date_edit[END].text() == "2000-01-01 04:00:00.000007"
    assert keep.field_message.isHidden()


def test_invalid_input_says_why_and_moves_nothing(window_page, qtbot, tmp_path):
    window, page = window_page
    two_recordings(qtbot, window, tmp_path)
    choose_all(page)
    keep = page.keep
    settle_map(qtbot, keep)
    before = keep.window
    type_into(keep.date_edit[START], "tomorrow at noon")
    assert keep.window == before and keep.map.start == before[0]
    assert not keep.field_message.isHidden() and "cannot be read" in keep.field_message.text()
    type_into(keep.number_edit[END], "12.5")
    assert keep.window == before and "whole number" in keep.field_message.text()
    # Start must stay before End.
    type_into(keep.number_edit[START], str(before[1]))
    assert keep.window == before and "Start must be before End" in keep.field_message.text()
    type_into(
        keep.date_edit[END], "1999-12-31 23:59:59"
    )  # before the span: clamped, then < Start? no
    assert keep.window == before or keep.window[1] > keep.window[0]
    # A good value clears the message.
    type_into(keep.number_edit[START], str(T0 + 10))
    assert keep.window[0] == T0 + 10 and keep.field_message.isHidden()


def test_a_value_outside_the_span_is_pulled_in_and_said(window_page, qtbot, tmp_path):
    window, page = window_page
    two_recordings(qtbot, window, tmp_path)
    choose_all(page)
    keep = page.keep
    settle_map(qtbot, keep)
    type_into(keep.number_edit[START], str(T0 - 10 * HOUR))
    assert keep.window[0] == T0 and "first moment" in keep.field_message.text()
    type_into(keep.number_edit[END], str(T0 + 90 * HOUR))
    assert keep.window[1] == T0 + 5 * HOUR and "last moment" in keep.field_message.text()


def test_a_recording_without_data_in_the_window_is_left_out(window_page, qtbot, tmp_path):
    window, page = window_page
    two_recordings(qtbot, window, tmp_path)
    choose_all(page)
    keep = page.keep
    settle_map(qtbot, keep)
    names = lambda: [r.entry.name for r in keep.rows]  # noqa: E731
    keep.set_window(T0 + 3 * HOUR + 600 * SECOND, T0 + 4 * HOUR)
    assert names() == ["B"] and keep.left_out
    assert keep.table.item(0, 4).text() == LEFT_OUT_TEXT
    assert keep.map.left_out and keep.ok
    assert page.has_changes()  # leaving one out is a change: a lone whole recording is fine
    # The later steps and the request know only about B.
    assert [s.key for s in keep.sources()] == [r.entry.key for r in keep.rows]
    assert page.make_request().sources[0].keep.start_us == T0 + 3 * HOUR + 600 * SECOND
    # Nothing at all inside: refused with a plain note, Next disabled.
    keep.set_window(T0 + 2 * HOUR, T0 + 2 * HOUR + SECOND)
    assert keep.rows == [] and not keep.ok
    assert NO_DATA_IN_WINDOW in keep.problems.text()
    assert not page.next_button.isEnabled()


def test_a_gap_inside_a_recording_counts_as_no_data(window_page, qtbot, tmp_path):
    window, page = window_page
    # One recording of two parts with an hour-long gap between them, and another one after it.
    gappy = tmp_path / "G.mefd"
    with mef3io.Writer(str(gappy), overwrite=True, metadata=metadata()) as w:
        v = np.arange(3600 * 2, dtype=float)
        w.write("a1", v, T0, 2.0)
        w.write("a1", v, T0 + 2 * HOUR, 2.0, new_segment=True)
    other = write(tmp_path / "O.mefd", T0 + 10 * HOUR, channels=("a1",))
    open_in_explorer(qtbot, window, [gappy, other])
    choose_all(page)
    keep = page.keep
    settle_map(qtbot, keep)
    keep.set_window(T0 + HOUR + 600 * SECOND, T0 + 5 * HOUR)  # inside G's gap, then data
    assert [r.entry.name for r in keep.rows] == ["G"]
    keep.set_window(T0 + HOUR + 600 * SECOND, T0 + HOUR + 1200 * SECOND)  # only the gap
    assert keep.rows == []


def test_touching_a_recordings_choice_switches_to_choose_for_each(window_page, qtbot, tmp_path):
    window, page = window_page
    two_recordings(qtbot, window, tmp_path)
    choose_all(page)
    keep = page.keep
    settle_map(qtbot, keep)
    keep.set_window(T0 + 600 * SECOND, T0 + 4 * HOUR)
    assert all(not r.isEnabled() for r in keep.all_rows)
    keep.rows[0].set_how(KEEP_FIRST)
    assert keep.mode == MODE_EACH and keep.mode_each.isChecked()
    assert not keep.map.lines_enabled and all(r.isEnabled() for r in keep.all_rows)
    # What each keeps comes from its own row again, shown shaded on the map.
    assert keep.rows[1].keep().how == KEEP_WHOLE
    assert keep.map.kept[keep.rows[0].entry.key] == (T0, T0 + 3600 * SECOND)
    # Back to the window: it is still where it was.
    keep.mode_window.setChecked(True)
    assert keep.window == (T0 + 600 * SECOND, T0 + 4 * HOUR)


def test_the_real_times_note_follows_the_dates_choice(window_page, qtbot, tmp_path):
    window, page = window_page
    two_recordings(qtbot, window, tmp_path)
    choose_all(page)
    keep = page.keep
    assert "real times" in keep.real_note.text() and "moved" not in keep.real_note.text()
    page.next()
    assert page.step == STEP_PRIVACY
    page.privacy.anonymous.setChecked(True)
    page.privacy.dates_move.setChecked(True)
    page.back()
    assert page.step == STEP_KEEP
    assert "dates moved" in keep.real_note.text()


def test_the_local_time_is_shown_beside_the_fields_when_a_zone_is_chosen(
    window_page, qtbot, tmp_path
):
    window, page = window_page
    two_recordings(qtbot, window, tmp_path)
    choose_all(page)
    keep = page.keep
    assert keep.local_note.isHidden()
    page.set_zone_name("Europe/Prague")
    assert not keep.local_note.isHidden() and "Prague" in keep.local_note.text()
    assert "+01:00" in keep.local_note.text()
    # The fields stay UTC.
    assert keep.date_edit[START].text() == "2000-01-01 00:00:00.000000"


def test_window_join_end_to_end_output_is_the_window_within_the_data(window_page, qtbot, tmp_path):
    window, page = window_page
    a = write(tmp_path / "A.mefd", T0)
    b = write(tmp_path / "B.mefd", T0 + 3 * HOUR)
    c = write(tmp_path / "C.mefd", T0 + 20 * HOUR)
    open_in_explorer(qtbot, window, [a, b, c])
    choose_all(page)
    keep = page.keep
    settle_map(qtbot, keep)
    start, end = T0 + HOUR + 30 * 60 * SECOND, T0 + 3 * HOUR + 15 * 60 * SECOND + 500_000
    type_into(keep.number_edit[START], str(start))
    type_into(keep.number_edit[END], str(end))
    assert [r.entry.name for r in keep.rows] == ["A", "B"]  # C has nothing in the window
    assert keep.ok
    page.next()  # privacy
    page.next()  # new recording
    out = tmp_path / "out"
    out.mkdir()
    page.new.folder = str(out)
    page.new.name.setText("windowed")
    assert page.new.problem_text() == ""
    check = page.next()
    with qtbot.waitSignal(check.finished, timeout=120_000):
        pass
    qtbot.waitUntil(lambda: page.step == 5 and page.result is not None, timeout=300_000)
    assert page.result.ok
    found = mef3io.time_range(str(out / "windowed.mefd"))
    assert (found.start_uutc, found.end_uutc) == (start, end)
    for p in (a, b, c):
        assert p.exists()
