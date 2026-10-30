"""The viewer's tile scheduler and byte-budget cache.

``TileScheduler`` reads tiles (:meth:`ViewerData.read_tile`) on a small
thread pool, most urgent first, and tells the caller when one is ready.
``TileCache`` is an LRU by bytes; ``ViewBlockCache`` keeps filtered view
blocks in the SAME budget. Plain threading, no Qt: ``on_ready`` runs on a
worker thread and the UI layer marshals it.

Reads here are far too small and too many to be jobs, and they must not
take the process-wide quiet lock; mef3io's warnings during them are counted
by the data object (:attr:`ViewerData.warning_count`), never logged.
"""

from __future__ import annotations

import heapq
import itertools
import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from ..model.cancel import CancelToken, JobCancelled, is_stop

#: Priorities (lower = sooner).
VISIBLE = 0
LOOKAHEAD = 1
SCREENS = 2

GROUP_SIZE = 16
DEFAULT_BUDGET = 512 * 1024 * 1024


def default_workers() -> int:
    return min(6, (os.cpu_count() or 2) // 2 or 1)


class TileCache:
    """A thread-safe LRU keyed by anything hashable, bounded by the bytes of
    its numpy values. An item larger than the budget is not kept."""

    def __init__(self, budget_bytes: int = DEFAULT_BUDGET) -> None:
        self._budget = int(budget_bytes)
        self._items: OrderedDict[Hashable, np.ndarray] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    @property
    def budget(self) -> int:
        return self._budget

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return self._bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, key: Hashable) -> bool:
        with self._lock:
            return key in self._items

    def set_budget(self, budget_bytes: int) -> None:
        with self._lock:
            self._budget = int(budget_bytes)
            self._evict()

    def get(self, key: Hashable) -> np.ndarray | None:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    def put(self, key: Hashable, value: np.ndarray) -> None:
        size = int(value.nbytes)
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= int(old.nbytes)
            if size > self._budget:
                return
            self._items[key] = value
            self._bytes += size
            self._evict()

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def _evict(self) -> None:
        while self._bytes > self._budget and self._items:
            _, old = self._items.popitem(last=False)
            self._bytes -= int(old.nbytes)


class ViewBlockCache:
    """Filtered view blocks, in the byte budget of a :class:`TileCache`.

    A block is keyed by (span, montage, reference, filter settings), not by
    tile: its values depend on the margin and on gap runs across tile edges.
    """

    def __init__(self, cache: TileCache) -> None:
        self._cache = cache

    @staticmethod
    def key(
        span: tuple[int, int], montage_key: Hashable, reference: Hashable, filter_key: Hashable
    ) -> tuple:
        return ("view", (int(span[0]), int(span[1])), montage_key, reference, filter_key)

    def get(self, span, montage_key, reference, filter_key) -> np.ndarray | None:
        return self._cache.get(self.key(span, montage_key, reference, filter_key))

    def put(self, span, montage_key, reference, filter_key, value: np.ndarray) -> None:
        self._cache.put(self.key(span, montage_key, reference, filter_key), value)


@dataclass(frozen=True)
class ChannelGroup:
    rate: float
    channels: tuple[str, ...]


@dataclass(frozen=True)
class Request:
    rate: float
    channels: tuple[str, ...]
    k: int
    priority: int
    generation: int


def group_channels(
    data,
    channels: Sequence[str],
    size: int = GROUP_SIZE,
    partners: dict[str, Iterable[str]] | None = None,
) -> list[ChannelGroup]:
    """Split ``channels`` into groups of ``size`` per sampling rate (first
    appearance order). ``partners`` maps a channel to channels that must be
    fetched with it (its reference or bipolar partners); they join the group
    of the channel that needs them when they share its rate (others are shown
    as recorded, and fetched in their own group)."""
    by_rate: dict[float, list[str]] = {}
    for ch in dict.fromkeys(channels):
        by_rate.setdefault(float(data.rate(ch)), []).append(ch)
    groups: list[ChannelGroup] = []
    for rate, names in by_rate.items():
        for i in range(0, len(names), size):
            part = names[i : i + size]
            extra: set[str] = set()
            for ch in part:
                for other in (partners or {}).get(ch, ()):
                    if other not in part and abs(float(data.rate(other)) - rate) < 1e-9:
                        extra.add(other)
            groups.append(ChannelGroup(rate, tuple(part) + tuple(sorted(extra))))
    return groups


