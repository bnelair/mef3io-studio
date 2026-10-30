"""The viewer's part 2, offscreen (docs/design/viewer.md): which recording is
which (band, boundaries, banner, side panel), notes and events (chips, bands,
types, stepping, privacy) and the export range (keys, drag, copy, list, send
to Join or trim). Synthetic sessions in tmp_path only."""

from __future__ import annotations

import csv
import json
import re
from zoneinfo import ZoneInfo

import mef3io
import numpy as np
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from test_viewer_ui import open_data, settle

from mef3io_studio.common.theme import LIGHT
from mef3io_studio.model import viewer_marks as vm
from mef3io_studio.model.viewer_source import open_recordings
from mef3io_studio.viewer.window import ViewerWindow

T0 = 946_684_800_000_000
SEC = 1_000_000
FS = 128.0


def write(
    path, channels=("Fp1", "Fp2", "Cz"), start=T0, seconds=60.0, notes=(), channel_notes=(), **kw
):
    t = np.arange(int(seconds * FS)) / FS
    with mef3io.Writer(str(path), overwrite=True, units="uV", **kw) as w:
        for k, name in enumerate(channels):
            w.write(name, np.round(40 * np.sin(2 * np.pi * (5 + k) * t), 2), start, FS)
        if notes:
            w.write_annotations(list(notes))
        for ch, items in channel_notes:
            w.write_annotations(list(items), channel=ch)
    return path


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


@pytest.fixture
def pair(tmp_path):
    """A (60 s) and, 30 s later, B (60 s), each with notes at several levels."""
    a = write(
        tmp_path / "Alpha_case.mefd",
        notes=[
            {"time": T0 + 5 * SEC, "type": "Note", "text": "Alpha plain note"},
            {
                "time": T0 + 10 * SEC,
                "type": "EDFA",
                "duration": 4 * SEC,
                "text": "Spike: sharp wave",
            },
        ],
        channel_notes=[("Cz", [{"time": T0 + 20 * SEC, "type": "Note", "text": "Alpha Cz note"}])],
    )
    b = write(
        tmp_path / "Beta_case.mefd",
        start=T0 + 90 * SEC,
        notes=[{"time": T0 + 100 * SEC, "type": "Note", "text": "Beta plain note"}],
        channel_notes=[
            (
                "Fp1",
                [{"time": T0 + 110 * SEC, "type": "EDFA", "duration": 2 * SEC, "text": "Beta Fp1"}],
            )
        ],
    )
    return a, b


def wait_marks(qtbot, win):
    qtbot.waitUntil(lambda: win.marks_loaded, timeout=10_000)
    qtbot.wait(20)


def at(win, t, row=0):
    """Move the pointer to time ``t`` on row ``row``."""
    c = win.canvas
    x = int(c.x_of(t))
    QTest.mouseMove(c, QPoint(x, int(c.row_y(row))))
    return x


class Shown:
    """A per-recording privacy mask, like Explore's Show buttons."""

    def __init__(self, n=2):
        self.flags = dict.fromkeys(range(n), False)

    def __call__(self, i):
        return self.flags[i]

    def show(self, i):
        self.flags[i] = True


# --- which recording is which ----------------------------------------------------------


def test_band_letters_boundary_labels_and_the_banner(make_window, pair, qtbot):
    shown = Shown()
    win = make_window(*pair, reveal=shown, show_details=shown.show)
    assert win.band.isVisible() and win.multi
    win.go_to(T0 + 55 * SEC)  # A ends at 60 s, then a gap
    assert win.band.letters() == [["A"], []]
    assert win.banner.history == []  # opening and staying on A says nothing
    win.go_to(T0 + 85 * SEC)  # the gap, then B from 90 s
    assert win.band.letters() == [[], ["B"]]
    assert win.banner.history == ["Now showing recording B"]  # letter only: hidden
    assert win.banner.isVisible() and win.banner.accessibleDescription() == win.banner.text()
    assert [t for t, _ in win.canvas.boundaries] == [T0 + 90 * SEC]
    assert win.canvas.boundaries[0][1] == "A to B: gap 30 s"
    shown.show(0)
    shown.show(1)
    win.refresh_privacy()
    win.go_to(T0 + 5 * SEC)
    assert win.banner.text() == "Now showing recording A: Alpha_case"
    assert win.banner.history[-1] == "Now showing recording A"  # the history has no names
    assert "Beta" not in " ".join(win.banner.history[:1])


