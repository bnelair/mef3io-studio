"""The viewer and "Join or trim" together, in the whole app window: the
viewer's export range reaches the joiner prefilled, and the joiner can open
the viewer on its recordings."""

from __future__ import annotations

import mef3io
import numpy as np
from explorer_sessions import metadata
from test_anonymizer_ui import open_in_explorer
from test_viewer_ui import settle

from mef3io_studio.joiner.page import STEP_KEEP, STEP_RECORDINGS
from mef3io_studio.model.join import KEEP_BETWEEN

T0 = 946_684_800_000_000
HOUR = 3_600_000_000


def write(path, start, seconds=2 * 3600, fs=2.0):  # seconds may end mid-second
    n = int(seconds * fs)
    with mef3io.Writer(str(path), overwrite=True, metadata=metadata()) as w:
        for name in ("a1", "a2"):
            w.write(name, np.arange(n, dtype=float), start, fs)
    return path


def open_viewer(window, qtbot):
    page = window.explorer
    qtbot.waitUntil(lambda: all(not e.busy for e in page.entries()), timeout=60_000)
    page.table.selectAll()
    page.view_button.click()
    qtbot.waitUntil(lambda: bool(window.viewers), timeout=60_000)
    win = window.viewers[0]
    qtbot.waitUntil(lambda: win._ready, timeout=10_000)
    settle(qtbot, win)
    return win


def test_send_to_join_or_trim_prefills_the_joiner(window, qtbot, tmp_path):
    window._closing = True
    window.show()
    a = write(tmp_path / "A.mefd", T0)
    b = write(tmp_path / "B.mefd", T0 + 3 * HOUR)
    open_in_explorer(qtbot, window, [a, b])
    win = open_viewer(window, qtbot)
    win.set_export_edge("start", T0 + HOUR + 30)  # 30 microseconds into the hour
    win.set_export_edge("end", T0 + 3 * HOUR + 1800 * 1_000_000)
    win.send_action.trigger()
    joiner = window.joiner
    assert window.pages.currentWidget() is joiner and joiner.step == STEP_KEEP
    rows = joiner.keep.rows
    assert [r.entry.name for r in rows] == ["A", "B"]
    assert all(r.how.currentData() == KEEP_BETWEEN for r in rows)
    zone = joiner.zone
    # whole seconds: the start rounded down, the end up; clamped to each recording
    assert rows[0].start.us(zone) == T0 + HOUR and rows[0].end.us(zone) == T0 + 2 * HOUR
    assert rows[1].start.us(zone) == T0 + 3 * HOUR
    assert rows[1].end.us(zone) == T0 + 3 * HOUR + 1800 * 1_000_000
    assert joiner.keep.ok  # the part is valid and checked on this page
    win.close()
    qtbot.waitUntil(lambda: not window.viewers, timeout=5000)


def test_the_joiner_opens_the_viewer_on_its_recordings(window, qtbot, tmp_path):
    window._closing = True
    window.show()
    a = write(tmp_path / "A.mefd", T0)
    b = write(tmp_path / "B.mefd", T0 + 3 * HOUR)
    open_in_explorer(qtbot, window, [a, b])
    window.module_list.setCurrentRow(window.module_keys.index("joiner"))
    page = window.joiner
    page.go(STEP_RECORDINGS)
    assert not page.view_button.isHidden() and not page.view_button.isEnabled()
    page.recordings.set_ticked([e.key for e in page.recordings.rows])
    page._update_buttons()
    assert page.view_button.isEnabled()
    page.view_button.click()
    qtbot.waitUntil(lambda: bool(window.viewers), timeout=60_000)
    win = window.viewers[0]
    assert win.multi and [r.letter for r in win.data.recordings] == ["A", "B"]
    win.close()
    qtbot.waitUntil(lambda: not window.viewers, timeout=5000)


def test_a_recording_ending_mid_second_is_kept_to_its_end(window, qtbot, tmp_path):
    window._closing = True
    window.show()
    a = write(tmp_path / "A.mefd", T0, seconds=3600.5)
    open_in_explorer(qtbot, window, [a])
    qtbot.waitUntil(lambda: all(not e.busy for e in window.explorer.entries()), timeout=60_000)
    entry = window.explorer.entries()[0]
    end = entry.details.end
    assert end % 1_000_000 != 0
    window.module_list.setCurrentRow(window.module_keys.index("joiner"))
    page = window.joiner
    assert page.choose_window([entry.key], T0 + 10 * 1_000_000 + 500, end)
    row = page.keep.rows[0]
    assert row.keep().end_us >= end  # never floored below the requested end
    assert row.keep().start_us <= T0 + 10 * 1_000_000 + 500
    note = page.keep.viewer_note
    assert not note.isHidden() and "whole seconds" in note.text()
    page.keep.recompute()
    assert not note.isHidden() and "From the viewer" in note.text()  # survives the recompute


def test_hiding_in_explore_reaches_an_open_viewer_at_once(window, qtbot, tmp_path):
    window._closing = True
    window.show()
    a = write(tmp_path / "Case_A.mefd", T0)
    b = write(tmp_path / "Case_B.mefd", T0 + 3 * HOUR)
    open_in_explorer(qtbot, window, [a, b])
    win = open_viewer(window, qtbot)
    entries = window.explorer.entries()
    assert win.recording_label(0) == "Recording A"
    for e in entries:
        e.revealed = True
    window.explorer.refresh_entry(entries[0])
    assert win.recording_label(0).startswith("Recording A: ")
    assert win.recordings_tab.cell_text(0, 1) != "(hidden)"
    for e in entries:
        e.revealed = False
    window.explorer.refresh_entry(entries[0])  # Hide, without touching the viewer
    assert win.recording_label(0) == "Recording A"
    assert win.recordings_tab.cell_text(0, 1) == "(hidden)"
    assert "Case" not in win.windowTitle()
    win.close()
    qtbot.waitUntil(lambda: not window.viewers, timeout=5000)