class _Job:
    __slots__ = ("key", "request", "token", "state", "seq")

    def __init__(self, key, request: Request, seq: int) -> None:
        self.key = key
        self.request = request
        self.token = CancelToken()
        self.state = "queued"  # queued | running | done | cancelled
        self.seq = seq


class TileScheduler:
    """Reads tiles by priority on a thread pool and fills a cache.

    ``on_ready(key)`` is called from a worker thread when a tile is in the
    cache (also for one that was already cached); ``on_error(key, exc)`` when
    a read failed (the exception is never logged here). Tile keys are
    ``(rate, channels, k)``; ``cache.get(key)`` gives the array.
    """

    def __init__(
        self,
        data,
        cache: TileCache,
        workers: int | None = None,
        on_ready: Callable[[tuple], object] | None = None,
        on_error: Callable[[tuple, BaseException], object] | None = None,
    ) -> None:
        self._data = data
        self._cache = cache
        self.on_ready = on_ready
        self.on_error = on_error
        self._cv = threading.Condition()
        self._heap: list[tuple[int, int, tuple]] = []
        self._jobs: dict[tuple, _Job] = {}
        self._seq = itertools.count()
        self._closed = False
        self._active = 0
        n = workers if workers else default_workers()
        self._threads = [
            threading.Thread(target=self._run, name=f"viewer-tiles-{i}", daemon=True)
            for i in range(n)
        ]
        for t in self._threads:
            t.start()

    # -- API -----------------------------------------------------------------

    @staticmethod
    def key_of(rate: float, channels: Sequence[str], k: int) -> tuple:
        return (float(rate), tuple(channels), int(k))

    @property
    def warning_count(self) -> int:
        """mef3io warnings during reads (counted by the data object)."""
        return int(getattr(self._data, "warning_count", 0))

    def request(
        self,
        rate: float,
        channels_group: Sequence[str],
        k: int,
        priority: int = VISIBLE,
        generation: int = 0,
    ) -> tuple:
        """Ask for a tile; returns its key. Asking again for a tile that is
        queued or running only raises its urgency / generation."""
        key = self.key_of(rate, channels_group, k)
        req = Request(float(rate), key[1], int(k), int(priority), int(generation))
        with self._cv:
            if self._closed:
                return key
            job = self._jobs.get(key)
            if job is not None and job.state in ("queued", "running"):
                old = job.request
                job.request = Request(
                    old.rate,
                    old.channels,
                    old.k,
                    min(old.priority, req.priority),
                    max(old.generation, req.generation),
                )
                if job.state == "queued" and req.priority < old.priority:
                    heapq.heappush(self._heap, (req.priority, next(self._seq), key))
                return key
            job = _Job(key, req, next(self._seq))
            self._jobs[key] = job
            heapq.heappush(self._heap, (req.priority, job.seq, key))
            self._cv.notify()
        return key

    def request_all(self, plan: Iterable[Request]) -> list[tuple]:
        return [self.request(r.rate, r.channels, r.k, r.priority, r.generation) for r in plan]

    def cancel_stale(self, generation: int) -> int:
        """Cancel every queued or running request older than ``generation``
        (running reads are stopped through mef3io's CancelToken). Returns
        how many were cancelled."""
        cancelled = 0
        with self._cv:
            for key, job in list(self._jobs.items()):
                if job.state in ("queued", "running") and job.request.generation < generation:
                    if job.state == "running":
                        job.token.cancel()
                    job.state = "cancelled"
                    del self._jobs[key]
                    cancelled += 1
        return cancelled

    def pending(self) -> int:
        with self._cv:
            return sum(1 for j in self._jobs.values() if j.state in ("queued", "running"))

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until nothing is queued or running (mainly for tests)."""
        with self._cv:
            return self._cv.wait_for(
                lambda: (
                    self._active == 0 and not any(j.state == "queued" for j in self._jobs.values())
                ),
                timeout,
            )

    def close(self) -> None:
        with self._cv:
            self._closed = True
            for job in self._jobs.values():
                if job.state == "running":
                    job.token.cancel()
                job.state = "cancelled"
            self._jobs.clear()
            self._heap.clear()
            self._cv.notify_all()
        for t in self._threads:
            t.join(timeout=5)

    # -- planning ---------------------------------------------------------------

    def prefetch_plan(
        self,
        page_t0: int,
        page_t1: int,
        direction: int,
        channel_groups: Sequence[ChannelGroup],
        adjacent_groups: Sequence[ChannelGroup] = (),
        generation: int = 0,
    ) -> list[Request]:
        """Ordered requests for a page: its tiles (priority 0), then 2 pages
        ahead in the direction of travel and 1 behind (priority 1), then the
        page's tiles for the adjacent channel screens (priority 2)."""
        data = self._data
        length = max(1, int(page_t1 - page_t0))
        sign = -1 if direction < 0 else 1
        if sign > 0:
            ahead = (page_t1, page_t1 + 2 * length)
            behind = (page_t0 - length, page_t0)
        else:
            ahead = (page_t0 - 2 * length, page_t0)
            behind = (page_t1, page_t1 + length)
        plan: list[Request] = []
        seen: set[tuple] = set()

        def add(group: ChannelGroup, k: int, priority: int) -> None:
            key = self.key_of(group.rate, group.channels, k)
            if key in seen or k < 0 or k >= data.tile_count(group.rate):
                return
            seen.add(key)
            plan.append(Request(group.rate, group.channels, k, priority, generation))

        def tiles(group: ChannelGroup, t0: int, t1: int, nearest_to: int) -> list[int]:
            first, last = data.tile_index_range(group.rate, t0, t1)
            ks = range(max(0, first), last + 1)
            return sorted(ks, key=lambda k: abs(data.tile_start_time(group.rate, k) - nearest_to))

        edge = page_t1 if sign > 0 else page_t0
        for g in channel_groups:
            for k in tiles(g, page_t0, page_t1, page_t0):
                add(g, k, VISIBLE)
        for window in (ahead, behind):
            near = edge if window is ahead else (page_t0 if sign > 0 else page_t1)
            for g in channel_groups:
                for k in tiles(g, window[0], window[1], near):
                    add(g, k, LOOKAHEAD)
        for g in adjacent_groups:
            for k in tiles(g, page_t0, page_t1, page_t0):
                add(g, k, SCREENS)
        return plan

    # -- workers --------------------------------------------------------------------

    def _next(self) -> _Job | None:
        with self._cv:
            while True:
                if self._closed:
                    return None
                while self._heap:
                    priority, _, key = heapq.heappop(self._heap)
                    job = self._jobs.get(key)
                    if job is None or job.state != "queued" or job.request.priority != priority:
                        continue  # cancelled, taken, or a stale heap entry
                    job.state = "running"
                    self._active += 1
                    return job
                self._cv.wait()

    def _run(self) -> None:
        while True:
            job = self._next()
            if job is None:
                return
            key = job.key
            result = None
            error: BaseException | None = None
            try:
                result = self._cache.get(key)
                if result is None:
                    req = job.request
                    result = self._data.read_tile(list(req.channels), req.rate, req.k, job.token)
                    if job.state != "cancelled":
                        self._cache.put(key, result)
            except BaseException as exc:  # noqa: BLE001 - a worker must survive
                if not (isinstance(exc, JobCancelled) or is_stop(exc)):
                    error = exc
                result = None
            with self._cv:
                still = job.state == "running"
                if still:
                    job.state = "done"
                    if self._jobs.get(key) is job:
                        del self._jobs[key]
                self._active -= 1
                self._cv.notify_all()
            if not still:
                continue
            try:
                if error is not None and self.on_error is not None:
                    self.on_error(key, error)
                elif result is not None and self.on_ready is not None:
                    self.on_ready(key)
            except Exception:  # a callback must never kill a worker
                pass
