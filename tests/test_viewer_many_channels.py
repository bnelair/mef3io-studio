"""Traces readable on open, many channels, channel organisation
(docs/design/viewer.md, "Many channels"). Synthetic recordings in tmp_path."""

# ruff: noqa: F811
from __future__ import annotations

import mef3io
import numpy as np
import pytest
from test_viewer_ui import make_window, settle  # noqa: F401

from mef3io_studio.common.settings import Settings

T0 = 946_684_800_000_000
SEC = 1_000_000


def write_kinds(path, kinds: dict, seconds=30.0, fs=128.0, seed=1):
    """``kinds``: channel -> "eeg" | "flat" | "huge" | "dc" | "ecg" (what its signal is)."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * fs)) / fs
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        for k, (name, kind) in enumerate(kinds.items()):
            x = 20 * np.sin(2 * np.pi * (6 + k % 5) * t) + 6 * rng.normal(size=t.size)
            if kind == "flat":
                x = np.full(t.size, 7.0)
            elif kind == "huge":
                x = 20000 * rng.normal(size=t.size)
            elif kind == "dc":
                x = x + 6000.0
            elif kind == "ecg":
                x = 1500 * np.sin(2 * np.pi * 1.2 * t) ** 21 + 3 * rng.normal(size=t.size)
            w.write(name, np.round(x, 2), T0, fs)
    return path


def eeg_names(n=10):
    return {f"LA{i}": "eeg" for i in range(1, n + 1)}


@pytest.mark.fits_on_open
def test_the_sensitivity_is_fitted_on_open_and_one_flat_or_huge_channel_does_not_move_it(
    make_window, tmp_path
):
    plain = make_window(write_kinds(tmp_path / "plain.mefd", eeg_names()))
    odd = make_window(
        write_kinds(
            tmp_path / "odd.mefd", {**eeg_names(), "LB1": "flat", "LB2": "huge", "LB3": "dc"}
        )
    )
    assert plain.state.sensitivity != 100  # it was fitted, not left at the default
    assert plain.state.sensitivity < 100  # small signals: a finer scale than the default
    assert odd.state.sensitivity == plain.state.sensitivity  # outliers do not move it


@pytest.mark.fits_on_open
def test_a_saved_sensitivity_is_kept_on_open(make_window, tmp_path):
    settings = Settings(tmp_path / "s.ini")
    settings.set("viewer/sensitivity", 70.0)
    win = make_window(write_kinds(tmp_path / "a.mefd", eeg_names()), settings=settings)
    assert win.state.sensitivity == 70.0


def test_every_trace_is_centred_on_its_row_whatever_its_offset(make_window, tmp_path):
    win = make_window(
        write_kinds(tmp_path / "dc.mefd", {**eeg_names(4), "LB1": "dc"}),
    )
    win.set_state(sensitivity=100)
    settle_rows(win)
    canvas = win.canvas
    names = [r.channel for r in canvas.rows]
    dc = names.index("LB1")
    shape = canvas._row_shape(canvas.rows[dc].piece, canvas.rows[dc].local, canvas.plot_width)
    assert 5800 < shape.centre < 6200  # the level it sits at ...
    assert canvas.clipped_rows[dc] == (False, False)  # ... is not why it would leave its row
    # the picture: the trace's pixels lie around the row's centre line
    img = canvas.grab_traces().toImage()
    yc = int(canvas.row_y(dc))
    dark = [
        y
        for y in range(yc - int(canvas.row_height), yc + int(canvas.row_height))
        for x in range(canvas.gutter + 5, canvas.width() - 5, 7)
        if img.pixelColor(x, y).lightness() < 90
    ]
    assert dark and abs(np.mean(dark) - yc) < canvas.row_height / 3


def settle_rows(win):
    from PySide6.QtWidgets import QApplication

    for _ in range(200):
        QApplication.processEvents()
        if win.controller.wait_idle(0.05) and not win.controller.loading:
            break
    win.canvas.grab_traces()


def test_a_huge_trace_is_cut_off_inside_a_band_and_the_cut_is_marked(make_window, tmp_path):
    win = make_window(write_kinds(tmp_path / "huge.mefd", {**eeg_names(4), "LB1": "huge"}))
    win.set_state(sensitivity=100)
    settle_rows(win)
    canvas = win.canvas
    names = [r.channel for r in canvas.rows]
    huge, quiet = names.index("LB1"), names.index("LA1")
    assert canvas.clipped_rows[huge] == (True, True)
    assert canvas.clipped_rows[quiet] == (False, False)
    img = canvas.grab_traces().toImage()
    sel = canvas.palette_.selection.lstrip("#")
    want = tuple(int(sel[i : i + 2], 16) for i in (0, 2, 4))
    yc, band = canvas.row_y(huge), canvas.row_height * 0.75

    def marks_near(y0):
        hits = 0
        for y in range(int(y0) - 6, int(y0) + 7):
            for x in range(canvas.gutter, canvas.width()):
                c = img.pixelColor(x, y)
                if abs(c.red() - want[0]) + abs(c.green() - want[1]) + abs(c.blue() - want[2]) < 90:
                    hits += 1
        return hits

    assert marks_near(yc - band) > 5 and marks_near(yc + band) > 5  # the sign, both ways


def test_channels_that_are_not_eeg_are_fitted_to_their_own_row(make_window, tmp_path):
    win = make_window(write_kinds(tmp_path / "ecg.mefd", {**eeg_names(4), "ECG": "ecg"}))
    win.set_state(sensitivity=100)
    settle_rows(win)
    rows = {r.channel: r for r in win.canvas.rows}
    assert rows["ECG"].own_scale and not rows["LA1"].own_scale
    # at 100 uV/mm a 1500 uV peak would be 15 mm: much more than the row; fitted, it is not cut
    ecg = [r.channel for r in win.canvas.rows].index("ECG")
    assert win.canvas.clipped_rows[ecg] == (False, False)


# --- many channels ----------


def many(tmp_path, n=120, name="many.mefd"):
    kinds = {}
    leads = "ABCDEFGHIJKLMNOP"
    for i in range(n):
        kinds[f"L{leads[i // 12 % 16]}{i % 12 + 1}"] = "eeg"
    return write_kinds(tmp_path / name, kinds, seconds=20.0, fs=64.0)


def test_with_more_rows_than_fit_the_window_says_which_ones_and_scrolls_to_the_rest(
    make_window, tmp_path, qtbot
):
    win = make_window(many(tmp_path))
    visible = win.canvas.visible_rows()
    assert visible < 120
    assert win.rows_label.text() == f"Rows 1-{visible} of 120"
    assert win.rows_label.isVisible() and win.row_scroll.isVisible()
    assert win.row_scroll.maximum() == 120 - visible and win.row_scroll.pageStep() == visible
    # the bar moves the rows
    win.row_scroll.setValue(30)
    settle(qtbot, win)
    assert win.state.row_offset == 30
    assert win.rows_label.text() == f"Rows 31-{30 + visible} of 120"
    # and the keys: Page Down is the next screenful, Page Up the one before
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.keyClick(win.canvas, Qt.Key.Key_PageDown)
    assert win.state.row_offset == 30 + visible
    QTest.keyClick(win.canvas, Qt.Key.Key_PageUp)
    assert win.state.row_offset == 30
    # to the end: the last rows, and the label says so
    win.scroll_rows(10_000)
    settle(qtbot, win)
    assert win.rows_label.text().endswith("of 120") and f"-{120} of" in win.rows_label.text()
    assert win.row_scroll.value() == win.state.row_offset


def test_few_rows_show_no_row_indicator_and_no_scroll_bar(make_window, tmp_path):
    win = make_window(write_kinds(tmp_path / "few.mefd", eeg_names(4)))
    assert win.rows_label.text() == "" and not win.row_scroll.isVisible()


def test_fit_all_rows_never_goes_under_a_readable_distance_and_is_remembered(
    make_window, tmp_path, qtbot
):
    from mef3io_studio.model import viewer_view as vv

    settings = Settings(tmp_path / "s.ini")
    win = make_window(write_kinds(tmp_path / "ten.mefd", eeg_names(10)), settings=settings)
    # the plot is tall enough for ten rows at a far larger distance: fitted, rows fill the plot
    win.spacing_box.setCurrentIndex(win.spacing_box.findData(vv.FIT_ALL))
    win.spacing_box.activated.emit(win.spacing_box.currentIndex())
    settle(qtbot, win)
    assert win.state.fit_rows and win.state.spacing > 7
    assert win.canvas.visible_rows() >= 10 and not win.row_scroll.isVisible()
    # 120 rows do not fit at a readable distance: the floor holds and the rest scrolls
    big = make_window(many(tmp_path), settings=settings)
    assert big.state.fit_rows is False  # nothing saved yet: the default
    big.spacing_box.setCurrentIndex(big.spacing_box.findData(vv.FIT_ALL))
    big.spacing_box.activated.emit(big.spacing_box.currentIndex())
    settle(qtbot, big)
    assert big.state.spacing == vv.READABLE_SPACING_MM
    assert big.canvas.visible_rows() < 120 and big.row_scroll.isVisible()
    # the choice is remembered
    big.save_preferences()
    assert settings.get("viewer/spacing") == vv.FIT_ALL
    again = make_window(many(tmp_path, name="again.mefd"), settings=settings)
    assert again.state.fit_rows and again.spacing_box.currentData() == vv.FIT_ALL
    # a typed distance leaves it
    again.spacing_box.setCurrentIndex(again.spacing_box.findData(10))
    again.spacing_box.activated.emit(again.spacing_box.currentIndex())
    assert not again.state.fit_rows and again.state.spacing == 10


def test_the_blocks_asked_for_follow_the_rows_on_screen(make_window, tmp_path, qtbot):
    win = make_window(many(tmp_path, n=200))
    ctl = win.controller
    win.scroll_rows(150)
    settle(qtbot, win)
    req = ctl._req
    first, last = req.visible
    assert first == 150 and last > first
    visible_plans = {
        gi for gi, plan in enumerate(req.plans) if any(first <= ri < last for ri in plan.rows)
    }
    assert visible_plans and set(req.keys) == visible_plans  # only groups holding rows on screen
    assert len(visible_plans) < len(req.plans)


# --- organisation: leads, channels that are not EEG ----------


def test_channels_that_are_not_eeg_come_last_in_every_montage(make_window, tmp_path):
    from mef3io_studio.model import viewer_view as vv

    kinds = {"ECG": "ecg", "LA1": "eeg", "LA2": "eeg", "LA3": "eeg", "EMG1": "eeg", "LB1": "eeg"}
    kinds["SpO2"] = "eeg"
    win = make_window(write_kinds(tmp_path / "order.mefd", kinds))
    assert win.row_labels() == ["LA1", "LA2", "LA3", "LB1", "ECG", "EMG1", "SpO2"]
    win.set_state(montage=vv.MONTAGE_REFERENCE, reference="LB1")
    assert win.row_labels() == [
        "LA1 − LB1",
        "LA2 − LB1",
        "LA3 − LB1",
        "ECG − LB1",
        "EMG1 − LB1",
        "SpO2 − LB1",
    ]
    win.set_state(montage=vv.MONTAGE_BIPOLAR)
    assert win.row_labels() == ["LA1 − LA2", "LA2 − LA3", "LB1", "ECG", "EMG1", "SpO2"]


def test_rows_of_one_lead_are_a_group_with_the_lead_named_once():
    from mef3io_studio.model import montage as m

    names = [f"LA{i}" for i in range(1, 6)] + ["Fp1", "Fp2"] + [f"LB{i}" for i in (1, 2, 3, 4)]
    names += ["ECG", "EMG1"]
    groups = m.group_rows(m.as_recorded(names))
    assert [g.first for g in groups] == [True, *[False] * 4, False, False, True, False, False] + [
        False,
        True,
        False,
    ]
    assert groups[1].short == "2" and groups[0].short == ""  # the lead is named by the first row
    assert (
        groups[5].group == "" and groups[6].group == ""
    )  # two rows are no group: kept as they are
    assert groups[9].short == "3" and groups[10].short == "4"
    assert groups[11].group == groups[12].group == m.OTHER_GROUP and groups[12].short == ""


def test_the_canvas_draws_a_line_between_leads_and_the_lead_name_once(make_window, tmp_path):
    kinds = {f"LA{i}": "eeg" for i in range(1, 5)} | {f"LB{i}": "eeg" for i in range(1, 5)}
    win = make_window(write_kinds(tmp_path / "leads.mefd", kinds))
    rows = win.canvas.rows
    assert [r.group_first for r in rows] == [True, False, False, False] * 2
    assert [r.short for r in rows] == ["", "2", "3", "4", "", "2", "3", "4"]
    assert [r.label for r in rows][:2] == ["LA1", "LA2"]  # the full names stay what rows are called
    win.canvas.grab_traces()
    img = win.canvas.grab_traces().toImage()
    y = round(win.canvas.row_y(4) - win.canvas.row_height / 2)
    # the line runs across the whole plot, under the traces' own pixels
    muted = win.canvas.palette_.muted.lstrip("#")
    want = tuple(int(muted[i : i + 2], 16) for i in (0, 2, 4))
    hits = sum(
        1
        for x in range(win.canvas.gutter + 2, win.canvas.width() - 2)
        for dy in (0, 1)
        if tuple(img.pixelColor(x, y + dy).getRgb()[:3]) == want
    )
    assert hits > win.canvas.plot_width * 0.5


def test_the_channel_chooser_ticks_a_whole_lead_and_searches_by_name(make_window, tmp_path, qtbot):
    from PySide6.QtCore import Qt

    kinds = {f"LA{i}": "eeg" for i in range(1, 5)} | {f"LB{i}": "eeg" for i in range(1, 5)}
    kinds |= {f"LC{i}": "eeg" for i in range(1, 4)}  # three contacts: not a lead
    kinds |= {"Cz": "eeg", "ECG": "ecg"}
    win = make_window(write_kinds(tmp_path / "chooser.mefd", kinds))
    chooser = win.channel_chooser
    leads = [
        chooser.leads.item(i).data(Qt.ItemDataRole.UserRole) for i in range(chooser.leads.count())
    ]
    assert leads == ["LA", "LB"]  # leads by the rule: four contacts, the first among them
    chooser.set_lead("LA", False)
    assert win.state.hidden == {"LA1", "LA2", "LA3", "LA4"}
    assert not any(n.startswith("LA") for n in win.row_labels())
    assert chooser.leads.item(0).checkState() == Qt.CheckState.Unchecked
    # one channel back: the lead is half ticked
    for i in range(chooser.list.count()):
        if chooser.list.item(i).text() == "LA2":
            chooser.list.item(i).setCheckState(Qt.CheckState.Checked)
    assert chooser.leads.item(0).checkState() == Qt.CheckState.PartiallyChecked
    assert win.state.hidden == {"LA1", "LA3", "LA4"}
    # a click on the lead's own box (as a person does) shows all its channels
    chooser.leads.item(0).setCheckState(Qt.CheckState.Checked)
    assert win.state.hidden == frozenset()
    # search finds a lead by a channel in it, and channels by name
    chooser.search.setText("lb2")
    assert [not chooser.leads.item(i).isHidden() for i in range(2)] == [False, True]
    assert [
        chooser.list.item(i).text()
        for i in range(chooser.list.count())
        if not chooser.list.item(i).isHidden()
    ] == ["LB2"]


@pytest.mark.fits_on_open
def test_the_fit_on_open_also_happens_when_many_channels_load_in_groups(make_window, tmp_path):
    """The first page of 120 channels arrives group by group; the fit waits for
    all rows on screen, and the controller still reports loading for a moment
    after the last block arrived. It must not wait for that."""
    win = make_window(many(tmp_path, n=120, name="fit_many.mefd"))
    assert win.state.sensitivity != 100 and win._auto_open is False


# --- "Fit all rows" really fits, whatever the height, the count and the screen ----------


def test_fit_all_rows_fits_at_every_height_row_count_and_screen_scale(qapp, monkeypatch):
    """The distance 'Fit all rows' works out and the number of rows the canvas
    says fit must agree to the last pixel: a float rounding once gave n - 1 rows
    and a scroll bar at about 45% of window heights."""
    from mef3io_studio.model import viewer_view as vv
    from mef3io_studio.viewer.canvas import TraceCanvas

    canvas = TraceCanvas(vv.PROFILES["white"])
    bad = []
    for px_per_mm in (
        3.7795275590551185,
        3.5433070866141736,
        4.0,
        5.118110236220472,
        7.559055118110236,
    ):
        monkeypatch.setattr(TraceCanvas, "px_per_mm", property(lambda _s, v=px_per_mm: v))
        for height in range(240, 2200, 3):
            canvas.resize(900, height)
            plot_h = canvas.plot_rect().height()
            for n in (2, 3, 5, 8, 13, 16, 21, 32, 43, 50, 64, 100):
                spacing = vv.fit_spacing(plot_h, px_per_mm, n)
                canvas.spacing_mm = spacing
                if vv.READABLE_SPACING_MM < spacing < vv.SPACING_STEPS[-1] and (
                    canvas.visible_rows() < n
                ):
                    bad.append((px_per_mm, height, n))
    assert not bad, bad[:5]


def test_fit_all_rows_shows_every_row_and_no_scroll_bar_in_the_window(make_window, tmp_path, qtbot):
    from mef3io_studio.model import viewer_view as vv

    win = make_window(write_kinds(tmp_path / "thirteen.mefd", eeg_names(13)))
    win.spacing_box.setCurrentIndex(win.spacing_box.findData(vv.FIT_ALL))
    win.spacing_box.activated.emit(win.spacing_box.currentIndex())
    for height in range(520, 1000, 19):
        win.resize(1200, height)
        settle(qtbot, win)
        win._refresh()
        canvas = win.canvas
        plot_h = canvas.plot_rect().height()
        shown = sum(
            1 for i in range(13) if canvas.row_y(i) + canvas.row_height / 2 <= plot_h + 1e-6
        )
        assert shown == 13, (height, shown)
        assert win.rows_label.text() == "" and not win.row_scroll.isVisible(), height


def test_the_rows_label_names_the_rows_that_are_fully_on_screen(make_window, tmp_path, qtbot):
    win = make_window(many(tmp_path, n=40, name="forty.mefd"))
    win.set_state(spacing=9.3)
    for height in range(520, 900, 37):
        win.resize(1200, height)
        settle(qtbot, win)
        win._refresh()
        canvas = win.canvas
        plot_h = canvas.plot_rect().height()
        first = win.state.row_offset
        whole = [
            i for i in range(first, 40) if canvas.row_y(i) + canvas.row_height / 2 <= plot_h + 1e-6
        ]
        assert win.rows_label.text() == f"Rows {first + 1}-{whole[-1] + 1} of 40", height


# --- leads and the guess about channels that are not EEG, in the window ----------


def lead_names():
    names = ["Fp1", "Fp2", "F3", "F4"] + [f"LA{i}" for i in range(1, 7)]
    return names + [f"HR{i}" for i in range(1, 5)] + ["ECG"]


def test_a_depth_lead_called_hr_stays_with_the_eeg_and_scalp_names_are_not_grouped(
    make_window, tmp_path
):
    win = make_window(write_kinds(tmp_path / "leads.mefd", {n: "eeg" for n in lead_names()}))
    labels = win.row_labels()
    assert labels[-1] == "ECG" and sorted(labels) == sorted(
        lead_names()
    )  # HR is not sent to the end
    rows = {r.channel: r for r in win.canvas.rows}
    assert not rows["HR1"].own_scale and rows["ECG"].own_scale
    assert not any(rows[n].group for n in ("Fp1", "Fp2", "F3", "F4"))
    assert rows["LA2"].short == "2" and rows["HR3"].short == "3" and rows["F4"].short == ""
    assert win._leads == {"LA", "HR"}


def test_the_first_row_on_screen_is_named_in_full_when_it_is_mid_lead(make_window, tmp_path):
    win = make_window(write_kinds(tmp_path / "mid.mefd", {f"LA{i}": "eeg" for i in range(1, 14)}))
    win.set_state(spacing=20)
    win.set_state(row_offset=6)
    canvas = win.canvas
    assert canvas.gutter_lines(6)[0] == "LA7"  # the first on screen: its lead is scrolled away
    assert canvas.gutter_lines(7)[0] == "8"
    win.set_state(row_offset=0)
    assert canvas.gutter_lines(0)[0] == "LA1" and canvas.gutter_lines(1)[0] == "2"


def test_the_derived_montages_name_their_rows_in_full(make_window, tmp_path):
    from mef3io_studio.model import viewer_view as vv

    names = [f"LA{i}" for i in range(1, 7)] + ["Cz"]
    win = make_window(write_kinds(tmp_path / "der.mefd", {n: "eeg" for n in names}))
    win.set_state(montage=vv.MONTAGE_BIPOLAR)
    shown = [win.canvas.gutter_lines(i)[0] for i in range(len(win.canvas.rows))]
    assert [x for x in shown if "−" in x] == [
        "LA1 − LA2",
        "LA2 − LA3",
        "LA3 − LA4",
        "LA4 − LA5",
        "LA5 − LA6",
    ]
    win.set_state(montage=vv.MONTAGE_REFERENCE, reference="Cz")
    shown = [win.canvas.gutter_lines(i)[0] for i in range(len(win.canvas.rows))]
    assert shown[:3] == ["LA1 − Cz", "LA2 − Cz", "LA3 − Cz"]  # full, never "2 − Cz"


def test_the_chooser_offers_only_leads_by_the_rule(make_window, tmp_path):
    win = make_window(write_kinds(tmp_path / "ch.mefd", {n: "eeg" for n in lead_names()}))
    chooser = win.channel_chooser
    offered = [chooser.leads.item(i).data(0x100) for i in range(chooser.leads.count())]
    assert sorted(offered) == ["HR", "LA"]
    win.hide_lead("F3")  # not in a lead: only itself
    assert win.state.hidden == {"F3"}