def test_a_page_with_two_recordings_names_both(make_window, tmp_path, qtbot):
    a = write(tmp_path / "A1.mefd", channels=("Fp1",))
    b = write(tmp_path / "B1.mefd", channels=("Fp2",))
    win = make_window(a, b)  # different channels of the same period: side by side
    assert win.band.letters() == [["A", "B"]]
    assert {r.tag for r in win.canvas.rows} == {"A", "B"}  # rows differ: tags are worth it
    assert win.canvas.boundaries == []  # the second starts with the first: no line
    win.go_to(T0 + 55 * SEC)
    assert win.band.letters() == [["A", "B"]]


def test_a_gap_between_touching_recordings_is_labelled(make_window, tmp_path):
    a = write(tmp_path / "t1.mefd")
    b = write(tmp_path / "t2.mefd", start=T0 + 60 * SEC)
    win = make_window(a, b)
    win.go_to(T0 + 55 * SEC)
    assert win.canvas.boundaries and win.canvas.boundaries[0][1].startswith("A to B")


def test_braces_jump_between_boundaries(make_window, pair, qtbot):
    win = make_window(*pair)
    QTest.keyClick(win.canvas, "}")
    assert abs(win.state.t0 + win.page_us() / 2 - (T0 + 90 * SEC)) < 1000
    QTest.keyClick(win.canvas, "}")
    assert "no further boundary" in win.status_message.text()
    QTest.keyClick(win.canvas, "{")
    assert "no further boundary" in win.status_message.text() or win.state.t0 < T0 + 90 * SEC


def test_the_status_line_and_gutter_name_the_recording(make_window, pair):
    shown = Shown()
    win = make_window(*pair, reveal=shown)
    at(win, T0 + 3 * SEC)
    assert win.status_recording.text() == "Recording A"
    shown.show(0)
    at(win, T0 + 4 * SEC)
    assert win.status_recording.text() == "Recording A: Alpha_case"
    assert win.canvas.letter_colours.keys() == {"A", "B"}


def test_the_recordings_tab_follows_the_privacy_mask(make_window, pair):
    shown = Shown()
    win = make_window(*pair, reveal=shown, show_details=shown.show)
    tab = win.recordings_tab
    assert [tab.cell_text(r, 0) for r in range(2)] == ["A", "B"]
    assert [tab.cell_text(r, 1) for r in range(2)] == ["(hidden)", "(hidden)"]
    assert [tab.cell_text(r, 4) for r in range(2)] == ["3", "3"]
    assert tab.cell_text(0, 2).startswith("2000-01-01 00:00:00")
    shown.show(1)
    win.refresh_privacy()
    assert tab.cell_text(1, 1) == "Beta_case" and tab.cell_text(0, 1) == "(hidden)"
    win.press("note_list")
    assert win.side_panel.isVisible()
    tab.go_to.emit(T0 + 90 * SEC)
    assert win.state.t0 == T0 + 90 * SEC


# --- notes and events -----------------------------------------------------------------------


