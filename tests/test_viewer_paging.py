"""Fast paging: the filtered blocks of several pages ahead are prepared in the
direction of travel, nearest first; reversing drops what looked the old way; an
idle viewer looks further ahead, within a share of the cache budget."""

# ruff: noqa: F811
from __future__ import annotations

import os
import threading

import pytest
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


@pytest.mark.parametrize("length", [10_000_000, 10_000_001, 7_654_321, 9_999_999])
def test_the_block_prepared_ahead_is_the_block_the_next_page_asks_for(
    make_window, tmp_path, qtbot, length
):
    """Whatever the page length in microseconds (odd ones too), the look-ahead
    blocks are the very blocks the following pages ask for."""
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=600))
    win.page_us = lambda: length
    win.set_state(t0=T0 + 100 * SEC, filters=dsp.FilterSettings(0.5, 70.0))
    settle(qtbot, win)
    ctl = win.controller
    ctl._watch.stop()
    assert ctl.wait_idle(20)
    prepared = {k[2] for k in view_keys(win)}
    for step in (1, 2, 3):
        win.set_state(t0=win.state.t0 + length)
        wanted = ctl._req.span
        assert wanted in prepared, (step, wanted)
        settle(qtbot, win)
        ctl.wait_idle(20)
        prepared |= {k[2] for k in view_keys(win)}


def test_the_decimation_step_is_designed_once_per_settings_and_rate(
    make_window, tmp_path, qtbot, monkeypatch
):
    """Looking ahead asks for the step of every page and group; the filter is
    not designed again for each of them (it costs milliseconds on the GUI thread)."""
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=600))
    calls = []
    real = dsp.applied_response

    def counting(settings, fs, freqs):
        if len(freqs) == 4097:  # the step search's grid (the filtering itself uses others)
            calls.append((settings, fs))
        return real(settings, fs, freqs)

    monkeypatch.setattr(dsp, "applied_response", counting)
    dsp.max_out_step.cache_clear()
    settings = dsp.FilterSettings(0.5, 61.0)  # not used by any other test
    win.set_state(t0=T0 + 100 * SEC, filters=settings)
    settle(qtbot, win)
    for _ in range(3):
        win.page(1)
        settle(qtbot, win)
    assert win.controller._req is not None
    assert settings in [c[0] for c in calls]
    # once per pair (two threads may miss together at the very start; it was ~50)
    assert len(calls) <= 3, len(calls)


def _submitted_while_visible_work_remains(win, monkeypatch):
    """Record every look-ahead block handed to the compute pool while a visible
    block was still waiting or running."""
    ctl = win.controller
    seen: list[tuple[int, int]] = []
    real = ctl._pool.submit

    def submit(fn, job, *a, **k):
        visible_left = any(j.priority == 0 for j in ctl._jobs.values())
        seen.append((job.priority, int(visible_left)))
        return real(fn, job, *a, **k)

    monkeypatch.setattr(ctl._pool, "submit", submit)
    return seen


def test_after_a_change_of_filter_look_ahead_waits_for_the_visible_blocks(
    make_window, tmp_path, qtbot, monkeypatch
):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=600))
    win.set_state(t0=T0 + 100 * SEC, filters=dsp.FilterSettings(0.5, 70.0))
    settle(qtbot, win)
    win.controller.wait_idle(20)
    seen = _submitted_while_visible_work_remains(win, monkeypatch)
    win.set_state(filters=dsp.FilterSettings(1.0, 40.0))
    settle(qtbot, win)
    win.controller.wait_idle(20)
    ahead = [v for p, v in seen if p > 0]
    assert ahead, "the change should have looked ahead too"
    assert not any(ahead), seen  # none started while a visible block was unfinished


def test_plain_paging_is_not_held_back_by_that_rule(make_window, tmp_path, qtbot, monkeypatch):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=600))
    win.set_state(t0=T0 + 100 * SEC, filters=dsp.FilterSettings(0.5, 70.0))
    settle(qtbot, win)
    win.controller.wait_idle(20)
    win.page(1)
    assert not win.controller._state_change  # same filters, rows and width: no hold


def _ready_to_deepen(win):
    ctl = win.controller
    ctl._watch.stop()  # the timer would deepen on its own: step by hand
    ctl.idle_before_deepening = 0.0
    return ctl


def test_an_idle_viewer_does_not_look_ahead_while_hidden_or_minimised(make_window, tmp_path, qtbot):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=600))
    win.set_state(t0=T0 + 100 * SEC, filters=dsp.FilterSettings(0.5, 70.0))
    settle(qtbot, win)
    ctl = _ready_to_deepen(win)
    win.hide()
    assert not ctl.active and not ctl._maybe_deepen()
    win.show()
    assert ctl.active
    win.showMinimized()
    qtbot.waitUntil(lambda: not ctl.active, timeout=3000)
    assert not ctl._maybe_deepen()
    win.showNormal()
    qtbot.waitUntil(lambda: ctl.active, timeout=3000)
    settle(qtbot, win)
    ctl.wait_idle(20)
    assert ctl._maybe_deepen()


