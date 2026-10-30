"""Signal filters for the viewer: design, response, filtering, settling.

Pure numpy (no scipy, no Qt). Spec: ``docs/design/viewer.md`` ("Filters").

* **One response function.** :func:`applied_response` is the single place the
  filter's frequency response is defined. The filtering multiplies a signal's
  spectrum by it and the response plot draws it, so they cannot disagree.
  Zero-phase mode applies ``|H|^2`` (real); causal mode applies ``H``.
* **Butterworth** high/low-pass: the analog prototype mapped with the bilinear
  transform (cutoff pre-warped), evaluated analytically at the requested
  frequencies. No difference equation, so a 0.5 Hz cutoff at 8 kHz is no
  numerical problem.
* **Cutoff means the same in both modes:** the applied response is exactly
  1/sqrt(2) (-3 dB, 70.7%) at the cutoff. Zero-phase moves the design cutoff
  so that ``|H|^2`` (not ``|H|``) is 1/sqrt(2) there.
* **Notch:** second-order bilinear notch (RBJ form), quality factor by width:
  narrow 50, normal 30, wide 10. Harmonics (2x, 3x, ...) are added up to
  ``min(low-pass cutoff, 0.98 * Nyquist)``.
* **Settling time** is measured on the step response of the response actually
  applied (see :func:`settling_samples`); the viewer reads that much margin on
  each side of a page.

Two deliberate deviations from the spec text. The edge test (a page filtered
with its margin against the whole signal) allows 3e-3 of the signal RMS, not
1e-3, because the 1e-3 settling criterion is relative to a unit step and a
signal of amplitude ~1 inherits about that error per side. The odd-reflection
pad and the zero tail are sized from the context actually present around the
kept part (none when the page carries a full settling margin, and runs are
cropped to margin plus page), which keeps the same accuracy with a shorter FFT.

Never logs anything.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

#: Quality factor of the mains notch for each width.
NOTCH_Q = {"narrow": 50.0, "normal": 30.0, "wide": 10.0}
#: Cutoffs are clamped to this fraction of the Nyquist frequency.
NYQUIST_FRACTION = 0.98
#: Settling tolerance (relative to the step height) and cap.
SETTLE_TOLERANCE = 1e-3
SETTLE_CAP_S = 60.0
#: Gaps of up to this many NaN samples are bridged linearly before filtering.
BRIDGE_MAX = 2

_SQRT2 = math.sqrt(2.0)


@dataclass(frozen=True)
class FilterSettings:
    """What the filter panel chooses. ``None`` = that filter is off."""

    highpass_hz: float | None = None
    lowpass_hz: float | None = None
    mains_hz: float | None = None
    mains_width: str = "normal"
    mains_harmonics: bool = False
    order: int = 4
    zero_phase: bool = True

    def __post_init__(self) -> None:
        if self.order not in (2, 4, 8):
            raise ValueError("order must be 2, 4 or 8")
        if self.mains_width not in NOTCH_Q:
            raise ValueError("mains_width must be narrow, normal or wide")
        for v in (self.highpass_hz, self.lowpass_hz, self.mains_hz):
            if v is not None and not v > 0:
                raise ValueError("frequencies must be positive")

    @property
    def is_off(self) -> bool:
        return self.highpass_hz is None and self.lowpass_hz is None and self.mains_hz is None

    def summary(self) -> str:
        """Short toolbar text: ``0.5–70 Hz, 60 Hz notch`` or ``No filters``."""
        parts: list[str] = []
        hp, lp = self.highpass_hz, self.lowpass_hz
        if hp is not None and lp is not None:
            parts.append(f"{hp:g}–{lp:g} Hz")
        elif hp is not None:
            parts.append(f"above {hp:g} Hz")
        elif lp is not None:
            parts.append(f"up to {lp:g} Hz")
        if self.mains_hz is not None:
            parts.append(f"{self.mains_hz:g} Hz notch")
        return ", ".join(parts) if parts else "No filters"


def inactive_filters(settings: FilterSettings, fs: float) -> tuple[str, ...]:
    """Filters that do not apply at this rate: a cutoff at or above Nyquist.

    Names: ``"highpass"``, ``"lowpass"``, ``"mains"``. The mains notch also
    counts as inactive above 98% of Nyquist (it would be a spike at the edge).
    """
    nyq = fs / 2.0
    out = []
    if settings.highpass_hz is not None and settings.highpass_hz >= nyq:
        out.append("highpass")
    if settings.lowpass_hz is not None and settings.lowpass_hz >= nyq:
        out.append("lowpass")
    if settings.mains_hz is not None and settings.mains_hz >= NYQUIST_FRACTION * nyq:
        out.append("mains")
    return tuple(out)


def effective(settings: FilterSettings, fs: float) -> FilterSettings:
    """The settings as applied at ``fs``: inactive filters off, cutoffs clamped."""
    off = inactive_filters(settings, fs)
    top = NYQUIST_FRACTION * fs / 2.0
    hp = None if "highpass" in off else settings.highpass_hz
    lp = None if "lowpass" in off else settings.lowpass_hz
    mains = None if "mains" in off else settings.mains_hz
    if hp is not None:
        hp = min(hp, top)
    if lp is not None:
        lp = min(lp, top)
    if (hp, lp, mains) == (settings.highpass_hz, settings.lowpass_hz, settings.mains_hz):
        return settings
    return FilterSettings(
        hp,
        lp,
        mains,
        settings.mains_width,
        settings.mains_harmonics,
        settings.order,
        settings.zero_phase,
    )


def notch_frequencies(settings: FilterSettings, fs: float) -> list[float]:
    """Centre frequencies of the mains notches applied at ``fs``."""
    eff = effective(settings, fs)
    if eff.mains_hz is None:
        return []
    out = [eff.mains_hz]
    if eff.mains_harmonics:
        limit = NYQUIST_FRACTION * fs / 2.0
        if eff.lowpass_hz is not None:
            limit = min(limit, eff.lowpass_hz)
        k = 2
        while k * eff.mains_hz <= limit:
            out.append(k * eff.mains_hz)
            k += 1
    return out


# --- response --------------------------------------------------------------


def _poles(order: int) -> np.ndarray:
    k = np.arange(1, order + 1)
    return np.exp(1j * np.pi * (2 * k + order - 1) / (2 * order))


def _butterworth(
    freqs: np.ndarray, fs: float, fc: float, order: int, highpass: bool, zero_phase: bool
) -> np.ndarray:
    """Complex H of the bilinear Butterworth; the design cutoff is moved in
    zero-phase mode so the applied |H|^2 is 1/sqrt(2) at ``fc``."""
    t_c = math.tan(math.pi * fc / fs)
    if zero_phase:
        if highpass:
            u_c = (_SQRT2 + 1.0) ** (1.0 / (2 * order))
        else:
            u_c = (_SQRT2 - 1.0) ** (1.0 / (2 * order))
        t_c = t_c / u_c
    u = np.tan(np.pi * np.asarray(freqs, dtype=np.float64) / fs) / t_c
    s = 1j * u
    h = np.ones(u.shape, dtype=np.complex128)
    for p in _poles(order):
        h = h * ((s / (s - p)) if highpass else (1.0 / (s - p)))
    return h


def _notch(freqs: np.ndarray, fs: float, f0: float, q: float) -> np.ndarray:
    w0 = 2.0 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2.0 * q)
    c = math.cos(w0)
    e1 = np.exp(-1j * 2.0 * np.pi * np.asarray(freqs, dtype=np.float64) / fs)
    e2 = e1 * e1
    return (1.0 - 2.0 * c * e1 + e2) / ((1.0 + alpha) - 2.0 * c * e1 + (1.0 - alpha) * e2)


def applied_response(settings: FilterSettings, fs: float, freqs) -> np.ndarray:
    """The response the filter multiplies the spectrum by, at ``freqs`` (Hz).

    Causal mode: complex ``H``. Zero-phase mode: real ``|H|^2``. The cutoff of
    a high/low-pass is where this is 1/sqrt(2) in both modes. Filters that do
    not apply at ``fs`` (see :func:`inactive_filters`) are left out.
    """
    freqs = np.asarray(freqs, dtype=np.float64)
    eff = effective(settings, fs)
    h = np.ones(freqs.shape, dtype=np.complex128)
    if eff.highpass_hz is not None:
        h = h * _butterworth(freqs, fs, eff.highpass_hz, eff.order, True, eff.zero_phase)
    if eff.lowpass_hz is not None:
        h = h * _butterworth(freqs, fs, eff.lowpass_hz, eff.order, False, eff.zero_phase)
    q = NOTCH_Q[eff.mains_width]
    for f0 in notch_frequencies(eff, fs):
        h = h * _notch(freqs, fs, f0, q)
    if eff.zero_phase:
        return h.real**2 + h.imag**2
    return h


def group_delay_s(settings: FilterSettings, fs: float, freqs) -> np.ndarray:
    """Time shift in seconds per frequency (causal mode; zeros in zero-phase)."""
    freqs = np.asarray(freqs, dtype=np.float64)
    if settings.zero_phase or settings.is_off:
        return np.zeros(freqs.shape)
    d = np.maximum(freqs * 1e-4, 1e-6)
    lo = np.maximum(freqs - d, 0.0)
    hi = freqs + d
    ratio = applied_response(settings, fs, hi) / np.where(
        applied_response(settings, fs, lo) == 0, 1e-300, applied_response(settings, fs, lo)
    )
    return -np.angle(ratio) / (2.0 * np.pi * (hi - lo))


@lru_cache(maxsize=256)
def settling_samples(settings: FilterSettings, fs: float) -> tuple[int, bool]:
    """Samples until the step response of the APPLIED response has settled.

    Settled = within 1e-3 of its final value (step height 1) from there on.
    Zero-phase: the response is symmetric, measured from its centre, per side.
    Capped at 60 s; the flag says the cap bit. ``(0, False)`` when off.
    """
    eff = effective(settings, fs)
    if eff.is_off:
        return 0, False
    cap = int(SETTLE_CAP_S * fs)
    length = 1 << 13
    while True:
        freqs = np.fft.rfftfreq(length, 1.0 / fs)
        resp = applied_response(eff, fs, freqs)
        h = np.fft.irfft(resp, length)
        if eff.zero_phase:
            step = np.cumsum(np.fft.fftshift(h))[length // 2 :]
        else:
            step = np.cumsum(h)[: length // 2]
        final = float(np.real(resp[0]))
        bad = np.flatnonzero(np.abs(step - final) > SETTLE_TOLERANCE)
        last = int(bad[-1]) + 1 if bad.size else 1
        half = step.size
        if last < half // 2:
            return min(last, cap), last > cap
        if half // 2 >= cap:
            return cap, True
        length *= 2


# --- filtering -------------------------------------------------------------


def _smooth_sizes() -> list[int]:
    out = []
    a = 1
    while a <= 1 << 28:
        b = a
        while b <= 1 << 28:
            c = b
            while c <= 1 << 28:
                out.append(c)
                c *= 5
            b *= 3
        a *= 2
    return sorted(out)


_SMOOTH = _smooth_sizes()


def fast_length(n: int) -> int:
    """Smallest 5-smooth (2^a 3^b 5^c) size >= n."""
    return _SMOOTH[bisect.bisect_left(_SMOOTH, max(1, n))]


def _fast_length_multiple(n: int, m: int) -> int:
    """Smallest 5-smooth size >= n that is a multiple of ``m`` (else any multiple)."""
    if m == 1:
        return fast_length(n)
    for v in _SMOOTH[bisect.bisect_left(_SMOOTH, max(1, n)) :]:
        if v % m == 0:
            return v
    return -(-n // m) * m


def max_out_step(settings: FilterSettings, fs: float) -> int:
    """Largest decimation step the low-pass allows (1 = none).

    A step is allowed when the applied response above the new Nyquist
    ``fs / (2 * step)`` is below 1e-3, so dropping those frequencies is free."""
    eff = effective(settings, fs)
    if eff.lowpass_hz is None:
        return 1
    freqs = np.linspace(0.0, fs / 2.0, 4097)
    mag = np.abs(applied_response(eff, fs, freqs))
    best = 1
    for step in range(2, 257):
        if mag[freqs > fs / (2.0 * step)].max() < 1e-3:
            best = step
        else:
            break
    return best


@lru_cache(maxsize=16)
def _response_grid(settings: FilterSettings, fs: float, nfft: int) -> np.ndarray:
    return applied_response(settings, fs, np.fft.rfftfreq(nfft, 1.0 / fs))


def _bridge(x: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
    """Linearly fill interior NaN gaps of <= BRIDGE_MAX samples.

    Returns the filled copy and the indices that were filled (or None)."""
    nan = np.isnan(x)
    if not nan.any():
        return x, None
    edges = np.flatnonzero(np.diff(np.concatenate(([0], nan.view(np.int8), [0]))))
    starts, ends = edges[0::2], edges[1::2]
    filled = []
    y = None
    for a, b in zip(starts, ends, strict=True):
        if b - a <= BRIDGE_MAX and a > 0 and b < x.size:
            if y is None:
                y = x.astype(np.float64)
            y[a:b] = np.interp(np.arange(a, b), [a - 1, b], [y[a - 1], y[b]])
            filled.append(np.arange(a, b))
    if y is None:
        return x, None
    return y, np.concatenate(filled)


def _finite_runs(x: np.ndarray) -> list[tuple[int, int]]:
    ok = ~np.isnan(x)
    if ok.all():
        return [(0, x.size)]
    edges = np.flatnonzero(np.diff(np.concatenate(([0], ok.view(np.int8), [0]))))
    return list(zip(edges[0::2].tolist(), edges[1::2].tolist(), strict=True))


def _filter_run(
    r: np.ndarray,
    eff: FilterSettings,
    fs: float,
    n_set: int,
    ctx_left: int,
    ctx_right: int,
    step: int = 1,
    lo: int = 0,
) -> tuple[np.ndarray, int]:
    """Filter one NaN-free run ``r`` by FFT.

    ``ctx_left`` / ``ctx_right`` = samples of real data between the run's ends
    and the part the caller keeps. Anything short of one settling time is made
    up by an odd-reflection pad (continues value and slope through the end),
    and the circular wrap is kept one settling time away from the kept part by
    zeros, so what is kept is exact to the settling tolerance.

    Returns ``(values, first)``: with ``step`` 1 the whole run (``first`` 0);
    with ``step`` > 1 only the samples whose global index (``lo`` + local) is a
    multiple of ``step``, ``first`` being the local index of the first one. The
    decimation is done in the frequency domain (shorter inverse FFT).
    """
    n = r.size
    pl = min(max(0, n_set - ctx_left), n - 1)
    pr = min(max(0, n_set - ctx_right), n - 1)
    if step > 1:
        pl += (lo - pl) % step
        while pl > n - 1:
            pl -= step
    gap = max(0, n_set - min(ctx_left + pl, ctx_right + pr))
    nfft = _fast_length_multiple(n + pl + pr + gap, step)
    buf = np.empty(n + pl + pr)
    buf[pl : pl + n] = r
    if pl > 0:
        buf[:pl] = 2.0 * r[0] - r[pl:0:-1]
    if pr > 0:
        buf[pl + n :] = 2.0 * r[-1] - r[-2 : -pr - 2 : -1]
    spec = np.fft.rfft(buf, nfft)
    spec *= _response_grid(eff, fs, nfft)
    if step == 1:
        return np.fft.irfft(spec, nfft)[pl : pl + n], 0
    m = nfft // step
    dec = np.fft.irfft(spec[: m // 2 + 1], m) * (m / nfft)
    k0 = -(-pl // step)
    k1 = -(-(pl + n) // step)
    return dec[k0:k1], k0 * step - pl


def filter_block(
    x: np.ndarray,
    fs: float,
    settings: FilterSettings,
    keep_from: int,
    keep_to: int,
    out_step: int = 1,
) -> tuple[np.ndarray, np.ndarray]:
    """Filter ``x`` (float32 ``[channels, n]``, margins included, NaN gaps).

    Returns ``(y, unfiltered)``: the ``[keep_from:keep_to]`` slice as float32
    and a bool mask of the same shape that is True where a sample belongs to a
    NaN-free run shorter than the settling time (returned unfiltered).
    Gaps of up to 2 samples are bridged and put back to NaN; longer gaps split
    a channel into runs that are filtered independently, each with odd
    reflection padding.

    ``out_step`` > 1 returns every ``out_step``-th sample, decimated in the
    frequency domain. It is clamped to :func:`max_out_step`. The returned
    samples are those at array indices that are multiples of ``out_step``
    inside ``[keep_from, keep_to)``: ``keep_from`` is effectively rounded UP
    to the next multiple, so the result equals the full-rate output
    ``[ceil(keep_from / out_step) * out_step : keep_to : out_step]``.
    """
    x = np.asarray(x)
    squeeze = x.ndim == 1
    if squeeze:
        x = x[None, :]
    eff = effective(settings, fs)
    step = max(1, min(int(out_step), max_out_step(eff, fs)))
    first = -(-keep_from // step) * step
    if eff.is_off:
        y = np.ascontiguousarray(x[:, first:keep_to:step], dtype=np.float32)
        mask = np.zeros(y.shape, dtype=bool)
    else:
        n_set, _ = settling_samples(eff, fs)
        n_out = len(range(first, keep_to, step))
        y = np.full((x.shape[0], n_out), np.nan, dtype=np.float32)
        mask = np.zeros((x.shape[0], n_out), dtype=bool)
        for c in range(x.shape[0]):
            row, bridged = _bridge(x[c])
            res = np.full(x.shape[1], np.nan)
            mfull = np.zeros(x.shape[1], dtype=bool) if step > 1 else None
            for a, b in _finite_runs(row):
                if b <= keep_from or a >= keep_to:
                    continue
                if b - a < n_set:
                    res[a:b] = row[a:b]
                    if mfull is not None:
                        mfull[a:b] = True
                    else:
                        mask[c, max(a, keep_from) - keep_from : min(b, keep_to) - keep_from] = True
                else:
                    # only the data within one settling time of the kept part matters
                    lo = max(a, keep_from - n_set)
                    hi = min(b, keep_to + n_set)
                    vals, f0 = _filter_run(
                        row[lo:hi].astype(np.float64),
                        eff,
                        fs,
                        n_set,
                        max(0, keep_from - lo),
                        max(0, hi - keep_to),
                        step,
                        lo,
                    )
                    if step == 1:
                        res[lo:hi] = vals
                    else:
                        res[lo + f0 : lo + f0 + vals.size * step : step] = vals
            if bridged is not None:
                res[bridged] = np.nan
            y[c] = res[first:keep_to:step]
            if mfull is not None:
                mask[c] = mfull[first:keep_to:step]
    if squeeze:
        return y[0], mask[0]
    return y, mask


# --- words and drawing -----------------------------------------------------


def _pct(frac: float) -> str:
    p = 100.0 * frac
    if p < 0.05:
        return "less than 0.1%"
    if p < 1.0:
        return f"{p:.1f}%"
    return f"{min(round(p), 100)}%"


def _num(v: float) -> str:
    return f"{v:.2g}" if v < 10 else f"{v:.0f}"


def _kept(settings: FilterSettings, fs: float, f: float) -> float:
    return float(np.abs(applied_response(settings, fs, [f]))[0])


def describe(settings: FilterSettings, fs: float) -> list[str]:
    """Plain-language lines about what the filters do at sampling rate ``fs``."""
    if settings.is_off:
        return ["No filters are on."]
    lines: list[str] = []
    eff = effective(settings, fs)
    for name in inactive_filters(settings, fs):
        what = {
            "highpass": "The slow-drift filter",
            "lowpass": "The fast-activity filter",
            "mains": "The mains filter",
        }[name]
        lines.append(f"{what} does not apply here: its frequency is above half the sampling rate.")
    if eff.highpass_hz is not None:
        f = eff.highpass_hz / 5.0
        lines.append(
            f"Slow drifts below {eff.highpass_hz:g} Hz are reduced: "
            f"at {f:g} Hz, {_pct(_kept(eff, fs, f))} is kept."
        )
    if eff.lowpass_hz is not None:
        f = min(eff.lowpass_hz * 2.0, fs / 2.0 * 0.999)
        lines.append(
            f"Fast activity above {eff.lowpass_hz:g} Hz is reduced: "
            f"at {f:g} Hz, {_pct(_kept(eff, fs, f))} is kept."
        )
    if eff.mains_hz is not None:
        lines.append(f"{eff.mains_hz:g} Hz mains: {_pct(_kept(eff, fs, eff.mains_hz))} kept.")
        extra = notch_frequencies(eff, fs)[1:]
        if extra:
            lines.append(
                f"Multiples of {eff.mains_hz:g} Hz are removed too, up to {extra[-1]:g} Hz."
            )
    span = _kept_span(eff, fs)
    if span is not None:
        lines.append(
            f"Brain activity between {_num(span[0])} and {_num(span[1])} Hz: at least 95% kept."
        )
    return lines


def _kept_span(eff: FilterSettings, fs: float) -> tuple[float, float] | None:
    """The widest (in octaves) frequency stretch keeping at least 95%."""
    grid = np.geomspace(0.01, NYQUIST_FRACTION * fs / 2.0, 4000)
    ok = np.abs(applied_response(eff, fs, grid)) >= 0.95
    if not ok.any():
        return None
    edges = np.flatnonzero(np.diff(np.concatenate(([0], ok.view(np.int8), [0]))))
    best = None
    for a, b in zip(edges[0::2], edges[1::2], strict=True):
        lo, hi = float(grid[a]), float(grid[b - 1])
        if best is None or hi / lo > best[1] / best[0]:
            best = (lo, hi)
    if best is None or best[0] <= 0.0101 and best[1] >= grid[-1] * 0.999:
        return None
    return best


def minmax_columns(y: np.ndarray, n_cols: int) -> tuple[np.ndarray, np.ndarray]:
    """NaN-aware minimum and maximum per pixel column along the last axis.

    A column with only NaN gives NaN. With fewer samples than columns each
    column takes the sample under it.
    """
    y = np.asarray(y)
    n = y.shape[-1]
    if n == 0 or n_cols <= 0:
        shape = y.shape[:-1] + (max(n_cols, 0),)
        return np.full(shape, np.nan), np.full(shape, np.nan)
    if n <= n_cols:
        idx = (np.arange(n_cols) * n) // n_cols
        v = y[..., idx]
        return v.copy(), v.copy()
    edges = (np.arange(n_cols) * n) // n_cols
    with np.errstate(all="ignore"):
        return np.fmin.reduceat(y, edges, axis=-1), np.fmax.reduceat(y, edges, axis=-1)