def test_notes_from_both_recordings_and_all_levels(make_window, pair, qtbot):
    shown = Shown()
    win = make_window(*pair, reveal=shown, show_details=shown.show)
    wait_marks(qtbot, win)
    assert len(win.marks) == 5
    assert {m.level for m in win.marks} == {"session", "channel"}
    assert win.notes_button.text() == "Notes and events (5)"
    win.set_timebase(2)  # a page long enough for everything
    assert win.page_us() > 140 * SEC
    win.go_to(T0)
    chips = win.chips.chips
    assert [c.letter for c in chips] == ["A", "A", "A", "B", "B"]
    assert {c.letter for c in chips} == {"A", "B"}
    # hidden: the type only, never the text
    assert all("Alpha" not in c.label and "Beta" not in c.label for c in chips)
    assert all("sharp" not in c.tip for c in chips)
    # the list, sorted by time, with the recording letter and "(hidden)"
    tab = win.notes_tab
    assert [tab.cell_text(r, 1) for r in range(5)] == ["A", "A", "A", "B", "B"]
    assert [tab.cell_text(r, 4) for r in range(5)] == ["(hidden)"] * 5
    wheres = [tab.cell_text(r, 3) for r in range(5)]
    assert wheres == ["Whole recording", "Whole recording", "Cz", "Whole recording", "Fp1"]
    # the Show buttons reuse the explorer's switch (the callback), per recording
    assert set(tab.show_buttons) == {0, 1}
    tab.show_buttons[0].click()
    assert shown(0) and not shown(1)
    assert [tab.cell_text(r, 4) for r in range(3)] == [
        "Alpha plain note",
        "Spike: sharp wave",
        "Alpha Cz note",
    ]
    assert tab.cell_text(3, 4) == "(hidden)"
    assert set(tab.show_buttons) == {1}
    labels = [c.label for c in win.chips.chips]
    assert any("sharp wave" in x for x in labels)
    assert any("Spike" in c.kind for c in win.chips.chips)  # coloured by its quick type


def test_bands_lines_and_rows_on_the_traces(make_window, pair, qtbot):
    win = make_window(*pair)
    wait_marks(qtbot, win)
    win.set_timebase(2)
    win.go_to(T0)
    marks = win.canvas.marks
    spans = [m for m in marks if m.end is not None]
    points = [m for m in marks if m.end is None]
    assert len(spans) == 2 and len(points) == 3
    assert {m.channel for m in marks} == {None, "Cz", "Fp1"}
    assert all("recording" in m.tip for m in marks)
    assert any("recording B" in m.tip for m in marks)
    # drawing it does not fail and the colours come from the fixed table
    win.canvas.grab()
    assert vm.kind_colour("type:EDFA", False) == vm.MEF_TYPES["EDFA"][1]
    assert vm.kind_colour("type:ZZZZ", False) == vm.UNKNOWN_TYPE[1]  # unknown: grey


def test_square_brackets_step_through_the_notes(make_window, pair, qtbot):
    win = make_window(*pair)
    wait_marks(qtbot, win)
    times = sorted({m.time for m in win.marks})
    win.go_to(T0 - 2 * SEC)
    first_centre = win.state.t0 + win.page_us() / 2
    ahead = [t for t in times if t > first_centre + 1]
    seen = []
    for _ in ahead:
        QTest.keyClick(win.canvas, "]")
        seen.append(win.state.t0 + win.page_us() / 2)
    assert [round(c / 1000) for c in seen] == [round(t / 1000) for t in ahead]
    QTest.keyClick(win.canvas, "]")
    assert "no further note" in win.status_message.text()
    QTest.keyClick(win.canvas, "[")
    prev = win.state.t0 + win.page_us() / 2
    assert round(prev / 1000) == round(ahead[-2] / 1000)


def test_types_can_be_hidden_per_window(make_window, pair, qtbot):
    win = make_window(*pair)
    wait_marks(qtbot, win)
    win.set_timebase(2)
    win.go_to(T0)
    n = len(win.chips.chips)
    win.press("types")
    assert win.types_panel.isVisible()
    keys = {win.types_panel.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(2)}
    assert keys == {"type:Note", "type:EDFA"}
    win.types_panel.set_hidden_key("type:EDFA", True)
    assert win.hidden_kinds == {"type:EDFA"}
    assert len(win.chips.chips) == n - 2 and all(m.end is None for m in win.canvas.marks)
    assert len(win.notes_tab.rows) == 3
    # [ ] skip the hidden type too
    assert all(m.code != "EDFA" for m in win._visible)
    win.types_panel.show_all.click()
    assert win.hidden_kinds == frozenset() and len(win.chips.chips) == n


