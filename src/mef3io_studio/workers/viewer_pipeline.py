"""From tiles to ready-to-draw view blocks (docs/design/viewer.md, "Data").

Plain numpy, no Qt. A *view block* is 1.5 page lengths of one group of rows
(same sampling rate): the raw tiles around it, the montage computed over the
margin-extended window, then :func:`~..model.dsp.filter_block` keeping only
the block, decimated as far as the pixel width allows. A page always lies
inside one block (``viewer_view.aligned_block``), so painting needs one
block per group. Overview blocks hold the index's min/max instead.

Runs on worker threads; nothing here logs.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from ..model import dsp, montage
from ..model.cancel import CancelToken
from ..model.viewer_source import TILE_SAMPLES
from .viewer_scheduler import ChannelGroup, group_channels


@dataclass
class ViewPiece:
    """Filtered rows of one group over one block, at a regular step."""

    values: np.ndarray  # float32 [rows, n]
    unfiltered: np.ndarray  # bool [rows, n]: a run too short to filter
    t_first_us: float
    dt_us: float

    @property
    def nbytes(self) -> int:
        return int(self.values.nbytes + self.unfiltered.nbytes)

    def window(self, row: int, ta: float, tb: float) -> np.ndarray:
        """The row's samples with time in [ta, tb]."""
        lo, hi = (ta, tb) if ta <= tb else (tb, ta)
        n = self.values.shape[1]
        j0 = max(0, math.ceil((lo - self.t_first_us) / self.dt_us))
        j1 = min(n, math.floor((hi - self.t_first_us) / self.dt_us) + 1)
        return self.values[row, j0:j1] if j1 > j0 else self.values[row, :0]

    def minmax(self, row: int, ta: float, tb: float) -> tuple[float, float] | None:
        w = self.window(row, ta, tb)
        w = w[np.isfinite(w)]
        if w.size == 0:
            return None
        return float(w.min()), float(w.max())


@dataclass
class EnvelopePiece:
    """Per-row block extremes from the index: arrays ``[4, n]`` of start
    time, end time, minimum, maximum."""

    rows: list[np.ndarray]

    @property
    def nbytes(self) -> int:
        return int(sum(r.nbytes for r in self.rows))

    def minmax(self, row: int, ta: float, tb: float) -> tuple[float, float] | None:
        lo, hi = (ta, tb) if ta <= tb else (tb, ta)
        a = self.rows[row]
        sel = (a[1] > lo) & (a[0] < hi) & np.isfinite(a[2]) & np.isfinite(a[3])
        if not sel.any():
            return None
        return float(a[2][sel].min()), float(a[3][sel].max())


@dataclass(frozen=True)
class GroupPlan:
    rate: float
    channels: tuple[str, ...]  # tile channels (rows' channels + partners)
    rows: tuple[int, ...]  # indexes into the montage row list


def plan_groups(data, rows: Sequence[montage.Row]) -> list[GroupPlan]:
    """Split montage rows into groups sharing a rate, with each row's
    partner channel fetched in the same group (docs: "always fetched with
    the group that needs them")."""
    plus = list(dict.fromkeys(r.plus for r in rows))
    partners: dict[str, set[str]] = {}
    for r in rows:
        if r.minus:
            partners.setdefault(r.plus, set()).add(r.minus)
    groups: list[ChannelGroup] = group_channels(data, plus, partners=partners)
    members: list[list[int]] = [[] for _ in groups]
    for i, r in enumerate(rows):
        for gi, g in enumerate(groups):
            if r.plus in g.channels and (r.minus is None or r.minus in g.channels):
                members[gi].append(i)
                break
        else:  # pragma: no cover - group_channels puts partners of equal rate in
            raise ValueError(f"No group for the row '{r.label}'.")
    return [
        GroupPlan(g.rate, g.channels, tuple(m)) for g, m in zip(groups, members, strict=True) if m
    ]


def sample_range(data, rate: float, span: tuple[int, int]) -> tuple[int, int]:
    """Sample indexes [i_s, i_e) (on the rate's grid) of a time span."""
    tl = data.timeline_start
    i_s = math.ceil((span[0] - tl) * rate / 1e6 - 1e-9)
    i_e = math.ceil((span[1] - tl) * rate / 1e6 - 1e-9)
    return i_s, max(i_s, i_e)


def margin_samples(settings: dsp.FilterSettings, rate: float) -> int:
    return dsp.settling_samples(settings, rate)[0] if not settings.is_off else 0


