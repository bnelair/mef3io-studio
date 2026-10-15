"""The combined timeline and "Save times as a table", offscreen, plus the
alignment of every Explorer table's headings with its cells.

Recordings are either real synthetic ones (written with mef3io, opened
through the helper process like in the app) or made-up details handed to
the list directly (for the 400-recording performance check)."""

from __future__ import annotations

import csv
import io
import time

import mef3io
import numpy as np
import pytest
from explorer_sessions import (
    FIRST,
    LAST,
    PATIENT_ID,
    SECOND,
    T0,
    set_time_offset,
    write_encrypted,
    write_plain,
)
from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtWidgets import QApplication

from mef3io_studio.explorer.combined import UNVERIFIED_TAG, CombinedTimelineWidget
from mef3io_studio.explorer.page import EXPORT_WARNING
from mef3io_studio.explorer.state import RecordingEntry
from mef3io_studio.explorer.table import (
    ALIGN_LEFT,
    ALIGN_RIGHT,
    COL_CHANNELS,
    COL_NAME,
    COL_RATES,
    COLUMNS,
)
from mef3io_studio.model.export import LEVEL_PARTS, LEVEL_RECORDINGS
from mef3io_studio.model.jobs import JobStatus
from mef3io_studio.model.recording import (
    ChannelInfo,
    InspectOutcome,
    Part,
    RecordingDetails,
)


@pytest.fixture
def page(window):
    window._closing = True
    window.show()
    window.resize(1280, 800)
    return window.explorer


def wait_loaded(qtbot, page, n, timeout=60_000):
    qtbot.waitUntil(
        lambda: len(page.entries()) == n and all(not e.busy for e in page.entries()),
        timeout=timeout,
    )


def wait_layout(qtbot, page, rows=None, timeout=30_000):
    def ready():
        data = page.combined.layout_data
        if data is None or page._layout_dirty or page._layout_timer.isActive():
            return False
        if page._layout_runner.busy:
            return False
        return rows is None or len(data.rows) == rows

    qtbot.waitUntil(ready, timeout=timeout)
    return page.combined.layout_data


def _write(path, start, seconds=10, channels=("c",)):
    with mef3io.Writer(str(path), overwrite=True) as w:
        for name in channels:
            w.write(name, np.zeros(int(256 * seconds)), start, 256.0)
    return path


def _row_y(widget: CombinedTimelineWidget, index: int) -> float:
    em, row, _gutter, axis = widget._metrics()
    return axis + (index - widget.scrollbar.value()) * row + row / 2


def test_combined_timeline_of_the_list_and_of_a_selection(page, qtbot, tmp_path):
    paths = [
        _write(tmp_path / "b2.mefd", T0 + 3600 * SECOND),
        _write(tmp_path / "a1.mefd", T0),
        _write(tmp_path / "c3.mefd", T0 + 5 * SECOND),  # overlaps a1
    ]
    page.open_paths([str(p) for p in paths], interactive=False)
    wait_loaded(qtbot, page, 3)
    page.select_entry(page.entries()[0])
    page.show_combined(True)
    assert page.detail_stack.currentWidget() is page.combined
    layout = wait_layout(qtbot, page, 3)
    # One selected: every recording in the list, in time order.
    assert [r.name for r in layout.rows] == ["a1", "c3", "b2"]
    assert "every recording in the list" in page.combined.caption.text()
    assert page.detail_heading.text() == "3 recordings on one timeline"
    assert [(o.first, o.second) for o in layout.overlaps] == [
        (page.model.entries[1].key, page.model.entries[2].key)
    ]
    assert "cover the same time" in page.combined.overlap_label.text()
    assert page.combined.overlap_box.isVisibleTo(page)
    assert "2 gaps" not in page.combined.summary.text()
    assert "1 gap between recordings" in page.combined.summary.text()

    # Two selected: only those.
    page.table.selectAll()
    model = page.table.selectionModel()
    model.select(
        page.proxy.index(2, 0),
        model.SelectionFlag.Deselect | model.SelectionFlag.Rows,
    )
    layout = wait_layout(qtbot, page, 2)
    assert "selected in the list" in page.combined.caption.text()

    # Clicking a row moves the current recording but keeps the selection.
    widget = page.combined.timeline
    second = layout.rows[1]
    qtbot.mouseClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(10, int(_row_y(widget, 1))))
    assert page.current_entry().key == second.key
    assert len(page.table.selectionModel().selectedRows()) == 2
    assert widget.current_key == second.key
    # Double-click: that recording's details.
    qtbot.mouseDClick(widget, Qt.MouseButton.LeftButton, pos=QPoint(10, int(_row_y(widget, 0))))
    assert page.details_mode_button.isChecked()
    assert page.detail_stack.currentWidget() is page.tabs
    assert page.current_entry().key == layout.rows[0].key


