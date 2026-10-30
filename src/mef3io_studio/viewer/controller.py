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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QObject, Signal

from ..model import dsp, montage
from ..model.cancel import CancelToken, JobCancelled, is_stop
from ..model.viewer_view import aligned_block, next_block_start
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
STRIP_BINS = 2000


@dataclass
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

    def __init__(self, data, budget_bytes: int, workers: int | None = None, parent=None):
        super().__init__(parent)
        self.data = data
        self.cache = TileCache(budget_bytes)
        self.blocks = ViewBlockCache(self.cache)
        self.scheduler = TileScheduler(
            data, self.cache, workers=workers, on_ready=self._on_tile, on_error=self._on_tile_error
        )
        self._pool = ThreadPoolExecutor(MAX_COMPUTE, thread_name_prefix="viewer-compute")
        self._strip_pool = ThreadPoolExecutor(1, thread_name_prefix="viewer-strip")
        self._jobs: dict[tuple, _Job] = {}
        self._seq = itertools.count()
        self._generation = 0
        self._req: _Request | None = None
        self._loading = False
        self._closed = False
        self._tile.connect(self._pump)
        self._done.connect(self._on_done)
        self._error.connect(self._on_error)
        self._strip.connect(self.strip_envelope)

    # -- worker-thread callbacks: only emit ------------------------------------

    def _on_tile(self, key, _array) -> None:
        if not self._closed:
            self._tile.emit(key)

    def _on_tile_error(self, key, exc) -> None:
        if not self._closed:
            self._error.emit(None, exc)

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
            plans = pipe.plan_groups(data, rows)
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
                job.tile_keys.append(TileScheduler.key_of(plan.rate, plan.channels, k, dk))
                self.scheduler.request(plan.rate, plan.channels, k, priority, req.generation)
        self._jobs[key] = job
        return key

    def _drop_stale(self, gen: int, wanted: set) -> None:
        for key, job in list(self._jobs.items()):
            if key in wanted:
                job.generation = max(job.generation, gen)
                continue
            if job.generation < gen:
                job.token.cancel()
                if job.state == "waiting":
                    del self._jobs[key]
                # a computing job finishes (or stops) and its result is kept
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
            t0, t0 + page_len, direction, groups, generation=req.generation
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
        waiting = sorted(
            (j for j in self._jobs.values() if j.state == "waiting"),
            key=lambda j: (j.priority, j.seq),
        )
        for job in waiting:
            if inflight >= MAX_COMPUTE:
                break
            if all(k in self.cache for k in job.tile_keys):
                job.state = "computing"
                inflight += 1
                self._pool.submit(self._compute, job)
        self._collect()

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
                    tile = cache.get(key)
                    if tile is None:  # evicted meanwhile: read it here
                        tile = data.read_tile(list(plan.channels), plan.rate, k, job.token)
                        cache.put(key, tile)
                    return tile

                piece = pipe.compute_piece(
                    data, get_tile, plan, job.rows, job.span, job.settings, job.plot_px, job.token
                )
            self.cache.put(job.key, piece)
            outcome, error = "ok", None
        except BaseException as exc:  # noqa: BLE001 - a worker must survive
            outcome, error = (
                ("stopped", None)
                if (is_stop(exc) or isinstance(exc, JobCancelled))
                else ("error", exc)
            )
        if not self._closed:
            self._done.emit(job, (outcome, error))

    def _on_done(self, job, result) -> None:
        if self._jobs.get(job.key) is job:
            del self._jobs[job.key]
        outcome, _error = result
        if outcome == "error":
            self.failed.emit("Part of the signal could not be read. Nothing was changed.")
        self._pump()

    def _on_error(self, _key, _exc) -> None:
        self.failed.emit("Part of the signal could not be read. Nothing was changed.")
        self._set_loading(False)

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
        return any(gi not in req.pieces for gi in req.keys)

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
        self._closed = True
        for job in self._jobs.values():
            job.token.cancel()
        self._jobs.clear()
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


__all__ = ["ViewController", "np"]