def tiles_needed(data, rate: float, span: tuple[int, int], settings) -> list[int]:
    """Tile numbers holding the span plus the filter's margin on each side."""
    i_s, i_e = sample_range(data, rate, span)
    m = margin_samples(settings, rate)
    i_a, i_b = i_s - m, i_e + m
    last = data.tile_count(rate) - 1
    first_k = max(0, i_a // TILE_SAMPLES)
    last_k = min(last, (i_b - 1) // TILE_SAMPLES)
    return list(range(first_k, last_k + 1)) if last_k >= first_k else []


def out_step_for(
    data, rate: float, span: tuple[int, int], settings, plot_px: int
) -> tuple[int, int]:
    """(requested step, step the filter really uses) for a block drawn over
    ``1.5 * plot_px`` pixels. Without filters nothing is decimated (the
    painter takes min/max per column); with them, as far as the low-pass
    allows while keeping two samples per pixel."""
    if settings.is_off:
        return 1, 1
    i_s, i_e = sample_range(data, rate, span)
    per_px = (i_e - i_s) / max(1.0, 1.5 * plot_px)
    want = max(1, int(per_px // 2))
    return want, dsp.effective_out_step(settings, rate, want)


def view_key(plan: GroupPlan, rows: Sequence[montage.Row], settings, step: int) -> tuple:
    """Everything besides data and span that a block's values depend on."""
    return (
        tuple((rows[i].plus, rows[i].minus) for i in plan.rows),
        plan.rate,
        settings,
        step,
    )


def compute_piece(
    data,
    get_tile: Callable[[int], np.ndarray],
    plan: GroupPlan,
    rows: Sequence[montage.Row],
    span: tuple[int, int],
    settings: dsp.FilterSettings,
    plot_px: int,
    cancel: CancelToken | None = None,
) -> ViewPiece:
    """Assemble, re-reference and filter one group over one block."""
    rate = plan.rate
    i_s, i_e = sample_range(data, rate, span)
    m = margin_samples(settings, rate)
    i_a, i_b = i_s - m, i_e + m
    n_ch = len(plan.channels)
    x = np.full((n_ch, max(0, i_b - i_a)), np.nan, dtype=np.float32)
    for k in tiles_needed(data, rate, span, settings):
        if cancel is not None:
            cancel.raise_if_cancelled()
        tile = get_tile(k)
        lo = max(i_a, k * TILE_SAMPLES)
        hi = min(i_b, (k + 1) * TILE_SAMPLES)
        if hi > lo:
            x[:, lo - i_a : hi - i_a] = tile[:, lo - k * TILE_SAMPLES : hi - k * TILE_SAMPLES]
    if cancel is not None:
        cancel.raise_if_cancelled()
    index = {name: i for i, name in enumerate(plan.channels)}
    y = montage.apply([rows[i] for i in plan.rows], {name: x[index[name]] for name in index})
    want, _ = out_step_for(data, rate, span, settings, plot_px)
    keep_from, keep_to = m, m + (i_e - i_s)
    out, mask = dsp.filter_block(y, rate, settings, keep_from, keep_to, want)
    step = dsp.effective_out_step(settings, rate, want) if not settings.is_off else 1
    first = -(-keep_from // step) * step
    t_first = data.timeline_start + (i_a + first) * 1e6 / rate
    return ViewPiece(out, mask, t_first, step * 1e6 / rate)


def compute_envelope(
    data, channels: Sequence[str], span: tuple[int, int], cancel: CancelToken | None = None
) -> EnvelopePiece:
    """Index-only min/max rows for ``channels`` over ``span`` (as recorded)."""
    rows = []
    for ch in channels:
        if cancel is not None:
            cancel.raise_if_cancelled()
        e = data.envelope(ch, span[0], span[1])
        rows.append(
            np.vstack(
                [
                    np.asarray(e["start_uutc"], dtype=np.float64),
                    np.asarray(e["end_uutc"], dtype=np.float64),
                    np.asarray(e["min"], dtype=np.float64),
                    np.asarray(e["max"], dtype=np.float64),
                ]
            )
        )
    return EnvelopePiece(rows)


def envelope_columns(
    piece_row: np.ndarray, t0: float, t1: float, n_cols: int
) -> tuple[np.ndarray, np.ndarray]:
    """Min and max per pixel column of one envelope row (NaN = no data)."""
    mn = np.full(n_cols, np.nan)
    mx = np.full(n_cols, np.nan)
    if piece_row.shape[1] == 0 or n_cols <= 0 or t1 <= t0:
        return mn, mx
    scale = n_cols / (t1 - t0)
    c0 = np.floor((piece_row[0] - t0) * scale).astype(np.int64)
    c1 = np.floor((piece_row[1] - 1 - t0) * scale).astype(np.int64)
    c0 = np.clip(c0, 0, n_cols - 1)
    c1 = np.clip(c1, 0, n_cols - 1)
    ok = (piece_row[1] > t0) & (piece_row[0] < t1) & np.isfinite(piece_row[2])
    counts = np.where(ok, c1 - c0 + 1, 0)
    if counts.sum() == 0:
        return mn, mx
    which = np.repeat(np.arange(counts.size), counts)
    offset = np.arange(which.size) - np.repeat(np.cumsum(counts) - counts, counts)
    cols = c0[which] + offset
    np.fmin.at(mn, cols, piece_row[2][which])
    np.fmax.at(mx, cols, piece_row[3][which])
    return mn, mx
