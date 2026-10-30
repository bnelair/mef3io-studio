"""The viewer's data flow, between the window and the workers.

``show()`` asks for a page: it turns the page into view blocks (one per
group of rows, 1.5 page lengths, see ``viewer_view.aligned_block``), asks the
tile scheduler for the tiles each needs (visible first, then look-ahead),
and when a block's tiles are all in the cache runs montage + filtering for
it on a small compute pool. Finished blocks go to the ``ViewBlockCache`` and
the window is told, on the GUI thread, through Qt signals; callbacks that
arrive from worker threads only emit a signal and never block.

Nothing here calls mef3io on the GUI thread: tiles and envelopes are read by
the scheduler's and the compute pool's threads. (The data object's channel
facts - rates, units, who owns what - were read once by the opening task.)
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from ..common.privacy import scrub
from ..model import dsp, montage
from ..model.cancel import CancelToken, JobCancelled, is_stop
from ..model.viewer_view import Performance, aligned_block, memory_bytes, next_block_start
from ..workers import viewer_pipeline as pipe
from ..workers.viewer_scheduler import (
    LOOKAHEAD,
    SCREENS,
    VISIBLE,
    ChannelGroup,
    TileCache,
    TileScheduler,
    ViewBlockCache,
    data_key_of,
)
from .strip import envelope_bins

MAX_COMPUTE = 2
log = logging.getLogger(__name__)
STRIP_BINS = 2000
#: A lost tile is asked for again at most this often / this many times.
RETRY_AFTER = 3.0
MAX_RETRIES = 3
#: Tiles held for waiting jobs may use this share of the cache budget.
HELD_SHARE = 0.25
#: ... but never less than this (a tiny budget must still let a block finish).
HELD_FLOOR = 32 << 20


@dataclass(eq=False)
class _Job:
    key: tuple
    kind: str  # "samples" | "envelope"
    plan: pipe.GroupPlan
    rows: list
    span: tuple[int, int]
    settings: dsp.FilterSettings
    plot_px: int
    priority: int
    generation: int
    tile_keys: list = field(default_factory=list)
    state: str = "waiting"  # waiting | computing
    token: CancelToken = field(default_factory=CancelToken)
    seq: int = 0
    tile_numbers: list = field(default_factory=list)
    retries: int = 0
    last_ask: float = field(default_factory=time.monotonic)
    arrays: dict = field(default_factory=dict)


@dataclass
class _Request:
    generation: int
    rows: list
    plans: list
    overview: bool
    settings: dsp.FilterSettings
    span: tuple[int, int]
    plot_px: int
    visible: tuple[int, int]
    #: plan index -> job/cache keys
    keys: dict[int, tuple] = field(default_factory=dict)
    pieces: dict[int, object] = field(default_factory=dict)
    failed: set = field(default_factory=set)
    location: dict[int, tuple[int, int]] = field(default_factory=dict)


class ViewController(QObject):
    #: Something new can be drawn (a block arrived or was found in the cache).
    pieces_changed = Signal()
    #: True while blocks of the visible rows are still being prepared.
    loading_changed = Signal(bool)
    #: A read or computation failed: a plain sentence.
    failed = Signal(str)
    #: The strip's envelope: ``[2, bins]`` normalised.
    strip_envelope = Signal(object)

    _tile = Signal(object)
    _done = Signal(object, object)  # (job, (outcome, error))
    _error = Signal(object, object)
    _strip = Signal(object)

    def __init__(self, data, perf: Performance | None = None, parent=None):
        super().__init__(parent)
        perf = perf or Performance()
        self.perf = perf
        self.data = data
        self.cache = TileCache(memory_bytes(perf))
        self.blocks = ViewBlockCache(self.cache)
        self.scheduler = TileScheduler(
            data,
            self.cache,
            workers=perf.load_workers or None,
            on_ready=self._on_tile,
            on_error=self._on_tile_error,
        )
        if perf.decode_threads:
            data.set_read_threads(perf.decode_threads)
        self.max_compute = perf.filter_workers or MAX_COMPUTE
        self._pool = ThreadPoolExecutor(self.max_compute, thread_name_prefix="viewer-compute")
        self._strip_pool = ThreadPoolExecutor(1, thread_name_prefix="viewer-strip")
        #: Tiles handed over by the scheduler that a waiting job still needs
        #: (the cache may evict, or refuse, a big one).
        self._held: dict[tuple, np.ndarray] = {}
        self._held_bytes = 0
        self._needed: set[tuple] = set()
        self._held_lock = threading.Lock()
        self._zombies: set[_Job] = set()
        self._jobs: dict[tuple, _Job] = {}
        self._seq = itertools.count()
        self._generation = 0
        self._req: _Request | None = None
        self._loading = False
        self._closed = False
        self._watch = QTimer(self)  # a waiting job is looked at again now and then
        self._watch.setInterval(250)
        self._watch.timeout.connect(self._pump)
        self._watch.start()
        self._tile.connect(self._pump)
        self._done.connect(self._on_done)
        self._error.connect(self._on_error)
        self._strip.connect(self.strip_envelope)

    # -- worker-thread callbacks: only emit ------------------------------------

    def _on_tile(self, key, array) -> None:
        if self._closed:
            return
        with self._held_lock:
            if key in self._needed and key not in self._held:
                size = int(getattr(array, "nbytes", 0))
                if self._held_bytes + size <= self._held_cap():
                    self._held[key] = array
                    self._held_bytes += size
        self._tile.emit(key)

    def _held_cap(self) -> float:
        return max(self.cache.budget * HELD_SHARE, HELD_FLOOR)

    def _on_tile_error(self, key, exc) -> None:
        if not self._closed:
            self._error.emit(key, exc)

    # -- requests ----------

    @property
    def generation(self) -> int:
        return self._generation

    def show(
        self,
        rows: list[montage.Row],
        page: tuple[int, int],
        settings: dsp.FilterSettings,
        plot_px: int,
        visible: tuple[int, int],
        overview: bool = False,
        direction: int = 1,
    ) -> None:
        """Ask for a page. ``visible`` is the row range on screen
        ``[first, last)``."""
        if self._closed:
            return
        self._generation += 1
        gen = self._generation
        data = self.data
        length = max(1, page[1] - page[0])
        span = aligned_block(page[0], length, data.timeline_start)
        if overview:
            settings = dsp.FilterSettings()
            plans = _envelope_plans(rows)
        else:
            try:
                plans = pipe.plan_groups(data, rows)
            except ValueError:
                self._req = None
                self.failed.emit("These channels cannot be shown together this way.")
                self._set_loading(False)
                return
        req = _Request(gen, list(rows), plans, overview, settings, span, plot_px, visible)
        for gi, plan in enumerate(plans):
            for local, ri in enumerate(plan.rows):
                req.location[ri] = (gi, local)
        self._req = req
        wanted: set[tuple] = set()
        for gi, plan in enumerate(plans):
            first, last = visible
            if any(first <= ri < last for ri in plan.rows):
                key = self._ensure(req, gi, span, VISIBLE, wanted)
                req.keys[gi] = key
        self._drop_stale(gen, wanted)
        self._collect()
        self._set_loading(self._visible_pending(req))
        if not overview:
            self._look_ahead(req, direction, wanted)
        self._pump()

    def _ensure(self, req: _Request, gi: int, span, priority: int, wanted: set) -> tuple:
        plan = req.plans[gi]
        settings = req.settings
        if req.overview:
            vkey = ("env", plan.channels)
            filt = None
            step = 1
        else:
            _, step = pipe.out_step_for(self.data, plan.rate, span, settings, req.plot_px)
            vkey = pipe.view_key(plan, req.rows, settings, step)[0]
            filt = (settings, step)
        dk = data_key_of(self.data)
        key = ViewBlockCache.key(dk, span, vkey, plan.rate, filt)
        wanted.add(key)
        if self.blocks.get(dk, span, vkey, plan.rate, filt) is not None:
            return key
        job = self._jobs.get(key)
        if job is not None and not job.token.cancelled:
            job.priority = min(job.priority, priority)
            job.generation = req.generation
            return key
        job = _Job(
            key,
            "envelope" if req.overview else "samples",
            plan,
            req.rows,
            span,
            settings,
            req.plot_px,
            priority,
            req.generation,
            seq=next(self._seq),
        )
        if not req.overview:
            for k in pipe.tiles_needed(self.data, plan.rate, span, settings):
                job.tile_numbers.append(k)
                job.tile_keys.append(TileScheduler.key_of(plan.rate, plan.channels, k, dk))
            with self._held_lock:  # before asking: a fast tile must be kept
                self._needed.update(job.tile_keys)
            for k in job.tile_numbers:
                self.scheduler.request(plan.rate, plan.channels, k, priority, req.generation)
        self._jobs[key] = job
        self._update_needed()
        return key

    def _update_needed(self) -> None:
        need = {k for j in self._jobs.values() if j.state == "waiting" for k in j.tile_keys}
        with self._held_lock:
            self._needed = need
            for k in [k for k in self._held if k not in need]:
                self._held_bytes -= int(getattr(self._held.pop(k), "nbytes", 0))

    def _drop_stale(self, gen: int, wanted: set) -> None:
        for key, job in list(self._jobs.items()):
            if key in wanted:
                job.generation = max(job.generation, gen)
                continue
            if job.generation < gen:
                job.token.cancel()
                del self._jobs[key]
                if job.state == "computing":
                    self._zombies.add(job)  # still running: counted until it ends
        self._update_needed()
        self.scheduler.cancel_stale(gen)

    def _look_ahead(self, req: _Request, direction: int, wanted: set) -> None:
        """Tiles 2 pages ahead and 1 behind; the filtered block of the next
        page in the direction of travel; the screens above and below."""
        length = req.span[1] - req.span[0]
        page_len = int(length / 1.5)
        groups = [
            ChannelGroup(p.rate, p.channels) for gi, p in enumerate(req.plans) if gi in req.keys
        ]
        t0 = req.span[0]
        plan = self.scheduler.prefetch_plan(
            t0,
            t0 + page_len,
            direction,
            groups,
            generation=req.generation,
            ahead_pages=self.perf.lookahead_pages,
        )
        self.scheduler.request_all(plan)
        nxt = aligned_block(
            next_block_start(req.span, page_len, direction), page_len, self.data.timeline_start
        )
        if nxt[0] >= self.data.timeline_start - page_len and nxt[0] < self.data.timeline_end:
            for gi in list(req.keys):
                self._ensure(req, gi, nxt, LOOKAHEAD, wanted)
        # the screens above and below
        first, last = req.visible
        n = max(1, last - first)
        for gi, p in enumerate(req.plans):
            if gi in req.keys:
                continue
            if any(first - n <= ri < last + n for ri in p.rows):
                self._ensure(req, gi, req.span, SCREENS, wanted)
        self._drop_stale(req.generation, wanted)

    # -- compute pool ----------

    def _pump(self, *_args) -> None:
        if self._closed:
            return
        inflight = sum(1 for j in self._jobs.values() if j.state == "computing")
        inflight += len(self._zombies)
        waiting = sorted(
            (j for j in self._jobs.values() if j.state == "waiting"),
            key=lambda j: (j.priority, j.seq),
        )
        for job in waiting:
            if inflight >= self.max_compute:
                break
            missing = self._missing(job)
            if missing:
                now = time.monotonic()
                if self.scheduler.pending() == 0 or now - job.last_ask >= RETRY_AFTER:
                    # a tile is neither here nor on its way (lost): ask again
                    if job.retries >= MAX_RETRIES:
                        self._give_up(job)
                        continue
                    job.retries += 1
                    job.last_ask = now
                    for k in missing:
                        self.scheduler.request(
                            job.plan.rate, job.plan.channels, k, job.priority, job.generation
                        )
                continue
            self._take_tiles(job)
            job.state = "computing"
            self._update_needed()
            inflight += 1
            self._pool.submit(self._compute, job)
        self._collect()

    def _give_up(self, job: _Job) -> None:
        job.token.cancel()
        if self._jobs.get(job.key) is job:
            del self._jobs[job.key]
        self._update_needed()
        self._fail_keys({job.key})

    def _take_tiles(self, job: _Job) -> None:
        """Hand the held arrays to the job; the controller lets go of them."""
        with self._held_lock:
            for key in job.tile_keys:
                if key in self._held:
                    job.arrays[key] = self._held[key]

    def _missing(self, job: _Job) -> list[int]:
        out = []
        with self._held_lock:
            for k, key in zip(job.tile_numbers, job.tile_keys, strict=True):
                if key not in self._held and key not in self.cache:
                    out.append(k)
        return out

    def _compute(self, job: _Job) -> None:
        data, cache = self.data, self.cache
        plan = job.plan
        try:
            job.token.raise_if_cancelled()
            if job.kind == "envelope":
                piece = pipe.compute_envelope(data, plan.channels, job.span, job.token)
            else:
                dk = data_key_of(data)

                def get_tile(k: int):
                    key = TileScheduler.key_of(plan.rate, plan.channels, k, dk)
                    tile = job.arrays.get(key)
                    if tile is None:
                        tile = cache.get(key)
                    if tile is None:  # lost meanwhile: read it here
                        job.token.raise_if_cancelled()
                        tile = data.read_tile(list(plan.channels), plan.rate, k, job.token)
                        cache.put(key, tile)
                    return tile

                piece = pipe.compute_piece(
                    data, get_tile, plan, job.rows, job.span, job.settings, job.plot_px, job.token
                )
            job.arrays.clear()
            self.cache.put(job.key, piece)
            outcome, error = "ok", piece
        except BaseException as exc:  # noqa: BLE001 - a worker must survive
            outcome, error = (
                ("stopped", None)
                if (is_stop(exc) or isinstance(exc, JobCancelled))
                else ("error", exc)
            )
        job.arrays.clear()
        if not self._closed:
            try:
                self._done.emit(job, (outcome, error))
            except RuntimeError:  # the controller is already deleted
                pass

    def _on_done(self, job, result) -> None:
        self._zombies.discard(job)
        if self._jobs.get(job.key) is job:
            del self._jobs[job.key]
        self._update_needed()
        outcome, error = result
        if outcome == "ok" and self._req is not None:
            # also when the cache refused it (a block bigger than the whole budget)
            for gi, key in self._req.keys.items():
                if key == job.key and gi not in self._req.pieces:
                    self._req.pieces[gi] = error
                    self.pieces_changed.emit()
        if outcome == "error":
            log.error("view block failed with %s: %s", type(error).__name__, scrub(str(error)))
            self._fail_keys({job.key})
        self._pump()

    def _on_error(self, tile_key, exc) -> None:
        log.error("tile read failed with %s: %s", type(exc).__name__, scrub(str(exc)))
        broken = {k for k, j in self._jobs.items() if tile_key in j.tile_keys}
        for k in broken:
            del self._jobs[k]
        self._update_needed()
        self._fail_keys(broken)

    def _fail_keys(self, keys: set) -> None:
        req = self._req
        if req is not None:
            for gi, k in req.keys.items():
                if k in keys:
                    req.failed.add(gi)
        self.failed.emit("Part of the signal could not be read. Nothing was changed.")
        self._set_loading(self._visible_pending(req) if req else False)

    # -- results ----------

    def _collect(self) -> None:
        """Move finished blocks of the current request into it."""
        req = self._req
        if req is None:
            return
        changed = False
        for gi, key in req.keys.items():
            if gi in req.pieces:
                continue
            piece = self.cache.get(key)
            if piece is not None:
                req.pieces[gi] = piece
                changed = True
        if changed:
            self.pieces_changed.emit()
        self._set_loading(self._visible_pending(req))

    def _visible_pending(self, req: _Request) -> bool:
        return any(gi not in req.pieces and gi not in req.failed for gi in req.keys)

    def _set_loading(self, loading: bool) -> None:
        if loading != self._loading:
            self._loading = loading
            self.loading_changed.emit(loading)

    @property
    def loading(self) -> bool:
        return self._loading

    def current_pieces(self) -> list[tuple[object, int] | None]:
        """For each row of the current request: ``(piece, local row)`` or
        None while its block is not ready."""
        req = self._req
        if req is None:
            return []
        out: list[tuple[object, int] | None] = []
        for i in range(len(req.rows)):
            gi, local = req.location.get(i, (-1, 0))
            piece = req.pieces.get(gi)
            out.append(None if piece is None else (piece, local))
        return out

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until the scheduler and compute pool are idle (tests)."""
        import time

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self._jobs and self.scheduler.pending() == 0:
                return True
            time.sleep(0.01)
        return False

    # -- the strip ----------

    def request_strip_envelope(self, channel: str) -> None:
        data = self.data

        def work():
            try:
                e = data.envelope(channel, data.timeline_start, data.timeline_end)
                env = envelope_bins(
                    e["start_uutc"], e["end_uutc"], e["min"], e["max"],
                    (data.timeline_start, data.timeline_end), STRIP_BINS,
                )  # fmt: skip
            except BaseException:  # noqa: BLE001 - the strip simply stays plain
                return
            if not self._closed:
                self._strip.emit(env)

        self._strip_pool.submit(work)

    # -- lifecycle ----------

    def close(self) -> None:
        """Mark closed and cancel at once (cheap, GUI thread); stopping the
        scheduler and pools happens in :meth:`finish_close`."""
        self._closed = True
        self._watch.stop()
        for job in [*self._jobs.values(), *self._zombies]:
            job.token.cancel()
        self._jobs.clear()

    def finish_close(self) -> None:
        """Stop the scheduler, then the pools (waiting for running work).
        Blocking: call from a background thread."""
        self.scheduler.close()
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._strip_pool.shutdown(wait=True, cancel_futures=True)
        self.cache.clear()


def _envelope_plans(rows: list[montage.Row]) -> list[pipe.GroupPlan]:
    """Overview: rows in groups of 16, as recorded (the envelope has no montage)."""
    from ..workers.viewer_scheduler import GROUP_SIZE

    out = []
    for a in range(0, len(rows), GROUP_SIZE):
        idx = tuple(range(a, min(len(rows), a + GROUP_SIZE)))
        out.append(pipe.GroupPlan(0.0, tuple(rows[i].plus for i in idx), idx))
    return out


__all__ = ["ViewController"]
