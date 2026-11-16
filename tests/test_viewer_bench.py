"""A speed baseline of the signal viewer, run on request only:

    MEF3IO_STUDIO_BENCH=1 python -m pytest -q -s tests/test_viewer_bench.py

It opens the real ViewerWindow offscreen at 1600x900 on a synthetic recording
and PRINTS how long the everyday moves take, in milliseconds. It asserts
nothing about time (only that every page does become complete), so a slow or
busy machine never fails it; the numbers are the measurement. The same switch
as ``test_viewer_dsp.test_benchmark_64ch_30s_8khz``.

Settings by environment variable, so a bigger recording can be tried by hand:

    MEF3IO_STUDIO_BENCH_CHANNELS   channels (default 64; in leads of 8 contacts)
    MEF3IO_STUDIO_BENCH_RATE       samples per second (default 2000)
    MEF3IO_STUDIO_BENCH_MINUTES    length of the recording (default 10)
    MEF3IO_STUDIO_BENCH_PAGES      pages in each run of pages (default 20)
    MEF3IO_STUDIO_BENCH_FILTERS    "0" = no filters (default: 0.5-70 Hz, 60 Hz mains)

Method, so the numbers can be compared: "complete" means no row of the screen
is still waiting for its data (the placeholder is gone and nothing is loading).
Every run of pages starts from an EMPTY cache, so each begins with nothing
prefetched; the operating system's file cache is warm (the recording was
written just before). One time per number: noisy, for orders of magnitude and
before/after comparisons on one machine, not for tight limits.
"""

# ruff: noqa: F811
from __future__ import annotations

import os
import time

import mef3io
import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from mef3io_studio.common.settings import Settings
from mef3io_studio.model import dsp
from mef3io_studio.model import viewer_view as vv
from mef3io_studio.model.viewer_source import open_recordings
from mef3io_studio.viewer.window import ViewerWindow

pytestmark = pytest.mark.skipif(
    os.environ.get("MEF3IO_STUDIO_BENCH") != "1", reason="set MEF3IO_STUDIO_BENCH=1"
)

T0 = 946_684_800_000_000
TIMEOUT = 120.0


def _env(name: str, default: float) -> float:
    return float(os.environ.get(f"MEF3IO_STUDIO_BENCH_{name}", default))


def channel_names(n: int) -> list[str]:
    """Leads of 8 contacts (A1..A8, B1..B8, ...): bipolar pairs exist."""
    leads = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return [f"{leads[(i // 8) % 26]}{i % 8 + 1}" if i < 208 else f"X{i}" for i in range(n)]


def build(path, channels: int, fs: float, minutes: float):
    rng = np.random.default_rng(1)
    n = int(minutes * 60 * fs)
    t = np.arange(n) / fs
    noise = rng.normal(size=n)
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        for k, name in enumerate(channel_names(channels)):
            x = 40 * np.sin(2 * np.pi * (4 + k % 20) * t) + 8 * np.roll(noise, 997 * k)
            w.write(name, np.round(x, 2), T0, fs)
    return path


def complete(win) -> bool:
    """No row of the screen is waiting for data."""
    if win.controller.loading:
        return False
    first = win.state.row_offset
    rows = win.canvas.rows[first : first + win.canvas.visible_rows() + 1]
    return bool(rows) and all(r.piece is not None for r in rows)


def wait_complete(win, timeout: float = TIMEOUT) -> float:
    """Seconds until the screen is complete (0 when it already is)."""
    t = time.perf_counter()
    end = t + timeout
    while not complete(win):
        QApplication.processEvents()
        time.sleep(0.0005)
        assert time.perf_counter() < end, "the page never became complete"
    return time.perf_counter() - t


def wait_idle(win) -> None:
    end = time.monotonic() + TIMEOUT
    while time.monotonic() < end:
        QApplication.processEvents()
        if win.controller.wait_idle(0.02) and complete(win):
            return
    raise AssertionError("the viewer never became idle")


def ms(seconds: float) -> str:
    return f"{seconds * 1e3:8.0f} ms"


def timed(win, move) -> tuple[float, bool]:
    """(seconds from the move until complete, whether the placeholder was shown)."""
    t = time.perf_counter()
    move()
    placeholder = not complete(win)
    wait_complete(win)
    return time.perf_counter() - t, placeholder


def run_paced_by_completion(win, step, pages: int) -> tuple[float, float, int]:
    total, worst, placeholders = 0.0, 0.0, 0
    for _ in range(pages):
        dt, placeholder = timed(win, step)
        total += dt
        worst = max(worst, dt)
        placeholders += placeholder
    return total, worst, placeholders


def run_paced_by_time(win, step, pages: int, gap_s: float = 0.030) -> tuple[int, float]:
    """Key presses 30 ms apart. Returns (pages complete when shown, seconds from
    the last press until complete)."""
    ready = 0
    last = time.perf_counter()
    for _ in range(pages):
        step()
        ready += complete(win)
        last = time.perf_counter()
        end = last + gap_s
        while time.perf_counter() < end:
            QApplication.processEvents()
            time.sleep(0.0005)
    wait_complete(win)
    return ready, time.perf_counter() - last