def test_zoom_pan_fit_and_tooltips(page, qtbot, tmp_path):
    path = write_plain(tmp_path / "rec.mefd", notes=False)
    other = _write(tmp_path / "later.mefd", T0 + 600 * SECOND)
    page.open_paths([str(path), str(other)], interactive=False)
    wait_loaded(qtbot, page, 2)
    page.entries()[0].revealed = True  # even with Show, no patient details here
    page.show_combined(True)
    wait_layout(qtbot, page, 2)
    widget = page.combined.timeline
    span = widget.v1 - widget.v0
    widget.zoom(4.0)
    assert widget.v1 - widget.v0 == pytest.approx(span / 4, rel=0.01)
    v0 = widget.v0
    widget.pan_pixels(-100)
    assert widget.v0 > v0
    page.combined.fit_button.click()
    assert widget.v1 - widget.v0 == span
    # Mouse wheel over the bars zooms in around the pointer.
    from PySide6.QtGui import QWheelEvent

    plot = widget.plot_rect()
    pos = QPointF(plot.center().x(), _row_y(widget, 0))
    event = QWheelEvent(
        pos,
        widget.mapToGlobal(pos),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)
    assert widget.v1 - widget.v0 < span
    page.combined.fit_button.click()
    # Tooltips: the name, times with the zone, channels with data.
    row = page.combined.layout_data.rows[0]
    x = plot.left() + (row.first + 2 * SECOND - widget.v0) / (widget.v1 - widget.v0) * plot.width()
    tip = widget.tooltip_at(QPointF(x, _row_y(widget, 0)))
    assert tip.startswith("rec\n") and "2000-01-01 00:00:00 UTC" in tip
    assert "All 3 channels have data here" in tip
    gap_x = plot.left() + (300 * SECOND + T0 - widget.v0) / (widget.v1 - widget.v0) * plot.width()
    assert widget.tooltip_at(QPointF(gap_x, _row_y(widget, 0))) == ""  # past its end
    gutter_tip = widget.tooltip_at(QPointF(5, _row_y(widget, 1)))
    assert gutter_tip.startswith("later\n") and "with data" in gutter_tip
    texts = [page.combined.summary.text(), page.combined.caption.text(), tip, gutter_tip]
    for secret in (FIRST, LAST, PATIENT_ID):
        assert not any(secret in t for t in texts)
    # The site time zone is used, with its offset.
    page.set_zone_name("America/Chicago")
    tip = widget.tooltip_at(QPointF(x, _row_y(widget, 0)))
    assert "1999-12-31 18:00:00 -06:00" in tip
    assert "-06:00" in page.combined.summary.text()


def test_locked_unreadable_and_unverified_rows(page, qtbot, tmp_path):
    from explorer_sessions import damage_details

    good = _write(tmp_path / "good.mefd", T0)
    masked = set_time_offset(_write(tmp_path / "masked.mefd", T0), 86_400 * SECOND)
    locked = write_encrypted(tmp_path / "locked.mefd")
    broken = damage_details(_write(tmp_path / "broken.mefd", T0))
    page.open_paths([str(p) for p in (good, masked, locked, broken)], interactive=False)
    wait_loaded(qtbot, page, 4)
    page.show_combined(True)
    layout = wait_layout(qtbot, page, 4)
    by = {r.name: r for r in layout.rows}
    assert [r.name for r in layout.rows][:2] == ["good", "masked"]
    assert not by["masked"].verified and by["masked"].uses_time_offset
    assert by["locked"].status == "locked" and not by["locked"].has_data
    assert by["broken"].status == "unreadable"
    summary = page.combined.summary.text()
    assert "Not counted: 1 recording with unverified times" in summary
    assert "2 recordings with nothing to draw" in summary
    # The unverified row does not count as overlapping the good one.
    assert layout.overlaps == ()
    widget = page.combined.timeline
    tips = {
        r.name: widget.tooltip_at(QPointF(5, _row_y(widget, i))) for i, r in enumerate(layout.rows)
    }
    assert "Password needed" in tips["locked"]
    assert "Could not be opened" in tips["broken"]
    assert "Unverified time" in tips["masked"]
    widget.grab()  # paints every kind of row without trouble
    assert UNVERIFIED_TAG == "unverified time"


