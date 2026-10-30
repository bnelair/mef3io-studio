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

#: A block's intermediate arrays (rows x samples, margins included) stay under
#: this many samples: bigger blocks are computed in chunks of rows.
MAX_BLOCK_SAMPLES = 16_000_000


@dataclass
class ViewPiece:
    """Filtered rows of one group over one block.

    ``paired`` pieces are reduced to the minimum and maximum of each bin
    (about one bin per pixel column): ``values`` holds ``(min, max)`` pairs,
    ``t_first_us`` is the centre of the first bin and ``dt_us`` the bin
    length. Otherwise one value per sample at a regular step."""

    values: np.ndarray  # float32 [rows, n] (n = 2 * bins when paired)
    unfiltered: np.ndarray  # bool, same shape
    t_first_us: float
    dt_us: float
    paired: bool = False

    @property
    def nbytes(self) -> int:
        return int(self.values.nbytes + self.unfiltered.nbytes)

    @property
    def n_points(self) -> int:
        return self.values.shape[1] // 2 if self.paired else self.values.shape[1]

    def window(self, row: int, ta: float, tb: float) -> np.ndarray:
        """The row's values with time in [ta, tb]."""
        lo, hi = (ta, tb) if ta <= tb else (tb, ta)
        n = self.n_points
        j0 = max(0, math.ceil((lo - self.t_first_us) / self.dt_us))
        j1 = min(n, math.floor((hi - self.t_first_us) / self.dt_us) + 1)
        if j1 <= j0:
            return self.values[row, :0]
        if self.paired:
            return self.values[row, 2 * j0 : 2 * j1]
        return self.values[row, j0:j1]

    def minmax(self, row: int, ta: float, tb: float) -> tuple[float, float] | None:
        w = self.window(row, ta, tb)
        w = w[np.isfinite(w)]
        if w.size == 0:
            return None
        return float(w.min()), float(w.max())


def reduce_pairs(
    values: np.ndarray, mask: np.ndarray, t_first: float, dt: float, bins_wanted: int
) -> ViewPiece:
    """Reduce to (min, max) pairs when there are clearly more samples than
    ``bins_wanted``; otherwise keep every sample."""
    n = values.shape[1]
    k = n // max(1, bins_wanted)
    if k < 2:
        return ViewPiece(values, mask, t_first, dt)
    m = -(-n // k)
    pad = m * k - n
    if pad:
        values = np.concatenate([values, np.full((values.shape[0], pad), np.nan, np.float32)], 1)
        mask = np.concatenate([mask, np.zeros((mask.shape[0], pad), bool)], 1)
    v3 = values.reshape(values.shape[0], m, k)
    out = np.empty((values.shape[0], 2 * m), dtype=np.float32)
    with np.errstate(all="ignore"):
        out[:, 0::2] = np.fmin.reduce(v3, axis=2)
        out[:, 1::2] = np.fmax.reduce(v3, axis=2)
    um = mask.reshape(mask.shape[0], m, k).any(axis=2)
    flags = np.repeat(um, 2, axis=1)
    return ViewPiece(out, flags, t_first + (k - 1) / 2 * dt, k * dt, paired=True)


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
    """Assemble, re-reference, filter and reduce one group over one block.
    Large blocks are done in chunks of rows (bounded memory)."""
    rate = plan.rate
    i_s, i_e = sample_range(data, rate, span)
    m = margin_samples(settings, rate)
    i_a, i_b = i_s - m, i_e + m
    length = max(0, i_b - i_a)
    want, _ = out_step_for(data, rate, span, settings, plot_px)
    step = dsp.effective_out_step(settings, rate, want) if not settings.is_off else 1
    keep_from, keep_to = m, m + (i_e - i_s)
    first = -(-keep_from // step) * step
    t_first = data.timeline_start + (i_a + first) * 1e6 / rate
    dt = step * 1e6 / rate
    my_rows = [rows[i] for i in plan.rows]
    per_chunk = max(1, MAX_BLOCK_SAMPLES // max(1, length))
    tiles = tiles_needed(data, rate, span, settings)
    outs, masks = [], []
    for a in range(0, len(my_rows), per_chunk):
        chunk = my_rows[a : a + per_chunk]
        names = montage.required_channels(chunk)
        index = {name: plan.channels.index(name) for name in names}
        x = np.full((len(names), length), np.nan, dtype=np.float32)
        for k in tiles:
            if cancel is not None:
                cancel.raise_if_cancelled()
            tile = get_tile(k)
            lo = max(i_a, k * TILE_SAMPLES)
            hi = min(i_b, (k + 1) * TILE_SAMPLES)
            if hi > lo:
                x[:, lo - i_a : hi - i_a] = tile[
                    [index[nm] for nm in names], lo - k * TILE_SAMPLES : hi - k * TILE_SAMPLES
                ]
        y = montage.apply(chunk, {nm: x[j] for j, nm in enumerate(names)})
        del x
        out, mask = dsp.filter_block(y, rate, settings, keep_from, keep_to, want)
        outs.append(out)
        masks.append(mask)
    values = np.concatenate(outs) if len(outs) > 1 else outs[0]
    mask = np.concatenate(masks) if len(masks) > 1 else masks[0]
    return reduce_pairs(values, mask, t_first, dt, max(1, int(1.5 * plot_px)))


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