def test_the_viewer_speed_baseline(qapp, tmp_path):
    channels = int(_env("CHANNELS", 64))
    fs = _env("RATE", 2000)
    minutes = _env("MINUTES", 10)
    pages = int(_env("PAGES", 20))
    filtered = os.environ.get("MEF3IO_STUDIO_BENCH_FILTERS", "1") != "0"

    t = time.perf_counter()
    path = build(tmp_path / "bench.mefd", channels, fs, minutes)
    built = time.perf_counter() - t
    settings = Settings(tmp_path / "bench.ini")
    if filtered:
        settings.set("viewer/filters", '{"highpass_hz": 0.5, "lowpass_hz": 70.0, "mains_hz": 60.0}')

    t = time.perf_counter()
    data = open_recordings([str(path)])
    for ch in data.channels:  # what the opening task reads once
        data.rate(ch), data.units(ch), data.ownership(ch)
    opened_data = time.perf_counter() - t

    out: dict[str, str] = {}
    t = time.perf_counter()
    win = ViewerWindow(data, settings=settings)
    try:
        win.resize(1600, 900)
        win.show()
        wait_complete(win)
        out["open to the first complete page"] = ms(time.perf_counter() - t)
        wait_idle(win)
        ctl = win.controller
        span = data.timeline_end - data.timeline_start
        length = win.page_us()
        n_pages = int(span // length)
        pages = max(2, min(pages, int(0.9 * n_pages) - 2))  # runs start at 5% and at the end
        start = data.timeline_start + int(0.05 * span)

        # one page forward, nothing prefetched / after waiting for idle
        ctl.cache.clear()
        dt, _ = timed(win, lambda: win.page(1))
        out["one page forward, cold (empty cache)"] = ms(dt)
        wait_idle(win)
        dt, _ = timed(win, lambda: win.page(1))
        out["one page forward, warm (after idle)"] = ms(dt)

        # 20 pages forward, each pressed as soon as the last one is complete
        def from_start() -> None:
            ctl.cache.clear()
            win.go_to(start)
            wait_complete(win)

        from_start()
        total, worst, shown = run_paced_by_completion(win, lambda: win.page(1), pages)
        out[f"{pages} pages forward, one after another: total"] = ms(total)
        out[f"{pages} pages forward, one after another: worst page"] = ms(worst)
        out[f"{pages} pages forward, one after another: showed the placeholder"] = (
            f"{shown:>5} of {pages}"
        )

        # 20 pages, a key press every 30 ms
        from_start()
        ready, tail = run_paced_by_time(win, lambda: win.page(1), pages)
        out[f"{pages} pages forward, 30 ms apart: complete when shown"] = f"{ready:>5} of {pages}"
        out[f"{pages} pages forward, 30 ms apart: last press until complete"] = ms(tail)

        # a key press every 100 ms (the pace the 30 ms run had while a page took ~80 ms to show)
        from_start()
        ready, tail = run_paced_by_time(win, lambda: win.page(1), pages, gap_s=0.100)
        out[f"{pages} pages forward, 100 ms apart: complete when shown"] = f"{ready:>5} of {pages}"
        out[f"{pages} pages forward, 100 ms apart: last press until complete"] = ms(tail)

        # the same backwards, from the end
        def from_end() -> None:
            ctl.cache.clear()
            win.go_to_edge(True)
            wait_complete(win)

        from_end()
        total, worst, shown = run_paced_by_completion(win, lambda: win.page(-1), pages)
        out[f"{pages} pages backward, one after another: total"] = ms(total)
        out[f"{pages} pages backward, one after another: worst page"] = ms(worst)
        out[f"{pages} pages backward, one after another: showed the placeholder"] = (
            f"{shown:>5} of {pages}"
        )
        from_end()
        ready, tail = run_paced_by_time(win, lambda: win.page(-1), pages)
        out[f"{pages} pages backward, 30 ms apart: complete when shown"] = f"{ready:>5} of {pages}"
        out[f"{pages} pages backward, 30 ms apart: last press until complete"] = ms(tail)

        # a jump to a far time (the cache keeps what the runs left)
        far = data.timeline_start + int(0.6 * span)
        win.go_to(data.timeline_start)
        wait_idle(win)
        dt, _ = timed(win, lambda: win.go_to(far))
        out["jump to a far time"] = ms(dt)
        wait_idle(win)

        # a change of filter, of montage, and a screenful of channels
        new = dsp.FilterSettings(1.0, 50.0, 50.0)
        old = win.state.filters
        dt, _ = timed(win, lambda: win.filter_panel.set_settings(new, emit=True))
        out["change of filter on the current page"] = ms(dt)
        win.filter_panel.set_settings(old, emit=True)
        wait_idle(win)
        dt, _ = timed(win, lambda: win.set_state(montage=vv.MONTAGE_BIPOLAR))
        out["change of montage (lead bipolar)"] = ms(dt)
        wait_idle(win)
        screen = win.canvas.visible_rows()
        dt, _ = timed(win, lambda: win.scroll_rows(screen))
        out[f"scroll one screenful of channels ({screen} rows, after idle)"] = ms(dt)

        used, budget = ctl.cache.bytes_used, ctl.cache.budget
        out["cache fill at the end"] = f"{used / 1e6:8.0f} MB of {budget / 1e6:.0f} MB"
    finally:
        win.close()

    width = max(len(k) for k in out)
    print(
        f"\nviewer bench: {channels} channels, {fs:g} Hz, {minutes:g} min, "
        f"{'filters 0.5-70 Hz + 60 Hz mains' if filtered else 'no filters'}, 1600x900, "
        f"cores {os.cpu_count()}, page {length / 1e6:.1f} s ({n_pages} pages)"
    )
    print(f"  (recording written in {built:.1f} s; data opened in {opened_data * 1e3:.0f} ms)")
    for key, value in out.items():
        print(f"  {key:<{width}}  {value}")
    assert len(out) >= 15