def test_layout_runs_in_short_slices(page, qtbot, tmp_path, monkeypatch):
    """The layout is spread over slices of a few milliseconds between other
    events (see workers/slices.py), and a newer request drops an older one."""
    from mef3io_studio.workers import slices

    longest = []
    real = slices.SlicedRunner._slice

    def timed(self):
        start = time.perf_counter()
        real(self)
        longest.append(time.perf_counter() - start)

    monkeypatch.setattr(slices.SlicedRunner, "_slice", timed)
    page.model.add(_synthetic(60, channels=8, parts=200))
    page._refresh_body()
    page.show_combined(True)
    page._layout_dirty = True
    page._start_layout()  # starts again at once: the first one is dropped
    wait_layout(qtbot, page, 60, timeout=120_000)
    assert len(longest) > 5
    assert max(longest) < 0.1, max(longest)


def test_page_fills_the_window_in_combined_mode(window, qtbot, tmp_path):
    window._closing = True
    window.show()
    page = window.explorer
    paths = [_write(tmp_path / f"r{i}.mefd", T0 + i * 3600 * SECOND) for i in range(3)]
    page.open_paths([str(p) for p in paths], interactive=False)
    wait_loaded(qtbot, page, 3)
    page.show_combined(True)
    for size in ((1280, 800), (1400, 900)):
        window.resize(*size)
        wait_layout(qtbot, page, 3)
        qtbot.wait(100)
        assert not page.scroll.verticalScrollBar().isVisible(), size
        # Every row fits, and the sidebar keeps its width.
        assert page.combined.timeline.visible_rows() >= 3
    width = window.width()
    page.show_combined(False)
    qtbot.wait(50)
    assert window.width() == width


# -- Save times as a table -----------------------------------------------------


def _read_csv(path):
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
    return [dict(zip(rows[0], r, strict=True)) for r in rows[1:]]


def test_save_times_as_a_table(page, qtbot, tmp_path):
    plain = write_plain(tmp_path / "=evil.mefd", notes=False)
    locked = write_encrypted(tmp_path / "locked.mefd")
    page.open_paths([str(plain), str(locked)], interactive=False)
    wait_loaded(qtbot, page, 2)
    asked = []
    page.confirm_export = lambda count, revealed: asked.append((count, revealed)) or False
    out = tmp_path / "out" / "times.csv"
    out.parent.mkdir()
    assert page.export_times(LEVEL_RECORDINGS, path=str(out)) is None
    assert asked == [(2, 0)] and not out.exists()  # refused: nothing written

    page.confirm_export = lambda count, revealed: asked.append((count, revealed)) or True
    handle = page.export_times(LEVEL_RECORDINGS, path=str(out))
    with qtbot.waitSignal(handle.finished, timeout=30_000):
        pass
    assert handle.record.status is JobStatus.SUCCEEDED
    assert "evil" not in handle.record.title  # no recording named in the task
    rows = {r["file_name"]: r for r in _read_csv(out)}
    evil = rows["'=evil.mefd"]  # neutralised: never a formula
    assert evil["recording"] == "'=evil" and evil["status"] == "opened"
    assert evil["start_utc"] == "2000-01-01T00:00:00Z"
    assert evil["patient_first_name"] == "[hidden]"
    assert rows["locked.mefd"]["status"] == "password needed"
    assert FIRST not in out.read_text(encoding="utf-8-sig")
    assert [p.name for p in out.parent.iterdir()] == ["times.csv"]  # no temp file left

    # Show chosen for one recording: its details are included, and the
    # warning said so.
    entry = next(e for e in page.entries() if e.details is not None)
    entry.revealed = True
    page.set_zone_name("Europe/Prague")
    handle = page.export_times(LEVEL_PARTS, path=str(out))
    with qtbot.waitSignal(handle.finished, timeout=30_000):
        pass
    assert asked[-1] == (2, 1)
    parts = _read_csv(out)
    assert len(parts) == 3 + 1  # three channels' parts, one row for the locked one
    assert parts[0]["start_site_time"] == "2000-01-01T01:00:00+01:00"
    handle = page.export_times(LEVEL_RECORDINGS, path=str(out))
    with qtbot.waitSignal(handle.finished, timeout=30_000):
        pass
    rows = {r["file_name"]: r for r in _read_csv(out)}
    assert rows["'=evil.mefd"]["patient_first_name"] == FIRST
    assert rows["'=evil.mefd"]["start_site_time"] == "2000-01-01T01:00:00+01:00"


