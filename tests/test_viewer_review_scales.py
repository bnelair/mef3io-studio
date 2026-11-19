# ruff: noqa: F811, F401
"""Fixes after the review (item 4): the automatic sensitivity is cheap, gives up after a few
pages it cannot judge, and never changes a scale once it has been fitted."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from test_viewer_scales import SEC, T0, eeg, settle_rows, write_signals
from test_viewer_ui import make_window, settle

from mef3io_studio.viewer import window as window_mod


class _Piece:
    def __init__(self, values):
        self.values = values

    def window(self, local, t0, t1):
        return self.values


@pytest.mark.fits_on_open
def test_the_fit_judges_a_row_from_a_bounded_sample(make_window, tmp_path, monkeypatch):
    win = make_window(write_signals(tmp_path / "a.mefd", eeg(3)))
    seen = []
    real = np.percentile
    monkeypatch.setattr(
        window_mod.np,
        "percentile",
        lambda a, *k, **kw: (seen.append(np.size(a)), real(a, *k, **kw))[1],
    )
    big = 100 * np.sin(np.arange(240_000) * 0.07)
    spread = win._page_spread(SimpleNamespace(piece=_Piece(big), local=0))
    assert spread is not None and 95 < spread < 102
    assert seen and max(seen) <= window_mod.JUDGE_SAMPLE
    # a short page is judged on every value, and too few values still cannot be judged
    assert win._page_spread(SimpleNamespace(piece=_Piece(big[:300]), local=0)) is not None
    assert win._page_spread(SimpleNamespace(piece=_Piece(big[:100]), local=0)) is None
    sparse = np.full(240_000, np.nan)
    sparse[:50] = 1.0
    assert win._page_spread(SimpleNamespace(piece=_Piece(sparse), local=0)) is None


def flat(t, rng):
    return np.full(t.size, 7.0)


@pytest.mark.fits_on_open
def test_the_fit_stops_trying_after_a_few_pages_it_cannot_judge(make_window, tmp_path):
    win = make_window(
        write_signals(tmp_path / "f.mefd", {f"LA{i}": flat for i in range(1, 5)}, seconds=600)
    )
    calls = []
    real = win.auto_sensitivity
    win.auto_sensitivity = lambda announce=True: (calls.append(1), real(announce))[1]
    for k in range(1, 12):
        win.go_to(T0 + 40 * k * SEC)
        settle_rows(win)
    assert len(calls) + 1 <= window_mod.MAX_FIT_TRIES  # the first page counted as one try
    assert not win._auto_open and win.state.sensitivity == 100


def loud_late(t, rng):
    return np.where(t < 50, 20, 900) * np.sin(2 * np.pi * 8 * t) + 3 * rng.normal(size=t.size)


@pytest.mark.fits_on_open
def test_a_fitted_scale_is_kept_when_the_next_pages_look_different(make_window, tmp_path):
    win = make_window(
        write_signals(tmp_path / "l.mefd", {f"LA{i}": loud_late for i in range(1, 5)}, seconds=300)
    )
    fitted = win.state.sensitivity
    assert fitted != 100 and not win._auto_open
    for k in range(1, 6):
        win.go_to(T0 + 60 * k * SEC)
        settle_rows(win)
        assert win.state.sensitivity == fitted


# --- 8e. the own sensitivity of a row that is not EEG is chosen again ----


def _ecg(t, rng):
    return 900 * np.sin(2 * np.pi * 1.2 * t) + 5 * rng.normal(size=t.size)


def test_a_row_that_is_not_eeg_is_judged_again_after_a_filter_change_and_the_overview(
    make_window, tmp_path
):
    from mef3io_studio.model import dsp

    win = make_window(write_signals(tmp_path / "e.mefd", {**eeg(5), "ECG": _ecg}, seconds=600))
    win.set_state(sensitivity=100)
    settle_rows(win)
    key = ("ECG", None)
    assert key in win._own_sens

    win._own_sens[key] = 12345.0  # a size chosen under other settings
    win._filters_chosen(dsp.FilterSettings(1.0, 50.0, 50.0))
    settle_rows(win)
    assert win._own_sens[key] != 12345.0  # judged again

    win._own_sens[key] = 12345.0
    win.set_timebase(0.05)  # into the overview
    settle_rows(win)
    assert win._own_sens.get(key) != 12345.0
    win._own_sens[key] = 12345.0
    win.set_timebase(30)  # and out of it
    settle_rows(win)
    assert win._own_sens.get(key) != 12345.0