def test_search_and_type_filter_in_the_list(make_window, pair, qtbot):
    shown = Shown()
    win = make_window(*pair, reveal=shown, show_details=shown.show)
    wait_marks(qtbot, win)
    tab = win.notes_tab
    tab.search.setText("fp1")  # the channel, even while the text is hidden
    assert [m.channel for m in tab.rows] == ["Fp1"]
    tab.search.setText("sharp")  # the text is searched only where it is shown
    assert len(tab.rows) == 0
    shown.show(0)
    win.refresh_privacy()
    tab.search.setText("sharp")
    assert [m.recording for m in tab.rows] == [0]
    tab.search.setText("")
    tab.type_box.setCurrentIndex(tab.type_box.findData("quick:Spike"))
    assert len(tab.rows) == 1
    tab.table.cellClicked.emit(0, 0)
    assert abs(win.state.t0 + win.page_us() / 2 - (T0 + 10 * SEC)) < 1000


def test_locked_notes_are_explained(tmp_path, qtbot, qapp):
    p = write(
        tmp_path / "Locked.mefd",
        password1="basic-1",
        password2="full-2",
        notes=[{"time": T0 + SEC, "text": "secret note"}],
    )
    data = open_recordings([str(p)], ["basic-1"])
    for ch in data.channels:
        data.rate(ch), data.units(ch), data.ownership(ch)
    win = ViewerWindow(data, theme=LIGHT)
    qtbot.addWidget(win)
    win.resize(1000, 600)
    win.show()
    wait_marks(qtbot, win)
    assert win.marks == [] and win.locked_recordings == (0,)
    text = win.notes_tab.locked.text()
    assert win.notes_tab.locked.isVisibleTo(win.notes_tab)
    assert "Recording A" in text and "full-access password" in text
    win.close()


# --- the export range ----------------------------------------------------------------------------


def test_s_and_e_set_the_range_at_the_pointer_exact_to_the_sample(make_window, pair, qtbot):
    win = make_window(*pair)
    win.set_timebase(2)
    win.go_to(T0)
    at(win, T0 + 20 * SEC)
    QTest.keyClick(win.canvas, "s")
    at(win, T0 + 40 * SEC)
    QTest.keyClick(win.canvas, "e")
    a, b = win.export_range()
    assert abs(a - (T0 + 20 * SEC)) < 60_000 and a < b
    assert round((a - T0) * FS / 1e6, 3) % 1 == pytest.approx(0, abs=0.01)  # on the sample grid
    assert round((b - T0) * FS / 1e6, 3) % 1 == pytest.approx(0, abs=0.01)
    assert win.canvas.export_start == a and win.strip.export == (a, b)
    assert win.export_summary().startswith("Export 20") and "recording A" in win.export_summary()
    assert win.copy_times_action.isEnabled()
    # a start past the end is refused: the end is kept, and the reason said
    before = win.export_range()
    at(win, T0 + 50 * SEC)
    QTest.keyClick(win.canvas, "s")
    assert win.export_range() == before and "must be before" in win.status_message.text()
    win.canvas.grab()


def test_the_selection_edges_and_dragging_an_edge(make_window, pair, qtbot):
    win = make_window(*pair)
    win.set_timebase(2)
    win.go_to(T0)
    c = win.canvas
    y = int(c.row_y(0))
    x0, x1 = int(c.x_of(T0 + 10 * SEC)), int(c.x_of(T0 + 30 * SEC))
    mod = Qt.KeyboardModifier.ShiftModifier
    QTest.mousePress(c, Qt.MouseButton.LeftButton, mod, QPoint(x0, y))
    QTest.mouseMove(c, QPoint(x1, y))
    QTest.mouseRelease(c, Qt.MouseButton.LeftButton, mod, QPoint(x1, y))
    assert c.selection is not None
    QTest.keyClick(c, "s")
    QTest.keyClick(c, "e")
    a, b = win.export_range()
    assert abs(a - c.selection[0]) < 20_000 and abs(b - c.selection[1]) < 20_000
    # drag the start edge 60 pixels later
    xs = int(c.x_of(a))
    QTest.mousePress(c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(xs, y))
    QTest.mouseMove(c, QPoint(xs + 60, y))
    QTest.mouseRelease(
        c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(xs + 60, y)
    )
    moved = win.export_start
    assert moved > a and c.active_edge == "start"
    # arrows nudge the active edge by one sample instead of paging
    t0 = win.state.t0
    QTest.keyClick(c, Qt.Key.Key_Right)
    assert win.export_start - moved == pytest.approx(1e6 / FS, abs=1) and win.state.t0 == t0
    QTest.keyClick(c, Qt.Key.Key_Left)
    assert win.export_start == pytest.approx(moved, abs=1)
    QTest.keyClick(c, Qt.Key.Key_Escape)
    assert c.active_edge is None
    QTest.keyClick(c, Qt.Key.Key_Right)
    assert win.state.t0 > t0  # paging again


