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


# --- 2: an old picture that is cut off says so too ----------


def arrow_pixels(canvas) -> int:
    img = canvas.grab_traces().toImage()
    sel = canvas.palette_.selection.lstrip("#")
    want = tuple(int(sel[k : k + 2], 16) for k in (0, 2, 4))
    return sum(
        1
        for y in range(0, img.height(), 2)
        for x in range(canvas.gutter, img.width(), 2)
        if sum(abs(a - b) for a, b in zip(img.pixelColor(x, y).getRgb()[:3], want, strict=False))
        < 120
    )


def test_the_old_picture_is_marked_where_it_is_cut_off(make_window, tmp_path):
    win = make_window(write_signals(tmp_path / "old.mefd", eeg(4), seconds=120))
    win.set_state(sensitivity=2)  # 20 uV waves at 2 uV/mm: cut off
    settle_rows(win)
    canvas = win.canvas
    complete = arrow_pixels(canvas)
    assert complete > 20
    win.controller.cache.clear()
    win.move_by(2_000_000, 1)  # a new page whose data has not arrived: the old picture stands in
    old_rows = [r for r in canvas.rows[: canvas.visible_rows()] if r.piece is None]
    assert old_rows and all(r.stale is not None for r in old_rows)
    assert any(any(v) for v in canvas.clipped_rows.values())
    assert arrow_pixels(canvas) > 25  # paler is fine, absent is not


# --- 3: the fit on open ----------

import pytest  # noqa: E402

from mef3io_studio.common.settings import Settings  # noqa: E402
from mef3io_studio.model.montage import Row  # noqa: E402,F401
from mef3io_studio.viewer.window import ViewerWindow  # noqa: E402


def open_unsettled(qtbot, path, settings=None, width=1200, choose=False):
    """A window whose first page has not completed yet (as a person can act
    in that moment)."""
    from mef3io_studio.model.viewer_source import open_recordings

    data = open_recordings([str(path)])
    for ch in data.channels:
        data.rate(ch), data.units(ch), data.ownership(ch)
    win = ViewerWindow(data, settings=settings)
    qtbot.addWidget(win)
    win.resize(width, 700)
    win.step_sensitivity(-1) if choose else None
    win.show()
    qtbot.waitUntil(lambda: win._ready, timeout=10_000)
    return win


@pytest.mark.fits_on_open
def test_a_sensitivity_the_person_chose_is_never_fitted_over(qtbot, tmp_path):
    # chosen before the first page is complete
    win = open_unsettled(qtbot, write_signals(tmp_path / "a.mefd", eeg(5), seconds=60), choose=True)
    chosen = win.state.sensitivity
    settle(qtbot, win)
    assert win.state.sensitivity == chosen
    assert win._auto_open is False
    win.close()


@pytest.mark.fits_on_open
def test_a_fitted_sensitivity_is_not_saved_as_a_choice(make_window, tmp_path):
    settings = Settings(tmp_path / "s.ini")
    win = make_window(write_signals(tmp_path / "a.mefd", eeg(5)), settings=settings)
    assert win.state.sensitivity != 100  # it was fitted
    win.auto_sensitivity()  # and the button too
    win.close()
    assert "viewer/sensitivity" not in settings.stored_keys()


def flat_then_signal(t, rng):
    return np.where(t < 30, 7.0, 80 * np.sin(2 * np.pi * 8 * t) + 8 * rng.normal(size=t.size))


@pytest.mark.fits_on_open
def test_a_page_that_cannot_be_judged_is_tried_again_on_the_next(make_window, tmp_path):
    flat = {f"LA{i}": flat_then_signal for i in range(1, 5)}
    win = make_window(write_signals(tmp_path / "f.mefd", flat, seconds=120))
    assert win.state.sensitivity == 100 and win._auto_open  # flat: left at the default, still armed
    win.go_to(T0 + 60 * SEC)
    settle_rows(win)
    assert win.state.sensitivity != 100 and not win._auto_open
    assert win.status_message.text() == (
        f"Sensitivity set to {win.state.sensitivity:g} µV/mm to fit this recording."
    )


