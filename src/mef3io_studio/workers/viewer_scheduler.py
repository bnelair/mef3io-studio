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
        data_key: Hashable,
        span: tuple[int, int],
        montage_key: Hashable,
        reference: Hashable,
        filter_key: Hashable,
    ) -> tuple:
        """``data_key`` = ``(data.data_id, data.epoch)`` (see
        :func:`data_key_of`)."""
        return ("view", data_key, (int(span[0]), int(span[1])), montage_key, reference, filter_key)

    def get(self, data_key, span, montage_key, reference, filter_key) -> np.ndarray | None:
        return self._cache.get(self.key(data_key, span, montage_key, reference, filter_key))

    def put(self, data_key, span, montage_key, reference, filter_key, value: np.ndarray) -> None:
        self._cache.put(self.key(data_key, span, montage_key, reference, filter_key), value)


def data_key_of(data) -> tuple:
    """What goes into every cache key so a reopened (or other) data object
    never serves old values."""
    return (getattr(data, "data_id", 0), getattr(data, "epoch", 0))


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
    __slots__ = ("key", "request", "token", "state", "seq", "deferred")

    def __init__(self, key, request: Request, seq: int) -> None:
        self.key = key
        self.request = request
        self.token = CancelToken()
        self.state = "queued"  # queued | running | done | cancelled
        self.seq = seq
        self.deferred = False  # waiting for a cancelled read of the same key


class TileScheduler:
    """Reads tiles by priority on a thread pool and fills a cache.

    ``on_ready(key, array)`` is called from a worker thread with the tile
    (also one that was already cached) exactly once per finished request,
    even when the tile is larger than the cache budget or evicted before the
    callback (the array is passed, not looked up); never after ``close()``
    returned (one already running may finish: callbacks must not block, close()
    does not wait for them). ``on_error(key, exc)`` is called when a read failed (never for
    a cancelled one; the exception is never logged here). Tile keys are
    ``(data_id, epoch, rate, channels, k)``.
    """

    def __init__(
        self,
        data,
        cache: TileCache,
        workers: int | None = None,
        on_ready: Callable[[tuple, np.ndarray], object] | None = None,
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
        #: Cancelled reads still running, by key (a re-request waits for them).
        self._zombies: dict[tuple, _Job] = {}
        self._draining = False
        n = workers if workers else default_workers()
        self._threads = [
            threading.Thread(target=self._run, name=f"viewer-tiles-{i}", daemon=True)
            for i in range(n)
        ]
        # One read decodes with several threads of its own. With several
        # reads at once that would oversubscribe the cores (workers x
        # mef3io's automatic pool), so each read gets its share of the
        # cores, at most 2 (mef3io 2.0: one n_threads count per reader).
        # A lone worker keeps mef3io's automatic count (set explicitly, so a
        # cap left by an earlier scheduler on the same data is lifted).
        if hasattr(data, "set_read_threads"):
            data.set_read_threads(max(1, min(2, (os.cpu_count() or 2) // n)) if n > 1 else None)
        for t in self._threads:
            t.start()

    # -- API -----------------------------------------------------------------

    @staticmethod
    def key_of(rate: float, channels: Sequence[str], k: int, data_key: tuple = (0, 0)) -> tuple:
        return (*data_key, float(rate), tuple(channels), int(k))

    def _key(self, rate, channels, k) -> tuple:
        return self.key_of(rate, channels, k, data_key_of(self._data))

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
        key = self._key(rate, channels_group, k)
        req = Request(float(rate), tuple(channels_group), int(k), int(priority), int(generation))
        with self._cv:
            if self._closed or self._draining:
                return key  # not accepted: the caller sees it never becomes ready
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
                if job.state == "queued" and not job.deferred and req.priority < old.priority:
                    heapq.heappush(self._heap, (req.priority, next(self._seq), key))
                return key
            job = _Job(key, req, next(self._seq))
            self._jobs[key] = job
            if key in self._zombies:
                job.deferred = True  # starts when the cancelled read of it ends
            else:
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
            for job in list(self._jobs.values()):
                if job.state in ("queued", "running") and job.request.generation < generation:
                    self._abandon(job)
                    cancelled += 1
        return cancelled

    def _abandon(self, job: _Job) -> None:
        """Cancel a job (caller holds the lock)."""
        if job.state == "running":
            job.token.cancel()
            self._zombies[job.key] = job
        job.state = "cancelled"
        if self._jobs.get(job.key) is job:
            del self._jobs[job.key]

    def drain(self, timeout: float | None = None) -> bool:
        """Cancel everything, queued and running, and wait for the running
        reads to end. Call before ``ViewerData.reopen()`` / ``close()``.
        Returns False on timeout."""
        with self._cv:
            self._draining = True  # requests made meanwhile are refused
            try:
                for job in list(self._jobs.values()):
                    if job.state in ("queued", "running"):
                        self._abandon(job)
                self._heap.clear()
                return self._cv.wait_for(lambda: self._active == 0, timeout)
            finally:
                self._draining = False

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
            for job in list(self._jobs.values()):
                self._abandon(job)
            self._heap.clear()
            self._cv.notify_all()
        for t in self._threads:
            if t is not threading.current_thread():
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
            key = self._key(group.rate, group.channels, k)
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
            result: np.ndarray | None = None
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
                live = job.state == "running"
                if live:
                    job.state = "done"
                    if self._jobs.get(key) is job:
                        del self._jobs[key]
                if self._zombies.get(key) is job:
                    del self._zombies[key]
                    waiting = self._jobs.get(key)
                    if waiting is not None and waiting.deferred:
                        waiting.deferred = False
                        heapq.heappush(self._heap, (waiting.request.priority, waiting.seq, key))
                self._active -= 1
                self._cv.notify_all()
            if not live:
                continue  # cancelled: no result, no error
            with self._cv:
                if self._closed:
                    continue  # none starts after close(); one already running may finish
            try:
                if error is not None:
                    if self.on_error is not None:
                        self.on_error(key, error)
                elif result is not None and self.on_ready is not None:
                    self.on_ready(key, result)
            except Exception:  # a callback must never kill a worker
                pass