def test_a_failed_read_stops_idle_deepening_until_the_next_page(
    make_window, tmp_path, qtbot, monkeypatch
):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=600))
    win.set_state(t0=T0 + 100 * SEC, filters=dsp.FilterSettings(0.5, 70.0))
    settle(qtbot, win)
    ctl = _ready_to_deepen(win)
    ctl.wait_idle(20)
    heard = []
    ctl.failed.connect(heard.append)
    # a block that is not on screen fails: no message, but no more deepening
    ctl._fail_keys({("view", "somewhere else")})
    assert heard == [] and not ctl._maybe_deepen()
    win.page(1)  # the next navigation lifts it
    settle(qtbot, win)
    ctl.wait_idle(20)
    assert ctl._maybe_deepen()
    # a block on screen fails: the message, and the same stop
    ctl._fail_keys({next(iter(ctl._req.keys.values()))})
    assert len(heard) == 1 and not ctl._maybe_deepen()


def _unrequested_tiles(ctl):
    """Tiles a waiting block needs that the scheduler has no request for."""
    sched = ctl.scheduler
    lost = []
    with sched._cv:
        for job in ctl._jobs.values():
            if job.state != "waiting":
                continue
            for key in job.tile_keys:
                if key in ctl._held or key in ctl.cache:
                    continue
                sj = sched._jobs.get(key)
                if sj is None or sj.state not in ("queued", "running"):
                    lost.append((job.priority, job.rank))
    return lost


def test_a_kept_lookahead_block_keeps_its_tile_requests_when_the_page_moves_on(
    make_window, tmp_path, qtbot, monkeypatch
):
    """The block the old page looked ahead to and the new page still wants keeps
    its running work: its tiles must still be wanted by the scheduler, not
    cancelled as stale (the block would then wait for a retry)."""
    names = [f"E{i}" for i in range(40)]  # three groups: the screens above and below too
    win = make_window(write(tmp_path / "m.mefd", names, seconds=900))
    win.set_state(t0=T0 + 100 * SEC, filters=dsp.FilterSettings(0.5, 70.0))
    settle(qtbot, win)
    gate = hold_reads(win, monkeypatch)
    ctl = win.controller
    ctl._watch.stop()
    try:
        for step in (0.1, 0.1, 1, 1, 1, -1, 1, 0.5, 0.5, 1):
            win.page(step)
            assert not _unrequested_tiles(ctl), step
    finally:
        gate.set()
    settle(qtbot, win)


def test_a_visible_block_that_gives_up_lets_the_held_lookahead_go_at_once(
    make_window, tmp_path, qtbot, monkeypatch
):
    """After a change of filter the look-ahead waits for the visible blocks. A
    visible block that is given up (its tile was lost too often) ends that
    wait in the same pump, not at the next 250 ms watch tick."""
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=600))
    win.set_state(t0=T0 + 100 * SEC, filters=dsp.FilterSettings(0.5, 70.0))
    settle(qtbot, win)
    ctl = win.controller
    ctl.wait_idle(20)
    ctl._watch.stop()  # the tick must not be what does it
    gate = hold_reads(win, monkeypatch)  # nothing reaches the cache while we look
    win.set_state(filters=dsp.FilterSettings(1.0, 40.0))
    assert ctl._state_change
    visible = [j for j in ctl._jobs.values() if j.priority == 0]
    ahead = [j for j in ctl._jobs.values() if j.priority > 0]
    assert visible and ahead
    lost = {id(j) for j in visible}
    real_missing = ctl._missing
    monkeypatch.setattr(ctl, "_missing", lambda j: [("lost", 0)] if id(j) in lost else [])
    monkeypatch.setattr(ctl.scheduler, "pending", lambda: 0)
    for j in visible:
        j.retries = ctlmod.MAX_RETRIES
    submitted = []
    monkeypatch.setattr(ctl._pool, "submit", lambda fn, job, *a, **k: submitted.append(job))
    ctl._pump()
    assert not [j for j in ctl._jobs.values() if j.priority == 0]  # given up
    assert submitted and all(j.priority > 0 for j in submitted)  # and the look-ahead went on
    gate.set()
    monkeypatch.setattr(ctl, "_missing", real_missing)


def key_repeat(win, key, n):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    for _ in range(n):
        ev = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, "", True, 1)
        QApplication.sendEvent(win, ev)


def test_a_held_page_key_moves_at_every_repeat_but_asks_for_the_newest_page_only(
    make_window, tmp_path, qtbot
):
    """Repeats that arrive together (the window was busy painting) move the
    page one by one, so the position keeps the key's pace, but only the page
    where they end is asked for: nothing is read or computed for the pages
    that were passed."""
    from PySide6.QtCore import Qt

    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=600))
    win.set_state(t0=T0 + 20 * SEC, filters=dsp.FilterSettings(0.5, 70.0))
    settle(qtbot, win)
    ctl = win.controller
    ctl.wait_idle(20)
    page = win.page_us()
    start, gen = win.state.t0, ctl.generation
    key_repeat(win, Qt.Key.Key_Right, 8)
    assert win.state.t0 == start + 8 * page  # the position kept every repeat
    qtbot.waitUntil(lambda: ctl.generation > gen, timeout=2000)
    settle(qtbot, win)
    assert ctl.generation - gen <= 2  # one page asked for, not eight
    assert abs(ctl._req.page_start - (start + 8 * page)) < 1
    # a single press (not a repeat) is still shown at once
    gen = ctl.generation
    win.page(1)
    assert ctl.generation == gen + 1
    # the end of the recording stops a held key
    key_repeat(win, Qt.Key.Key_Right, 400)
    assert win.state.t0 + page <= win.data.timeline_end + 1
    qtbot.waitUntil(lambda: ctl._req is not None and not ctl.loading, timeout=10_000)