@pytest.mark.fits_on_open
def test_after_the_person_chooses_a_flat_page_is_not_tried_again(make_window, tmp_path):
    flat = {f"LA{i}": flat_then_signal for i in range(1, 5)}
    win = make_window(write_signals(tmp_path / "f2.mefd", flat, seconds=120))
    win.step_sensitivity(1)
    chosen = win.state.sensitivity
    win.go_to(T0 + 60 * SEC)
    settle_rows(win)
    assert win.state.sensitivity == chosen


def slow(tmp_path, name="slow.mefd", fs=64.0, seconds=7200):
    """Two hours at 64 Hz: 30 uV for the first minute, 300 uV after it."""

    def sig(t, rng):
        return np.where(t < 60, 30.0, 300.0) * np.sin(2 * np.pi * 1.0 * t) + rng.normal(size=t.size)

    return write_signals(tmp_path / name, {f"LA{i}": sig for i in range(1, 5)}, seconds, fs)


@pytest.mark.fits_on_open
def test_the_fit_follows_a_switch_into_the_overview_and_back(make_window, tmp_path):
    win = make_window(slow(tmp_path))
    first = win.state.sensitivity
    assert first < 100  # the first page holds 30 uV waves
    win.set_state(timebase=0.5)  # a page of ten minutes: the overview
    settle_rows(win)
    assert win.overview()
    in_overview = win.state.sensitivity
    assert in_overview > first  # the blocks' highest values are 300 uV
    assert not any(any(v) for v in win.canvas.clipped_rows.values())  # nothing cut off
    assert "sensitivity" not in win._chosen
    win.set_state(timebase=30)
    settle_rows(win)
    assert not win.overview() and win.state.sensitivity == first


@pytest.mark.fits_on_open
def test_a_chosen_sensitivity_is_not_fitted_again_in_the_overview(make_window, tmp_path):
    win = make_window(slow(tmp_path, "slow2.mefd"))
    win.step_sensitivity(1)
    chosen = win.state.sensitivity
    win.set_state(timebase=0.5)
    settle_rows(win)
    assert win.state.sensitivity == chosen


def test_auto_works_in_the_overview_and_says_when_it_cannot_decide(make_window, tmp_path):
    win = make_window(slow(tmp_path, "slow3.mefd"))
    win.set_state(timebase=0.5, sensitivity=5)
    settle_rows(win)
    assert win.overview() and win.canvas.rows[0].piece is not None
    assert win.auto_sensitivity() is True
    assert win.state.sensitivity > 5
    flat = {f"LA{i}": (lambda t, r: np.full(t.size, 3.0)) for i in range(1, 4)}
    win2 = make_window(write_signals(tmp_path / "flat.mefd", flat, seconds=7200, fs=64))
    win2.set_state(timebase=0.5)
    settle_rows(win2)
    assert win2.auto_sensitivity() is False
    assert "Could not choose a sensitivity" in win2.status_message.text()


@pytest.mark.fits_on_open
def test_half_a_hertz_over_days_does_not_end_with_every_row_cut_off(make_window, tmp_path):
    fs = 0.5
    n = int(3 * 86400 * fs)
    rng = np.random.default_rng(4)
    tt = np.arange(n) / fs
    path = tmp_path / "days.mefd"
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        for i in range(1, 5):
            x = 1200 * np.sin(2 * np.pi * tt / 3600) + 5 * rng.normal(size=n)
            w.write(f"LA{i}", np.round(x, 2), T0, fs)
    win = make_window(path)
    win.set_state(timebase=0.005)  # the whole stretch in one screen
    settle_rows(win)
    assert win.overview()
    shown = [i for i in range(len(win.canvas.rows)) if win.canvas.rows[i].piece is not None]
    assert shown and not any(any(win.canvas.clipped_rows.get(i, (0, 0))) for i in shown)


# --- 4: the centre of a row is held, and what was taken off is said ----------


def mean_y(win, channel) -> float:
    """The vertical position (pixels) of the middle of the drawn trace of ``channel``."""
    canvas = win.canvas
    i = index_of(win, channel)
    img = canvas.grab_traces().toImage()
    yc, half = canvas.row_y(i), canvas.row_height / 2
    ys = [
        y
        for y in range(int(yc - half), int(yc + half))
        for x in range(canvas.gutter + 4, canvas.width() - 4, 2)
        if img.pixelColor(x, y).lightness() < 80
    ]
    return float(np.mean(ys))