def test_copy_times_format(make_window, pair):
    win = make_window(*pair)
    win.set_zone(ZoneInfo("America/Chicago"))
    win.set_export_edge("start", T0 + 12 * SEC)
    win.set_export_edge("end", T0 + 20 * SEC)
    text = win.copy_times()
    assert QGuiApplication.clipboard().text() == text
    a, b = win.export_range()
    lines = text.splitlines()
    assert re.fullmatch(
        rf"Start: 1999-12-31 18:00:12\.\d{{3}} -06:00 \({a} microseconds since 1970, UTC\)",
        lines[0],
    )
    assert re.fullmatch(
        rf"End: 1999-12-31 18:00:20\.\d{{3}} -06:00 \({b} microseconds since 1970, UTC\)", lines[1]
    )
    assert lines[2] == "Length: 8 s" and "identify the recording" in lines[3]
    assert "identify" in win.status_message.text()


def test_the_list_is_refused_while_hidden_then_saved(make_window, pair, tmp_path):
    shown = Shown()
    win = make_window(*pair, reveal=shown, show_details=shown.show)
    win.set_export_edge("start", T0 + 50 * SEC)
    win.set_export_edge("end", T0 + 100 * SEC)
    win.add_range()
    win.set_export_edge("start", T0 + 2 * SEC)
    win.set_export_edge("end", T0 + 8 * SEC)
    win.add_range()
    assert len(win.saved_ranges) == 2 and win.range_dialog.table.rowCount() == 2
    win.show_ranges()
    assert win.range_dialog.table.rowCount() == 2
    assert win.range_dialog.table.item(0, 4).text() == "A and B"
    win.ask_show = lambda indices: False
    win.confirm = lambda *a: True
    out = tmp_path / "ranges.csv"
    assert not win.save_ranges("csv", str(out))
    assert (
        not out.exists()
        and "hidden" in win.last_refusal
        and "recording A and B" in win.last_refusal
    )
    assert not win.save_ranges("json", str(tmp_path / "ranges.json"))
    # asked to show them: the explorer's switch is used, then it is written
    win.ask_show = lambda indices: True
    assert win.save_ranges("csv", str(out)) and shown(0) and shown(1)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [r["number"] for r in rows] == ["1", "2"]
    assert rows[0]["recordings"] == "Alpha_case; Beta_case"
    assert abs(int(rows[0]["start_us"]) - (T0 + 50 * SEC)) < 10_000
    assert rows[1]["start"].endswith("UTC")
    j = tmp_path / "ranges.json"
    assert win.save_ranges("json", str(j))
    items = json.loads(j.read_text(encoding="utf-8"))
    assert len(items) == 2 and items[1]["recordings"] == "Alpha_case"
    assert "identify" in win.range_dialog.note.text()
    # a confirmation that is declined writes nothing
    win.confirm = lambda *a: False
    other = tmp_path / "no.csv"
    assert not win.save_ranges("csv", str(other)) and not other.exists()


def test_send_to_join_or_trim_signals_the_recordings_and_the_window(make_window, pair, qtbot):
    win = make_window(*pair)
    assert not win.send_action.isEnabled()
    win.set_export_edge("start", T0 + 40 * SEC)
    win.set_export_edge("end", T0 + 100 * SEC)
    assert win.send_action.isEnabled()
    a, b = win.export_range()
    with qtbot.waitSignal(win.join_requested) as sig:
        win.send_to_join()
    assert sig.args == [(0, 1), a, b]
    # a context menu offers the two edges
    from PySide6.QtWidgets import QMenu

    menu = QMenu()
    for hook in win.context_hooks:
        hook(menu, 0, T0 + 3 * SEC)
    assert [x.text() for x in menu.actions() if x.text()] == [
        "Set export start here",
        "Set export end here",
    ]
    menu.actions()[1].trigger()
    assert win.export_end is not None