def test_the_export_warning_is_plain(page, qtbot, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    seen = {}

    def fake_exec(box):
        seen["text"] = box.text()
        seen["more"] = box.informativeText()
        seen["default"] = box.defaultButton().text()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    assert page.confirm_export(3, 1) is False
    assert seen["text"] == EXPORT_WARNING
    assert "patient details" in seen["more"] and seen["default"] == "Don't save"


# -- Performance ---------------------------------------------------------------


def _synthetic(n_recordings=400, channels=8, parts=50):
    """Made-up details: no files. 400 recordings x 8 channels x 50 parts =
    160,000 parts, a few overlapping each other."""
    entries = []
    for r in range(n_recordings):
        t0 = T0 + r * 3 * 86_400 * SECOND
        chans = []
        for c in range(channels):
            ps = tuple(
                Part(
                    i,
                    t0 + i * 700 * SECOND,
                    t0 + i * 700 * SECOND + 600 * SECOND,
                    600 * 256 - (0 if (i + c) % 7 else 1000),
                    1,
                )
                for i in range(parts if c < channels - 1 else parts // 2)
            )
            chans.append(ChannelInfo(f"c{c}", 256.0, "uV", 0, ps[0].start, ps[-1].end, ps))
        location = f"/made/up/r{r:03d}.mefd"
        details = RecordingDetails(location, tuple(chans), recording_time_offset=0 if r % 50 else 7)
        entry = RecordingEntry(location=location)
        entry.outcome = InspectOutcome(location, details=details)
        entries.append(entry)
    return entries


def test_four_hundred_recordings_never_stall_the_window(page, qtbot):
    page.model.add(_synthetic())
    page._refresh_body()
    page.select_entry(page.entries()[0])
    ticks = []
    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    qtbot.wait(50)

    def max_gap(action):
        start = len(ticks)
        action()
        qtbot.wait(60)
        window = ticks[max(0, start - 1) :]
        return max(b - a for a, b in zip(window, window[1:], strict=False))

    widget = page.combined.timeline

    def show():
        page.show_combined(True)
        wait_layout(qtbot, page, 400, timeout=120_000)
        widget.repaint()

    def steps(action):
        # One user step at a time (one wheel turn, one drag move), each
        # painted before the next, like real input.
        def run():
            worst = 0.0
            for _ in range(12):
                worst = max(worst, max_gap(action))
            return worst

        return run

    def zoom():
        widget.zoom(1.6)
        widget.repaint()

    def pan():
        widget.pan_pixels(-80)
        widget.repaint()

    def scroll():
        widget.scrollbar.setValue(widget.scrollbar.value() + 30)
        widget.repaint()

    def select_all():
        page.table.selectAll()
        wait_layout(qtbot, page, 400, timeout=120_000)
        widget.repaint()

    gaps = {
        "show": max_gap(show),
        "zoom": steps(zoom)(),
        "pan": steps(pan)(),
        "scroll": steps(scroll)(),
        "fit": max_gap(lambda: (widget.fit_all(), widget.repaint())),
        "select": max_gap(select_all),
    }
    timer.stop()
    layout = page.combined.layout_data
    assert len(layout.rows) == 400 and len(layout.unverified) == 8
    assert max(gaps.values()) < 0.25, gaps


# -- Table alignment -------------------------------------------------------------


def _explorer_table_models(page):
    return {
        "recordings": page.model,
        "channels": page.channels.table.rows,
        "notes": page.notes.table.rows,
    }


def test_every_heading_is_aligned_like_its_cells(page, qtbot, tmp_path):
    page.open_paths([str(write_plain(tmp_path / "al.mefd"))], interactive=False)
    wait_loaded(qtbot, page, 1)
    page.select_entry(page.entries()[0])
    horizontal = int(Qt.AlignmentFlag.AlignHorizontal_Mask)
    for name, model in _explorer_table_models(page).items():
        assert model.rowCount() > 0, name
        for col in range(model.columnCount()):
            head = model.headerData(
                col, Qt.Orientation.Horizontal, Qt.ItemDataRole.TextAlignmentRole
            )
            cell = model.data(model.index(0, col), Qt.ItemDataRole.TextAlignmentRole)
            assert head is not None and cell is not None, (name, col)
            assert int(head) & horizontal == int(cell) & horizontal, (name, col)
            assert int(cell) & int(Qt.AlignmentFlag.AlignVCenter), (name, col)
    assert (
        page.model.headerData(
            COL_CHANNELS, Qt.Orientation.Horizontal, Qt.ItemDataRole.TextAlignmentRole
        )
        == ALIGN_RIGHT
    )
    assert (
        page.model.headerData(
            COL_NAME, Qt.Orientation.Horizontal, Qt.ItemDataRole.TextAlignmentRole
        )
        == ALIGN_LEFT
    )
    assert len(COLUMNS) == page.model.columnCount()


def _ink_edges(image, x0, x1, y0, y1):
    """The leftmost and rightmost columns in the box that hold text (pixels
    far from the box's background colour)."""
    bg = image.pixelColor(x0 + 1, y0 + 1)
    found = []
    for x in range(x0, x1):
        for y in range(y0, y1):
            c = image.pixelColor(x, y)
            if (
                abs(c.red() - bg.red()) + abs(c.green() - bg.green()) + abs(c.blue() - bg.blue())
                > 200
            ):
                found.append(x)
                break
    return (min(found), max(found)) if found else None


def test_headings_line_up_with_cells_on_screen(qapp, page, qtbot, tmp_path):
    """Rendered offscreen with the Fusion style (what the app uses): for
    each column of the recordings list, the heading's text and a cell's
    text start (left-aligned) or end (right-aligned) at the same place."""
    # The app sets Fusion; with its style sheet on top Qt reports the
    # style-sheet wrapper around it.
    assert qapp.style().metaObject().className() in ("QFusionStyle", "QStyleSheetStyle")
    rec = tmp_path / "rates.mefd"
    with mef3io.Writer(str(rec), overwrite=True) as w:
        w.write("fast", np.zeros(8000), T0, 8000.0)
        w.write("slow", np.zeros(512), T0, 512.0)
    page.open_paths([str(rec)], interactive=False)
    wait_loaded(qtbot, page, 1)
    page.table.clearSelection()
    qtbot.wait(100)
    table = page.table
    header = table.horizontalHeader()
    # Wider than their text, so a heading drawn centred (Qt's default)
    # would land far from cells drawn at one side.
    header.setStretchLastSection(False)
    for col in range(len(COLUMNS)):
        header.resizeSection(col, header.sectionSize(col) + 80)
    qtbot.wait(50)
    row_h = table.rowHeight(0)
    assert page.model.display(page.entries()[0], COL_RATES) == "512 Hz / 8 kHz"
    checked = 0
    for col in range(len(COLUMNS)):
        table.scrollTo(page.proxy.index(0, col))
        qtbot.wait(20)
        head_img = header.grab().toImage()
        body_img = table.viewport().grab().toImage()
        x0 = max(0, header.sectionViewportPosition(col))
        x1 = min(body_img.width(), x0 + header.sectionSize(col))
        head = _ink_edges(head_img, x0 + 1, x1 - 1, 2, head_img.height() - 2)
        cell = _ink_edges(body_img, x0 + 1, x1 - 1, 2, row_h - 2)
        if head is None or cell is None:
            continue  # an empty cell
        align = page.model.data(page.model.index(0, col), Qt.ItemDataRole.TextAlignmentRole)
        side = 1 if align == ALIGN_RIGHT else 0
        # Fusion pads headings a few pixels more than cells.
        assert abs(head[side] - cell[side]) <= 10, (COLUMNS[col], head, cell)
        checked += 1
    assert checked >= 6