def with_step(size):
    def sig(t, rng):
        return 20 * np.sin(2 * np.pi * 10 * t) + size * (t > 100) + 2 * rng.normal(size=t.size)

    return sig


def walk(win, t_from, t_to, step=5):
    for s in range(t_from, t_to, step):
        win.go_to(T0 + s * SEC)
        settle_rows(win)


def test_a_small_step_in_the_level_moves_the_trace_instead_of_re_centring_it(make_window, tmp_path):
    win = make_window(write_signals(tmp_path / "step.mefd", {"LA1": with_step(100.0)}, 200, 256))
    win.set_state(sensitivity=70)
    win.go_to(T0 + 60 * SEC)
    settle_rows(win)
    before = mean_y(win, "LA1")
    walk(win, 65, 150)  # page by page, over the step and on
    after = mean_y(win, "LA1")
    shift = 100 / 70 * win.canvas.px_per_mm  # 100 uV is 1.4 mm, well inside half a row
    assert before - after == pytest.approx(shift, abs=2.0)  # higher on screen: it did rise


def test_a_big_step_re_centres_so_the_trace_stays_in_its_row(make_window, tmp_path):
    win = make_window(write_signals(tmp_path / "big.mefd", {"LA1": with_step(900.0)}, 200, 256))
    win.set_state(sensitivity=70)
    win.go_to(T0 + 60 * SEC)
    settle_rows(win)
    walk(win, 65, 150)
    assert win.canvas.clipped_rows[0] == (False, False)
    row = win.canvas.row_y(0)
    assert abs(mean_y(win, "LA1") - row) < win.canvas.row_height / 4


def test_a_far_jump_starts_the_centre_again(make_window, tmp_path):
    win = make_window(write_signals(tmp_path / "far.mefd", {"LA1": with_step(100.0)}, 400, 256))
    win.set_state(sensitivity=70)
    win.go_to(T0 + 60 * SEC)
    settle_rows(win)
    held = win.canvas.row_level(0)
    win.go_to(T0 + 300 * SEC)  # nothing in common with the last page
    settle_rows(win)
    assert win.canvas.row_level(0) - held == pytest.approx(100, abs=10)


def test_the_old_picture_and_the_new_one_share_the_centre(make_window, tmp_path):
    win = make_window(write_signals(tmp_path / "old.mefd", {"LA1": with_step(100.0)}, 200, 256))
    win.set_state(sensitivity=70)
    win.go_to(T0 + 95 * SEC)
    settle_rows(win)
    held = win.canvas.row_level(0)
    win.controller.cache.clear()
    win.move_by(3 * SEC, 1)  # the page now holds the step, its data has not arrived
    win.canvas.grab_traces()
    assert win.canvas.rows[0].piece is None and win.canvas.rows[0].stale is not None
    assert win.canvas.row_level(0) == held


def test_a_level_that_was_taken_off_is_said_when_the_pointer_is_on_the_row(make_window, tmp_path):
    dc = {"LA1": None, "LB1": lambda t, r: 4000 + 5 * r.normal(size=t.size)}
    win = make_window(write_signals(tmp_path / "dc.mefd", dc))
    win.set_state(sensitivity=10)
    settle_rows(win)
    win._pointer(T0 + 10 * SEC, index_of(win, "LB1"))
    assert "shown 4.0 mV lower" in win.status_row.text()
    win._pointer(T0 + 10 * SEC, index_of(win, "LA1"))
    assert "shown" not in win.status_row.text()


def test_a_measurement_reads_true_values_whatever_was_taken_off(make_window, tmp_path):
    from PySide6.QtCore import QPointF

    dc = {"LB1": lambda t, r: 4000 + 5 * np.sin(2 * np.pi * 3 * t)}
    win = make_window(write_signals(tmp_path / "dc2.mefd", dc))
    win.set_state(sensitivity=10)
    settle_rows(win)
    c = win.canvas
    y = c.row_y(0)
    c._measure_from = QPointF(c.gutter + 20, y)
    c._pointer = QPointF(c.gutter + 600, y)
    text = c.measurement()
    assert "399" in text or "400" in text  # 3995.x to 4005.x
    assert "µV" in text