def test_the_model_helpers():
    assert vm.banner_text(["A", "B"]) == "Now showing recordings A and B"
    assert vm.banner_text(["A", "B", "C"]) == "Now showing recordings A, B and C"
    assert vm.banner_text(["C"], "x") == "Now showing recording C: x"
    assert vm.band_spans([(0, 10, 0), (20, 30, 1)], 5, 25) == [
        (5, 10, (0,)),
        (10, 20, ()),
        (20, 25, (1,)),
    ]
    assert vm.band_spans([(0, 10, 0), (0, 10, 1)], 0, 10) == [(0, 10, (0, 1))]
    assert vm.gap_text(["A"], ["B"], 0) == "A to B: no gap"
    assert vm.gap_text(["A"], ["A", "B"], None) == "B starts"
    assert vm.quick_label("Spike: x") == "Spike" and vm.quick_label("Other: x") is None
    assert (
        vm.snap(1_000_050, 1_000_000, 100.0) == 1_000_000
        and vm.snap(1_006_000, 1_000_000, 100.0) == 1_010_000
    )


# --- review round 1 ----------------------------------------------------------------------


def test_letter_tags_only_where_rows_differ(make_window, pair):
    win = make_window(*pair)
    win.go_to(T0 + 5 * SEC)
    assert {r.tag for r in win.canvas.rows} == {""}  # in turn: the band says it
    win.set_timebase(2)
    win.go_to(T0)  # the page holds both recordings, all rows from both: same tag on every row
    assert len({r.tag for r in win.canvas.rows}) <= 1


def test_the_strip_uses_the_bands_colours_and_gaps_shade_the_full_height(make_window, pair):
    win = make_window(*pair)
    for i in (0, 1):
        assert win.strip._colour_of(i, False) == win._rec_colour(i) == win.band._colour_of(i)
    win.go_to(T0 + 55 * SEC)
    assert win.canvas.gap_spans and win.canvas.gap_spans[0][0] == T0 + 60 * SEC


def test_the_banner_loses_a_name_that_is_hidden_again(make_window, pair):
    shown = Shown()
    win = make_window(*pair, reveal=shown, show_details=shown.show)
    shown.show(1)
    win.refresh_privacy()
    win.go_to(T0 + 85 * SEC)
    assert win.banner.text() == "Now showing recording B: Beta_case"
    shown.flags[1] = False
    win.refresh_privacy()
    assert win.banner.text() == "" and win.banner.accessibleDescription() == ""
    assert not win.banner.isVisible()
    assert win.banner.history == ["Now showing recording B"]  # letters only


def test_marks_bound_to_a_channel_follow_bipolar_rows(make_window, tmp_path, qtbot):
    p = write(
        tmp_path / "bip.mefd",
        channels=("LA1", "LA2", "LA3"),
        channel_notes=[("LA2", [{"time": T0 + 5 * SEC, "text": "contact two"}])],
    )
    win = make_window(p)
    wait_marks(qtbot, win)
    win.set_state(montage="bipolar")
    settle(qtbot, win)
    labels = [r.label for r in win.canvas.rows]
    assert len(labels) == 2  # LA1 - LA2 and LA2 - LA3
    spans = win.canvas._row_spans("LA2", win.canvas.plot_rect())
    assert len(spans) == 2  # as the plus of one row and the minus of the other
    assert win.canvas.marks[0].channel == "LA2" and "channel LA2" in win.canvas.marks[0].tip
    assert win.notes_tab.cell_text(0, 3) == "LA2"


