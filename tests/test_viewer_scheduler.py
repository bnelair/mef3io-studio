"""The viewer's tile scheduler and cache (workers/viewer_scheduler.py)."""

from __future__ import annotations

import threading

import mef3io
import numpy as np

from mef3io_studio.model.cancel import JobCancelled
from mef3io_studio.model.viewer_source import TILE_SAMPLES, open_recordings
from mef3io_studio.workers.viewer_scheduler import (
    LOOKAHEAD,
    SCREENS,
    VISIBLE,
    ChannelGroup,
    TileCache,
    TileScheduler,
    ViewBlockCache,
    group_channels,
)

T0 = 946_684_800_000_000
SEC = 1_000_000


class FakeData:
    """Stands in for ViewerData: reads wait for a gate, and are recorded."""

    timeline_start = T0
    warning_count = 7

    def __init__(self, gate: threading.Event | None = None, tiles: int = 100):
        self.gate = gate
        self.tiles = tiles
        self.order: list[tuple] = []
        self.started = threading.Event()
        self.cancelled: list[tuple] = []
        self.lock = threading.Lock()

    def rate(self, ch):
        return 512.0 if ch.startswith("hi") else 256.0

    def tile_count(self, rate):
        return self.tiles

    def tile_start_time(self, rate, k):
        return T0 + int(round(k * TILE_SAMPLES * 1e6 / rate))

    def tile_index_range(self, rate, t0, t1):
        n = TILE_SAMPLES
        first = max(0, int((t0 - T0) * rate / 1e6) // n)
        last = max(first, int(((t1 - T0) * rate / 1e6 - 1e-9) // n))
        return first, last

    def read_tile(self, channels, rate, k, cancel=None):
        with self.lock:
            self.order.append((tuple(channels), k))
        self.started.set()
        if self.gate is not None:
            while True:
                released = self.gate.wait(0.01)
                if cancel is not None and cancel.cancelled:
                    with self.lock:
                        self.cancelled.append((tuple(channels), k))
                    raise JobCancelled()
                if released:
                    break
        return np.zeros((len(channels), 4), dtype=np.float32)


def test_cache_evicts_least_recently_used_under_the_budget():
    cache = TileCache(budget_bytes=4000)
    block = np.zeros(250, dtype=np.float32)  # 1000 bytes
    for i in range(4):
        cache.put(i, block)
    assert cache.bytes_used == 4000 and len(cache) == 4
    assert cache.get(0) is not None  # 0 is now the most recent
    cache.put(4, block)
    assert 1 not in cache and 0 in cache and 4 in cache
    assert cache.bytes_used <= 4000
    cache.put("big", np.zeros(5000, dtype=np.float32))  # larger than the budget
    assert "big" not in cache and cache.bytes_used <= 4000
    cache.set_budget(2000)
    assert cache.bytes_used <= 2000 and len(cache) == 2
    cache.put(0, np.zeros(100, dtype=np.float32))  # replacing keeps the count right
    assert cache.bytes_used == sum(cache.get(k).nbytes for k in list(cache._items))


def test_view_blocks_share_the_tile_budget():
    cache = TileCache(budget_bytes=3000)
    views = ViewBlockCache(cache)
    arr = np.zeros(250, dtype=np.float32)
    cache.put(("tile", 1), arr)
    cache.put(("tile", 2), arr)
    views.put((0, 10), "bip", None, "hp0.5", arr)
    assert views.get((0, 10), "bip", None, "hp0.5") is not None
    assert views.get((0, 10), "bip", None, "hp1") is None  # other filter settings
    assert views.get((0, 10), "ref", "Cz", "hp0.5") is None  # other montage/reference
    views.put((10, 20), "bip", None, "hp0.5", arr)  # over the budget: oldest tile goes
    assert ("tile", 1) not in cache and cache.bytes_used <= 3000


def test_cache_is_thread_safe():
    cache = TileCache(budget_bytes=50_000)
    block = np.zeros(100, dtype=np.float32)

    def hammer(seed):
        for i in range(500):
            cache.put((seed, i % 40), block)
            cache.get((seed, (i * 7) % 40))

    threads = [threading.Thread(target=hammer, args=(s,)) for s in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert cache.bytes_used <= 50_000
    assert cache.bytes_used == sum(v.nbytes for v in cache._items.values())


def test_visible_tiles_are_read_before_lookahead_before_screens():
    gate = threading.Event()
    data = FakeData(gate)
    ready = []
    sched = TileScheduler(data, TileCache(), workers=1, on_ready=ready.append)
    try:
        # the first request occupies the only worker
        sched.request(256.0, ("blocker",), 0, VISIBLE, 0)
        assert data.started.wait(5)
        sched.request(256.0, ("s",), 1, SCREENS, 0)
        sched.request(256.0, ("l",), 1, LOOKAHEAD, 0)
        sched.request(256.0, ("v",), 1, VISIBLE, 0)
        sched.request(256.0, ("l",), 2, LOOKAHEAD, 0)
        gate.set()
        assert sched.wait_idle(10)
        assert [c[0] for c, _ in data.order] == ["blocker", "v", "l", "l", "s"]
        assert len(ready) == 5
    finally:
        sched.close()


def test_a_later_request_can_raise_the_urgency():
    gate = threading.Event()
    data = FakeData(gate)
    sched = TileScheduler(data, TileCache(), workers=1)
    try:
        sched.request(256.0, ("blocker",), 0, VISIBLE, 0)
        assert data.started.wait(5)
        sched.request(256.0, ("a",), 1, SCREENS, 0)
        sched.request(256.0, ("b",), 1, LOOKAHEAD, 0)
        sched.request(256.0, ("a",), 1, VISIBLE, 0)  # the user scrolled onto it
        gate.set()
        assert sched.wait_idle(10)
        assert [c[0] for c, _ in data.order] == ["blocker", "a", "b"]
    finally:
        sched.close()


def test_stale_requests_are_cancelled_queued_and_running():
    gate = threading.Event()
    data = FakeData(gate)
    ready = []
    sched = TileScheduler(data, TileCache(), workers=1, on_ready=ready.append)
    try:
        running = sched.request(256.0, ("run",), 0, LOOKAHEAD, generation=1)
        assert data.started.wait(5)
        sched.request(256.0, ("queued",), 0, LOOKAHEAD, generation=1)
        keep = sched.request(256.0, ("fresh",), 0, VISIBLE, generation=2)
        assert sched.cancel_stale(2) == 2
        gate.set()
        assert sched.wait_idle(10)
        assert data.cancelled == [(("run",), 0)]  # stopped inside the read
        assert ready == [keep]
        assert [c[0] for c, _ in data.order] == ["run", "fresh"]
        assert running not in [r for r in ready]
    finally:
        sched.close()


def test_cached_tiles_are_not_read_again_and_still_signal():
    data = FakeData()
    cache = TileCache()
    ready = []
    sched = TileScheduler(data, cache, workers=2, on_ready=ready.append)
    try:
        key = sched.request(256.0, ("a", "b"), 3)
        assert sched.wait_idle(10)
        assert cache.get(key).shape == (2, 4)
        sched.request(256.0, ("a", "b"), 3)
        assert sched.wait_idle(10)
        assert len(data.order) == 1 and ready == [key, key]
        assert sched.warning_count == 7
    finally:
        sched.close()


def test_a_failing_read_reports_and_the_worker_survives():
    class Broken(FakeData):
        def read_tile(self, channels, rate, k, cancel=None):
            if channels[0] == "bad":
                raise RuntimeError("boom")
            return super().read_tile(channels, rate, k, cancel)

    errors, ready = [], []
    sched = TileScheduler(
        Broken(),
        TileCache(),
        workers=1,
        on_ready=ready.append,
        on_error=lambda k, e: errors.append(k),
    )
    try:
        sched.request(256.0, ("bad",), 0)
        good = sched.request(256.0, ("ok",), 0)
        assert sched.wait_idle(10)
        assert [e[1] for e in errors] == [("bad",)] and ready == [good]
    finally:
        sched.close()


def test_default_worker_count_is_bounded():
    from mef3io_studio.workers.viewer_scheduler import default_workers

    assert 1 <= default_workers() <= 6


def test_channels_are_grouped_by_rate_in_sixteens_with_partners():
    data = FakeData()
    names = [f"lo{i}" for i in range(20)] + ["hi1", "hi2"]
    groups = group_channels(data, names)
    assert [(g.rate, len(g.channels)) for g in groups] == [(256.0, 16), (256.0, 4), (512.0, 2)]
    forced = group_channels(data, names, partners={"lo0": ["lo17", "hi1", "lo1"]})
    assert "lo17" in forced[0].channels and len(forced[0].channels) == 17
    assert "hi1" not in forced[0].channels  # different rate: not a partner


def test_prefetch_plan_order_and_direction():
    data = FakeData()
    sched = TileScheduler(data, TileCache(), workers=1)
    try:
        n_us = int(TILE_SAMPLES * 1e6 / 256.0)  # one tile of time
        page0 = T0 + 10 * n_us + n_us // 2
        page1 = page0 + n_us  # a page spanning tiles 10 and 11
        here = ChannelGroup(256.0, ("a",))
        below = ChannelGroup(256.0, ("b",))
        plan = sched.prefetch_plan(page0, page1, +1, [here], [below], generation=5)
        assert [(r.k, r.priority) for r in plan if r.channels == ("a",)] == [
            (10, VISIBLE),
            (11, VISIBLE),
            (12, LOOKAHEAD),
            (13, LOOKAHEAD),
            (9, LOOKAHEAD),
        ] or [r.k for r in plan if r.channels == ("a",)][:5] == [10, 11, 12, 13, 9]
        prios = [r.priority for r in plan]
        assert prios == sorted(prios)  # visible, then look-ahead, then screens
        assert [r.channels for r in plan if r.priority == SCREENS] == [("b",)] * 2
        assert all(r.generation == 5 for r in plan)
        back = sched.prefetch_plan(page0, page1, -1, [here])
        ahead = [r.k for r in back if r.priority == LOOKAHEAD]
        assert ahead[0] == 9 and 8 in ahead and ahead.index(12) > ahead.index(8)
        # never before the timeline start or past its end
        start = sched.prefetch_plan(T0, T0 + n_us // 2, -1, [here])
        assert all(r.k >= 0 for r in start)
    finally:
        sched.close()


def test_the_scheduler_fills_the_cache_from_a_real_session(tmp_path):
    fs = 256.0
    rng = np.random.default_rng(0)
    path = tmp_path / "s.mefd"
    with mef3io.Writer(str(path), overwrite=True) as w:
        for ch in ("a", "b"):
            w.write(ch, np.round(rng.normal(0, 5, int(300 * fs)), 2), T0, fs)
    data = open_recordings([str(path)])
    cache = TileCache()
    ready = threading.Event()
    seen = []

    def on_ready(key):
        seen.append(key)
        if len(seen) == 2:
            ready.set()

    sched = TileScheduler(data, cache, workers=2, on_ready=on_ready)
    try:
        groups = group_channels(data, ["a", "b"])
        t1 = data.tile_start_time(fs, 1)
        plan = sched.prefetch_plan(t1 - SEC, t1 + SEC, +1, groups)
        sched.request_all(plan)
        assert ready.wait(20)
        assert sched.wait_idle(20)
        key = sched.key_of(fs, ("a", "b"), 0)
        assert np.isfinite(cache.get(key)).all()
        assert cache.get(sched.key_of(fs, ("a", "b"), 1)) is not None
    finally:
        sched.close()
        data.close()
