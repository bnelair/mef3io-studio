"""Viewer filters: response, filtering, settling, gaps (synthetic signals)."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from mef3io_studio.model import dsp
from mef3io_studio.model.dsp import FilterSettings as FS

RATES = [256.0, 1000.0, 8000.0]


def _amp(y, fs, f):
    """Amplitude and phase of a sinusoid at f by projection."""
    t = np.arange(y.size) / fs
    z = 2 * np.mean(y * np.exp(-2j * np.pi * f * t))
    return abs(z), np.angle(z)


def _run(settings, fs, f, seconds=80.0):
    n = int(seconds * fs)
    t = np.arange(n) / fs
    x = np.sin(2 * np.pi * f * t).astype(np.float32)[None, :]
    a, b = int(30 * fs), int(50 * fs)
    y, _ = dsp.filter_block(x, fs, settings, a, b)
    return y[0].astype(np.float64)


def test_summary_and_off():
    assert FS().summary() == "No filters" and FS().is_off
    assert FS(0.5, 70, 60).summary() == "0.5–70 Hz, 60 Hz notch"
    assert not FS(mains_hz=50).is_off
    with pytest.raises(ValueError):
        FS(order=3)


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("zero_phase", [True, False])
def test_sine_matches_applied_response(fs, zero_phase):
    s = FS(0.5, 70.0, 60.0, "wide", False, 4, zero_phase)
    for f in (1.0, 12.5, 40.0, 59.0, 63.0):
        if f >= fs / 2:
            continue
        y = _run(s, fs, f)
        amp, _ = _amp(y, fs, f)
        want = abs(dsp.applied_response(s, fs, [f])[0])
        assert amp == pytest.approx(want, rel=5e-3, abs=2e-4), (fs, f)


@pytest.mark.parametrize("zero_phase", [True, False])
@pytest.mark.parametrize("order", [2, 4, 8])
def test_cutoff_is_minus_3db_both_modes(zero_phase, order):
    for fs in RATES:
        for kind in ("highpass_hz", "lowpass_hz"):
            fc = 0.5 if kind == "highpass_hz" else 70.0
            s = FS(**{kind: fc}, order=order, zero_phase=zero_phase)
            assert abs(dsp.applied_response(s, fs, [fc])[0]) == pytest.approx(2**-0.5, abs=1e-9)
    s = FS(highpass_hz=1.0, zero_phase=zero_phase)
    y = _run(s, 1000.0, 1.0)
    assert _amp(y, 1000.0, 1.0)[0] == pytest.approx(0.7071, rel=5e-3)


def test_zero_phase_pulse_not_shifted_and_causal_delay_is_group_delay():
    fs = 1000.0
    n = 20000
    x = np.zeros((1, n), np.float32)
    x[0, n // 2] = 1.0
    s = FS(None, 100.0, None, order=4, zero_phase=True)
    y, _ = dsp.filter_block(x, fs, s, 0, n)
    assert int(np.argmax(y[0])) == n // 2
    c = FS(None, 100.0, None, order=4, zero_phase=False)
    f = 10.0
    gd = dsp.group_delay_s(c, fs, [f])[0]
    assert np.all(dsp.group_delay_s(s, fs, [f]) == 0)
    sig = np.sin(2 * np.pi * f * np.arange(n) / fs).astype(np.float32)[None]
    y, _ = dsp.filter_block(sig, fs, c, 5000, 15000)
    _, ph = _amp(y[0].astype(float), fs, f)
    _, ph0 = _amp(sig[0, 5000:15000].astype(float), fs, f)
    delay = -(ph - ph0) / (2 * np.pi * f)
    assert delay == pytest.approx(gd, rel=0.02)


def test_nan_gaps_bridged_and_split():
    fs = 256.0
    n = 4096
    t = np.arange(n) / fs
    x = np.sin(2 * np.pi * 10 * t).astype(np.float32)[None, :].copy()
    s = FS(highpass_hz=1.0, lowpass_hz=40.0)
    x[0, 1000:1002] = np.nan  # bridged
    x[0, 2500:2600] = np.nan  # splits
    y, mask = dsp.filter_block(x, fs, s, 0, n)
    assert np.isnan(y[0, 1000:1002]).all()  # restored
    assert np.isnan(y[0, 2500:2600]).all()
    assert np.isfinite(y[0, 990:1000]).all() and np.isfinite(y[0, 1002:1010]).all()
    assert np.isnan(y).sum() == 102
    assert not mask.any()
    # bridged gap did not split: same values as filtering the interpolated signal
    ref = x.copy()
    ref[0, 1000:1002] = np.interp([1000, 1001], [999, 1002], [x[0, 999], x[0, 1002]])
    yr, _ = dsp.filter_block(ref, fs, s, 0, n)
    np.testing.assert_allclose(y[0, 100:990], yr[0, 100:990], atol=1e-5)


def test_runs_filtered_independently_and_short_runs_unfiltered():
    fs = 256.0
    n = 6000
    rng = np.random.default_rng(1)
    a = rng.standard_normal(2500).astype(np.float32) + 5
    b = rng.standard_normal(2500).astype(np.float32) - 7
    x = np.full((1, n), np.nan, np.float32)
    x[0, :2500] = a
    x[0, 3000:5500] = b
    s = FS(highpass_hz=2.0, lowpass_hz=60.0)
    y, mask = dsp.filter_block(x, fs, s, 0, n)
    ya, _ = dsp.filter_block(a[None], fs, s, 0, 2500)
    yb, _ = dsp.filter_block(b[None], fs, s, 0, 2500)
    np.testing.assert_allclose(y[0, :2500], ya[0], atol=1e-5)
    np.testing.assert_allclose(y[0, 3000:5500], yb[0], atol=1e-5)
    assert not mask.any()
    # a run shorter than the settling time is returned unfiltered
    n_set, _ = dsp.settling_samples(s, fs)
    short = np.full((1, 3 * n_set), np.nan, np.float32)
    short[0, n_set : n_set + 50] = 3.0
    y, mask = dsp.filter_block(short, fs, s, 0, short.shape[1])
    assert mask[0, n_set : n_set + 50].all() and mask.sum() == 50
    assert (y[0, n_set : n_set + 50] == 3.0).all()


@pytest.mark.parametrize("fs", [256.0, 1000.0])
def test_no_edge_step_with_computed_margin(fs):
    s = FS(0.5, 70.0, 60.0, "normal", True, 4, True)
    n_set, capped = dsp.settling_samples(s, fs)
    assert not capped
    rng = np.random.default_rng(2)
    n = int(120 * fs)
    t = np.arange(n) / fs
    x = (
        0.3 * rng.standard_normal(n)
        + 1.0
        + np.sin(2 * np.pi * 9 * t)
        + 0.5 * np.sin(2 * np.pi * 60 * t)
    ).astype(np.float32)[None]
    whole, _ = dsp.filter_block(x, fs, s, 0, n)
    a, b = int(50 * fs), int(60 * fs)
    page, _ = dsp.filter_block(x[:, a - n_set : b + n_set], fs, s, n_set, n_set + (b - a))
    rms = float(np.sqrt(np.mean(whole[0, a:b] ** 2)))
    # the 1e-3 settling criterion is per unit of step; signal ~1 -> a few 1e-3
    assert np.max(np.abs(page[0] - whole[0, a:b])) < 3e-3 * rms


def test_settling_cap_and_cache():
    n, capped = dsp.settling_samples(FS(highpass_hz=0.01), 256.0)
    assert capped and n == 60 * 256
    n2, capped2 = dsp.settling_samples(FS(highpass_hz=1.0), 256.0)
    assert not capped2 and 0 < n2 < n
    assert dsp.settling_samples(FS(), 256.0) == (0, False)
    assert dsp.settling_samples(FS(highpass_hz=1.0), 256.0) == (n2, capped2)


def test_inactive_cutoff_for_low_rate():
    s = FS(lowpass_hz=300.0, mains_hz=60.0)
    assert dsp.inactive_filters(s, 256.0) == ("lowpass",)
    assert dsp.inactive_filters(s, 1000.0) == ()
    h = dsp.applied_response(FS(lowpass_hz=300.0), 256.0, [10.0, 100.0])
    np.testing.assert_allclose(h, 1.0)
    x = np.ones((1, 1000), np.float32)
    y, _ = dsp.filter_block(x, 256.0, FS(lowpass_hz=300.0), 10, 900)
    assert y.shape == (1, 890) and np.allclose(y, 1.0)


def test_describe_and_harmonics():
    s = FS(0.5, 70.0, 60.0)
    lines = dsp.describe(s, 1000.0)
    assert lines[0].startswith("Slow drifts below 0.5 Hz are reduced: at 0.1 Hz,")
    assert any(ln.startswith("60 Hz mains:") for ln in lines)
    assert any(ln.startswith("Brain activity between") and "at least 95%" in ln for ln in lines)
    assert dsp.describe(FS(), 256.0) == ["No filters are on."]
    h = FS(lowpass_hz=200.0, mains_hz=50.0, mains_harmonics=True)
    assert dsp.notch_frequencies(h, 1000.0) == [50.0, 100.0, 150.0, 200.0]


def test_minmax_columns():
    y = np.array([1, 5, np.nan, 2, np.nan, np.nan, -3, 4], np.float32)
    lo, hi = dsp.minmax_columns(y, 4)
    np.testing.assert_array_equal(lo[[0, 1, 3]], [1, 2, -3])
    np.testing.assert_array_equal(hi[[0, 1, 3]], [5, 2, 4])
    assert np.isnan(lo[2]) and np.isnan(hi[2])
    lo, hi = dsp.minmax_columns(np.arange(3.0), 6)
    assert lo.shape == (6,) and lo[0] == 0 and lo[-1] == 2
    lo, hi = dsp.minmax_columns(np.zeros((2, 10)), 5)
    assert lo.shape == (2, 5)


@pytest.mark.skipif(
    os.environ.get("MEF3IO_STUDIO_BENCH") != "1", reason="set MEF3IO_STUDIO_BENCH=1"
)
def test_benchmark_64ch_30s_8khz():
    fs = 8000.0
    s = FS(0.5, 70.0, 60.0)
    n_set, _ = dsp.settling_samples(s, fs)
    n = int(30 * fs) + 2 * n_set
    rng = np.random.default_rng(3)
    x = rng.standard_normal((64, n)).astype(np.float32)
    a, b = n_set, n - n_set

    def work(rows):
        return dsp.filter_block(x[rows], fs, s, a, b)

    dsp.filter_block(x[:1], fs, s, a, b)  # warm the caches
    t0 = time.perf_counter()
    work(slice(0, 64))
    single = time.perf_counter() - t0
    chunks = [slice(i, i + 16) for i in range(0, 64, 16)]
    best = 1e9
    with ThreadPoolExecutor(4) as pool:
        for _ in range(3):
            t0 = time.perf_counter()
            list(pool.map(work, chunks))
            best = min(best, time.perf_counter() - t0)
    ms = dsp.max_out_step(s, fs)

    def work_dec(rows):
        return dsp.filter_block(x[rows], fs, s, a, b, out_step=ms)

    work_dec(slice(0, 1))
    best_dec = 1e9
    with ThreadPoolExecutor(4) as pool:
        for _ in range(3):
            t0 = time.perf_counter()
            list(pool.map(work_dec, chunks))
            best_dec = min(best_dec, time.perf_counter() - t0)
    print(f"\nbench out_step={ms}: 4 threads {best_dec * 1e3:.0f} ms")
    print(
        f"\nbench 64ch x 30s @ 8 kHz (margin {n_set / fs:.1f} s/side): "
        f"single {single * 1e3:.0f} ms, 4 threads {best * 1e3:.0f} ms, "
        f"threads {'scale' if best < 0.7 * single else 'do NOT scale'}"
    )
    # spec target is 300 ms on a 4-core laptop; the hard limit here is looser
    # so a busy CI box does not fail, the printed numbers are the measurement
    print(f"meets the 300 ms target: {best < 0.3}")
    assert best < 1.0