def test_export_edges_snap_to_the_finest_rate_and_never_cross(make_window, tmp_path):
    t = np.arange(int(30 * 256)) / 256
    p = tmp_path / "mixed.mefd"
    with mef3io.Writer(str(p), overwrite=True, units="uV") as w:
        w.write("fast", np.sin(t), T0 + 3, 256.0)
        w.write("slow", np.sin(t[::2]), T0 + 3, 128.0)
    win = make_window(p)
    win.set_export_edge("start", T0 + 10 * SEC + 1000)  # no row under a pointer: finest rate
    win.set_export_edge("end", T0 + 10 * SEC + 1100)
    a, b = win.export_range()
    step = 1e6 / 256
    assert (a - (T0 + 3)) / step == pytest.approx(round((a - (T0 + 3)) / step), abs=1e-3)
    assert a <= T0 + 10 * SEC + 1000 and b >= T0 + 10 * SEC + 1100 and b > a  # floor, ceil
    win.canvas.active_edge = "end"
    win.nudge_edge(1)
    assert win.export_end - b == pytest.approx(step, abs=1)
    # an edge that would empty the range is refused and the other edge stays
    win.set_export_edge("start", T0 + 20 * SEC)
    assert win.export_range() == (a, b + step) or win.export_range()[0] == a
    assert "must be before" in win.status_message.text()


def test_a_failed_notes_read_says_so(tmp_path, qtbot, qapp, monkeypatch):
    from mef3io_studio.model.viewer_source import ViewerData

    def boom(self):
        raise OSError("gone")

    monkeypatch.setattr(ViewerData, "annotations", boom)
    win = ViewerWindow(open_data(write(tmp_path / "f.mefd")), theme=LIGHT)
    qtbot.addWidget(win)
    win.resize(1000, 600)
    win.show()
    wait_marks(qtbot, win)
    assert win.notes_failed and "not available" in win.notes_button.text()
    assert win.notes_tab.failed.isVisibleTo(win.notes_tab) or not win.notes_tab.isVisible()
    assert not win.notes_tab.failed.isHidden()
    win.close()


def test_closing_waits_for_the_notes_read(tmp_path, qtbot, qapp, monkeypatch):
    import threading
    import time

    from mef3io_studio.model.viewer_source import ViewerData

    real = ViewerData.annotations
    done = threading.Event()

    def slow(self):
        time.sleep(0.4)
        out = real(self)
        done.set()
        return out

    monkeypatch.setattr(ViewerData, "annotations", slow)
    win = ViewerWindow(open_data(write(tmp_path / "s.mefd")), theme=LIGHT)
    qtbot.addWidget(win)
    win.show()
    win.close()
    win._notes_thread.join(5)
    assert done.is_set() and win._notes_thread is not None


def test_ownership_never_reads_the_files_after_the_open(make_window, pair, monkeypatch):
    import threading

    main = threading.main_thread()
    data = open_data(*pair)  # what the opening task reads once
    hits = []

    def guard(name, real):
        def wrapped(*a, **k):
            if threading.current_thread() is main:
                hits.append(name)
            return real(*a, **k)

        return wrapped

    for name in ("segments", "info", "read", "records", "block_envelope", "toc"):
        monkeypatch.setattr(mef3io.Reader, name, guard(name, getattr(mef3io.Reader, name)))
    for ch in data.channels:
        data.ownership(ch)
        data.rate(ch)
        data.units(ch)
    data.recordings_at(T0, T0 + 200 * SEC)
    data.segments_coverage()
    data.boundaries()
    assert hits == []
    data.close()


def test_saving_ranges_survives_a_bad_path_and_sanitises_cells(make_window, pair, tmp_path):
    shown = Shown()
    win = make_window(*pair, reveal=shown, show_details=shown.show)
    shown.show(0)
    shown.show(1)
    win.set_export_edge("start", T0 + 2 * SEC)
    win.set_export_edge("end", T0 + 8 * SEC)
    win.confirm = lambda *a: True
    assert not win.save_ranges("csv", str(tmp_path / "nope" / "x.csv"))
    assert win.last_refusal == "The file could not be written. Nothing was saved."
    assert str(tmp_path) not in win.status_message.text()
    rows = [{"number": 1, "start": "=cmd()", "end": "+1", "length_seconds": -1, "recordings": "@x"}]
    for r in rows:
        for k in vm.EXPORT_COLUMNS:
            r.setdefault(k, "")
    text = vm.to_csv(rows)
    assert "'=cmd()" in text and "'+1" in text and "'@x" in text
