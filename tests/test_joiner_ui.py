"""The "Join or trim" page, offscreen: recordings from the Explore list,
what to keep of each (in time order, with the preview), overlaps stopping
the run, and a real join through the helper process."""

from __future__ import annotations

import os

import mef3io
import numpy as np
import pytest
from explorer_sessions import metadata
from test_anonymizer_ui import open_in_explorer

from mef3io_studio.joiner.page import (
    KEEP_FIRST,
    KEEP_LAST,
    STEP_CHANNELS,
    STEP_KEEP,
    STEP_NEW,
    STEP_RECORDINGS,
    STEP_RUN,
)
from mef3io_studio.model.join import PARTS_PREFIX

T0 = 946_684_800_000_000
HOUR = 3_600_000_000


def write(path, start, seconds=2 * 3600, fs=2.0):
    n = int(seconds * fs)
    with mef3io.Writer(str(path), overwrite=True, metadata=metadata()) as w:
        for name in ("a1", "a2"):
            w.write(name, np.arange(n, dtype=float), start, fs)
    return path


@pytest.fixture
def window_page(window):
    window._closing = True
    window.show()
    window.module_list.setCurrentRow(window.module_keys.index("joiner"))
    return window, window.joiner


def choose_all(page):
    page.go(STEP_RECORDINGS)
    page.recordings.set_ticked([e.key for e in page.recordings.rows])
    page.next()
    assert page.step == STEP_KEEP


def test_the_page_is_in_the_navigation(window):
    from mef3io_studio.joiner import JoinerPage

    index = window.module_keys.index("joiner")
    assert isinstance(window.page_widgets[index], JoinerPage)
    assert window.module_list.item(index).text() == "Join or trim"
    # Built only when first used, like the Anonymize page.
    assert not window.joiner.built


def test_keep_choices_preview_and_overlap(window_page, qtbot, tmp_path):
    window, page = window_page
    # B is opened first but starts later: the list is in time order.
    b = write(tmp_path / "B.mefd", T0 + HOUR)
    a = write(tmp_path / "A.mefd", T0)
    open_in_explorer(qtbot, window, [b, a])
    before = sorted(p.name for p in tmp_path.rglob("*"))
    choose_all(page)
    keep = page.keep
    assert [r.entry.name for r in keep.rows] == ["A", "B"]
    assert keep.table.item(0, 0).text() == "A"
    # Both whole: they overlap by an hour. Start is not possible, and why.
    assert not keep.ok and not page.next_button.isEnabled()
    assert "overlap" in keep.problems.text()
    assert all(r[4] for r in keep.preview.rows)
    # The first 90 minutes of A and the last hour of B follow each other.
    keep.rows[0].set_how(KEEP_FIRST)
    keep.rows[0].duration.set_seconds(5400)
    assert not keep.ok  # A's first 90 minutes still overlap all of B
    keep.rows[1].set_how(KEEP_LAST)
    keep.rows[1].duration.set_seconds(3600)
    assert keep.ok and page.next_button.isEnabled()
    assert keep.problems.isHidden()
    spans = [r[3] for r in keep.preview.rows]
    assert [(s.start, s.end) for s in spans] == [
        (T0, T0 + 3 * HOUR // 2),
        (T0 + 2 * HOUR, T0 + 3 * HOUR),
    ]
    assert not any(r[4] for r in keep.preview.rows)
    assert keep.table.item(0, 4).text() == "1 h 30 min"
    assert "2 h 30 min" in keep.summary.text()
    # A duration of zero is refused on the spot.
    keep.rows[1].duration.set_seconds(0)
    assert not keep.ok and "more than zero" in keep.problems.text()
    keep.rows[1].duration.set_seconds(3600)
    # The choices survive a change of time zone (same instants).
    page.set_zone_name("Europe/Prague")
    assert [r.keep().how for r in keep.rows] == [KEEP_FIRST, KEEP_LAST]
    assert keep.ok
    # Back and forth writes nothing.
    page.back()
    assert page.step == STEP_RECORDINGS
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_join_two_recordings_end_to_end(window_page, qtbot, tmp_path):
    window, page = window_page
    a = write(tmp_path / "A.mefd", T0)
    b = write(tmp_path / "B.mefd", T0 + 3 * HOUR)
    open_in_explorer(qtbot, window, [a, b])
    choose_all(page)
    page.keep.rows[0].set_how(KEEP_LAST)
    page.keep.rows[0].duration.set_seconds(3600)
    page.keep.rows[1].set_how(KEEP_FIRST)
    page.keep.rows[1].duration.set_seconds(1800)
    page.next()
    assert page.step == STEP_CHANNELS
    page.picker.click("a2", False)
    page.next()
    assert page.step == STEP_NEW
    out = tmp_path / "out"
    out.mkdir()
    page.new.folder = str(out)
    page.new.name.setText("joined")
    assert page.new.problem_text() == ""
    assert page.next_button.text() == "Check and save"
    check = page.next()
    with qtbot.waitSignal(check.finished, timeout=120_000):
        pass
    assert page.step == STEP_RUN, page.new.findings.count()
    with qtbot.waitSignal(page.handle.finished, timeout=120_000):
        pass
    assert page.result is not None and page.result.ok, page.result
    assert page.run_step.heading.text() == "Saved and checked"
    assert page.run_step.open_button.isVisibleTo(page)
    dst = out / "joined.mefd"
    assert mef3io.Reader(str(dst)).channels == ["a1"]
    found = mef3io.time_range(str(dst))
    assert (found.start_uutc, found.end_uutc) == (T0 + HOUR, T0 + 3 * HOUR + HOUR // 2)
    assert not [p for p in os.listdir(out) if p.startswith(PARTS_PREFIX) or "partial" in p]
    # "Open it in Explore" lists the new recording there.
    page.open_result()
    assert window.pages.currentWidget() is window.explorer
    qtbot.waitUntil(
        lambda: any(e.name == "joined" and not e.busy for e in window.explorer.entries()),
        timeout=60_000,
    )


def test_a_refusal_is_shown_before_anything_is_written(window_page, qtbot, tmp_path):
    window, page = window_page
    a = write(tmp_path / "A.mefd", T0)
    b = write(tmp_path / "B.mefd", T0 + 3 * HOUR, fs=4.0)  # another sampling rate
    open_in_explorer(qtbot, window, [a, b])
    choose_all(page)
    page.next()
    page.next()
    assert page.step == STEP_NEW
    page.new.name.setText("joined")
    before = sorted(p.name for p in tmp_path.rglob("*"))
    check = page.next()
    with qtbot.waitSignal(check.finished, timeout=120_000):
        pass
    assert page.step == STEP_NEW and page.handle is None
    assert page.new.findings.count() == 1
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_one_recording_kept_whole_needs_a_change(window_page, qtbot, tmp_path):
    window, page = window_page
    a = write(tmp_path / "A.mefd", T0)
    open_in_explorer(qtbot, window, [a])
    choose_all(page)
    assert page.keep.ok
    page.next()
    page.next()
    assert page.step == STEP_NEW
    assert page.new.name.text() == "A-part"
    assert "Nothing would change" in page.new.problem_text()
    assert not page.next_button.isEnabled()
    page.back()
    page.picker.click("a2", False)
    page.next()
    assert page.new.problem_text() == "" and page.next_button.isEnabled()
