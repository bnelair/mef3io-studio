"""Every real way into Join or trim hands Explore's facts to the request, so
the check and the run never read times, open a Reader, read block envelopes
or walk folders again. Synthetic recordings only.

The one case with no facts: Explore could not confirm a recording's times
(protected, full-access password not given); then that recording alone is
opened, once (see ``JoinPage.known_layouts``)."""

from __future__ import annotations

import mef3io
import pytest
from test_anonymizer_ui import open_in_explorer
from test_join_anonymous import HOUR, T0, write
from test_joiner_ui import choose_all, wait_for_map

from mef3io_studio.joiner.page import MODE_EACH, STEP_KEEP
from mef3io_studio.model import join as j


@pytest.fixture
def window_page(window):
    window._closing = True
    window.show()
    window.module_list.setCurrentRow(window.module_keys.index("joiner"))
    return window, window.joiner


def spy_on_reads(monkeypatch) -> dict:
    """Count every call that would read a recording again."""
    calls = {"time_range": 0, "reader": 0, "block_envelope": 0, "walk": 0, "read_time": 0}

    def counted(name, real):
        def f(*a, **k):
            calls[name] += 1
            return real(*a, **k)

        return f

    real_env = mef3io.Reader.block_envelope
    monkeypatch.setattr(
        mef3io.Reader,
        "block_envelope",
        lambda self, *a, **k: (
            (calls.__setitem__("block_envelope", calls["block_envelope"] + 1))
            or real_env(self, *a, **k)
        ),
        raising=False,
    )
    monkeypatch.setattr(mef3io, "time_range", counted("time_range", mef3io.time_range))
    monkeypatch.setattr(mef3io, "Reader", counted("reader", mef3io.Reader))
    monkeypatch.setattr(j, "read_time", counted("read_time", j.read_time))
    monkeypatch.setattr(j, "_folder_bytes", counted("walk", j._folder_bytes))
    return calls


def check_and_run(page, tmp_path, monkeypatch, name):
    page.new.name.setText(name)
    (tmp_path / "out").mkdir(exist_ok=True)
    page.new.folder = str(tmp_path / "out")
    request = page.make_request()
    assert request.layouts and set(request.layouts) == {s.key for s in request.sources}
    assert all(request.layouts[k].total_bytes for k in request.layouts)  # sizes from Explore
    calls = spy_on_reads(monkeypatch)
    assert j.check_join(request) == []
    assert j.facts_still_true(request)
    result = j.make_joined(request)
    assert result.ok, result.message
    assert calls == dict.fromkeys(calls, 0), calls


def two(qtbot, window, tmp_path):
    a = write(tmp_path / "A.mefd", T0, seconds=600)
    b = write(tmp_path / "B.mefd", T0 + HOUR, seconds=600)
    open_in_explorer(qtbot, window, [a, b])
    qtbot.waitUntil(lambda: all(not e.busy for e in window.explorer.entries()), timeout=60_000)
    return a, b


def test_explore_several_recordings_window_mode(window_page, qtbot, tmp_path, monkeypatch):
    window, page = window_page
    two(qtbot, window, tmp_path)
    choose_all(page)
    assert page.keep.mode != MODE_EACH
    check_and_run(page, tmp_path, monkeypatch, "w")


def test_explore_several_recordings_each_mode(window_page, qtbot, tmp_path, monkeypatch):
    window, page = window_page
    two(qtbot, window, tmp_path)
    choose_all(page)
    page.keep.mode_each.setChecked(True)
    check_and_run(page, tmp_path, monkeypatch, "e")


def test_explore_one_recording(window_page, qtbot, tmp_path, monkeypatch):
    window, page = window_page
    a = write(tmp_path / "A.mefd", T0, seconds=600)
    open_in_explorer(qtbot, window, [a])
    qtbot.waitUntil(lambda: all(not e.busy for e in window.explorer.entries()), timeout=60_000)
    key = window.explorer.entries()[0].key
    assert page.choose([key])
    page.next()
    wait_for_map(page)
    page.keep.mode_each.setChecked(True)
    row = page.keep.rows[0]
    row.set_how(j.KEEP_BETWEEN)  # trimmed
    row.start.set_us(T0 + 60 * 1_000_000, page.zone)
    row.end.set_us(T0 + 300 * 1_000_000, page.zone)
    page.keep.recompute()
    check_and_run(page, tmp_path, monkeypatch, "one")


def test_viewer_send_several_recordings(window_page, qtbot, tmp_path, monkeypatch):
    window, page = window_page
    two(qtbot, window, tmp_path)
    keys = [e.key for e in window.explorer.entries()]
    assert page.choose_window(keys, T0 + 60 * 1_000_000, T0 + HOUR + 120 * 1_000_000)
    page.next()
    assert page.step == STEP_KEEP
    check_and_run(page, tmp_path, monkeypatch, "v2")


def test_viewer_send_one_recording(window_page, qtbot, tmp_path, monkeypatch):
    window, page = window_page
    a = write(tmp_path / "A.mefd", T0, seconds=600)
    open_in_explorer(qtbot, window, [a])
    qtbot.waitUntil(lambda: all(not e.busy for e in window.explorer.entries()), timeout=60_000)
    key = window.explorer.entries()[0].key
    assert page.choose_window([key], T0 + 60 * 1_000_000, T0 + 300 * 1_000_000)
    page.next()
    check_and_run(page, tmp_path, monkeypatch, "v1")
