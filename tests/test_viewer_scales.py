"""What the picture says about size: rows on their own scale, cut-off marks on
an old picture, the fit on open, the held centre (docs/design/viewer.md,
"Many channels"). Synthetic recordings in tmp_path."""

# ruff: noqa: F811
from __future__ import annotations

import mef3io
import numpy as np
from test_viewer_ui import make_window, settle  # noqa: F401

T0 = 946_684_800_000_000
SEC = 1_000_000


def write_signals(path, signals: dict, seconds=60.0, fs=128.0, seed=1):
    """``signals``: channel -> callable(t, rng) -> samples, or None (a small sine)."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * fs)) / fs
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        for k, (name, f) in enumerate(signals.items()):
            if f is None:
                x = 20 * np.sin(2 * np.pi * (6 + k % 5) * t) + 6 * rng.normal(size=t.size)
            else:
                x = f(t, rng)
            w.write(name, np.round(x, 3), T0, fs)
    return path


def eeg(n=6):
    return {f"LA{i}": None for i in range(1, n + 1)}


def settle_rows(win):
    from PySide6.QtWidgets import QApplication

    for _ in range(200):
        QApplication.processEvents()
        if win.controller.wait_idle(0.05) and not win.controller.loading:
            break
    win.canvas.grab_traces()


def index_of(win, channel):
    return [r.channel for r in win.canvas.rows].index(channel)


def trace_extent(win, channel) -> int:
    """How many pixel rows tall the drawn trace of ``channel`` is (the trace's
    own colour only, in the plot area of its row band)."""
    canvas = win.canvas
    i = index_of(win, channel)
    img = canvas.grab_traces().toImage()
    yc, half = canvas.row_y(i), canvas.row_height / 2
    ys = [
        y
        for y in range(int(yc - half), int(yc + half))
        for x in range(canvas.gutter + 4, canvas.width() - 4, 3)
        if img.pixelColor(x, y).lightness() < 80
    ]
    return (max(ys) - min(ys) + 1) if ys else 0


def gutter_text(win, channel) -> list[str]:
    return win.canvas.gutter_lines(index_of(win, channel))


# --- 1: rows that are not EEG have their own sensitivity, and say so ----------


def lead_off_ecg(t, rng):
    return 0.2 * rng.normal(size=t.size)


def real_ecg(t, rng):
    return 1500 * np.sin(2 * np.pi * 1.2 * t) ** 21 + 3 * rng.normal(size=t.size)


def test_a_lead_off_row_looks_as_dead_as_it_is(make_window, tmp_path):
    win = make_window(write_signals(tmp_path / "off.mefd", {**eeg(5), "ECG": lead_off_ecg}))
    win.set_state(sensitivity=10)
    settle_rows(win)
    i = index_of(win, "ECG")
    # never more magnifying than the toolbar, so 0.2 uV of noise is a flat line
    assert trace_extent(win, "ECG") <= 3
    assert win.canvas.row_sensitivity(i) == 10
    assert trace_extent(win, "LA1") > 10  # while the EEG rows are the real thing


def test_a_row_on_its_own_scale_names_it_and_keeps_it_between_pages(make_window, tmp_path):
    def ecg_growing(t, rng):  # twice as large in the second half
        return real_ecg(t, rng) * np.where(t < 30, 1.0, 0.1)

    win = make_window(write_signals(tmp_path / "ecg.mefd", {**eeg(5), "ECG": ecg_growing}))
    win.set_state(sensitivity=100)
    settle_rows(win)
    i = index_of(win, "ECG")
    own = win.canvas.row_sensitivity(i)
    assert own > 100  # a ladder value coarser than the toolbar's
    assert own in vv_steps()
    assert gutter_text(win, "ECG")[0] == "ECG"
    assert gutter_text(win, "ECG")[1] == f"{own:g} µV/mm"  # it says so
    assert len(gutter_text(win, "LA1")) == 1  # an EEG row follows the toolbar and says nothing
    first = trace_extent(win, "ECG")
    win.go_to(T0 + 35 * SEC)  # a page where the signal is ten times smaller
    settle_rows(win)
    assert win.canvas.row_sensitivity(index_of(win, "ECG")) == own  # held, not refitted
    assert trace_extent(win, "ECG") < first / 4  # so the smaller signal looks smaller
    # Auto chooses again, from what is on screen now
    win.auto_sensitivity()
    settle_rows(win)
    assert win.canvas.row_sensitivity(index_of(win, "ECG")) < own


def test_a_row_is_never_more_magnifying_than_the_toolbar(make_window, tmp_path):
    win = make_window(write_signals(tmp_path / "e.mefd", {**eeg(5), "ECG": real_ecg}))
    win.set_state(sensitivity=100)
    settle_rows(win)
    i = index_of(win, "ECG")
    assert win.canvas.row_sensitivity(i) > 100
    win.set_state(sensitivity=5000)
    settle_rows(win)
    assert win.canvas.row_sensitivity(index_of(win, "ECG")) == 5000
    assert len(gutter_text(win, "ECG")) == 1  # the same as the toolbar: nothing to add


def test_the_hover_on_a_row_names_its_scale(make_window, tmp_path):
    win = make_window(write_signals(tmp_path / "h.mefd", {**eeg(5), "ECG": real_ecg}))
    win.set_state(sensitivity=100)
    settle_rows(win)
    i = index_of(win, "ECG")
    scale = win.canvas.row_sensitivity(i)
    win._pointer(T0 + 10 * SEC, i)
    assert f"{scale:g} µV/mm" in win.status_row.text()
    win._pointer(T0 + 10 * SEC, index_of(win, "LA1"))
    assert "µV/mm" not in win.status_row.text()


def vv_steps():
    from mef3io_studio.model import viewer_view as vv

    return vv.SENS_STEPS
