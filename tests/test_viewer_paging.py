"""Fast paging: the filtered blocks of several pages ahead are prepared in the
direction of travel, nearest first; reversing drops what looked the old way; an
idle viewer looks further ahead, within a share of the cache budget."""

# ruff: noqa: F811
from __future__ import annotations

import os
import threading

from test_viewer_ui import SEC, T0, make_window, settle, write  # noqa: F401

from mef3io_studio.model import dsp
from mef3io_studio.viewer import controller as ctlmod
from mef3io_studio.workers.viewer_scheduler import LOOKAHEAD

NAMES = [f"E{i}" for i in range(4)]


def view_keys(win) -> set:
    cache = win.controller.cache
    with cache._lock:
        return {k for k in cache._items if k[0] == "view"}


def hold_reads(win, monkeypatch):
    gate = threading.Event()
    real = win.data.read_tile

    def held(*a, **k):
        gate.wait(20)
        return real(*a, **k)

    monkeypatch.setattr(win.data, "read_tile", held)
    ctl = win.controller
    ctl.cache.clear()
    ctl._held.clear()
    ctl._held_bytes = 0
    return gate


def ahead_jobs(ctl):
    return [j for j in ctl._jobs.values() if j.priority == LOOKAHEAD]


def test_the_blocks_of_several_pages_ahead_are_asked_for_nearest_first(
    make_window, tmp_path, qtbot, monkeypatch
):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=600))
    win.set_state(t0=T0 + 100 * SEC, filters=dsp.FilterSettings(0.5, 70.0))
    settle(qtbot, win)
    gate = hold_reads(win, monkeypatch)
    win.page(1)
    ctl = win.controller
    assert ctl.perf.lookahead_pages == 4
    req_start = ctl._req.span[0]
    page = int((ctl._req.span[1] - req_start) / 1.5)
    ahead = sorted({j.span[0] for j in ahead_jobs(ctl) if j.span[0] > req_start})
    assert ahead == [req_start + k * page for k in range(1, 5)]
    ranks = {j.span[0]: j.rank for j in ahead_jobs(ctl) if j.span[0] > req_start}
    assert [ranks[s] for s in ahead] == [1, 2, 3, 4]
    gate.set()
    settle(qtbot, win)


def test_reversing_drops_what_was_prepared_for_the_old_direction_at_once(
    make_window, tmp_path, qtbot, monkeypatch
):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=600))
    win.set_state(t0=T0 + 300 * SEC, filters=dsp.FilterSettings(0.5, 70.0))
    settle(qtbot, win)
    gate = hold_reads(win, monkeypatch)
    ctl = win.controller
    win.page(1)
    forward = {j.span[0] for j in ahead_jobs(ctl)}
    win.page(-1)
    start = ctl._req.span[0]
    page = int((ctl._req.span[1] - start) / 1.5)
    now = {j.span[0] for j in ahead_jobs(ctl)}
    assert all(s <= start + page for s in now), "nothing further ahead than the one page behind"
    assert min(now) == start - 4 * page
    assert max(forward) > start + page  # the forward look-ahead did reach further
    gate.set()
    settle(qtbot, win)


def test_an_idle_viewer_looks_further_ahead_within_a_share_of_the_budget(
    make_window, tmp_path, qtbot
):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=600))
    win.set_state(t0=T0 + 100 * SEC, filters=dsp.FilterSettings(0.5, 70.0))
    settle(qtbot, win)
    ctl = win.controller
    ctl._watch.stop()  # the timer would deepen on its own: step by hand
    assert ctl.idle_before_deepening == ctlmod.IDLE_BEFORE_DEEPENING == 1.0
    assert not ctl._maybe_deepen()  # not idle for a second yet
    ctl.idle_before_deepening = 0.0
    before = len(view_keys(win))
    for _ in range(3):
        assert ctl._maybe_deepen()
        settle(qtbot, win)
    assert ctl._deeper == 3 and len(view_keys(win)) >= before + 3
    # within a share of the budget: a full cache deepens no more
    ctl.cache.set_budget(max(1, ctl.cache.bytes_used))
    assert not ctl._maybe_deepen()
    win.page(1)  # a new page starts from the preference again
    assert ctl._deeper == 0


def test_compute_workers_scale_with_the_machine_and_the_preference_overrides():
    cores = os.cpu_count() or 2
    assert 2 <= ctlmod.default_compute_workers() <= 8
    assert ctlmod.default_compute_workers() == max(2, min(8, cores // 2))
