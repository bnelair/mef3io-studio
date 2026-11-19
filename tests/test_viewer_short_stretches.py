"""Stretches of signal too short to filter are drawn paler and unfiltered; the
viewer says why, in the filter window and in the status bar
(docs/design/viewer.md, "Gaps")."""

# ruff: noqa: F811
from __future__ import annotations

import mef3io
import numpy as np
from test_viewer_ui import SEC, T0, make_window, settle, write  # noqa: F401

from mef3io_studio.model import dsp
from mef3io_studio.model import viewer_view as vv
from mef3io_studio.workers.viewer_pipeline import ViewPiece

FS = 256.0
FILTERS = dsp.FilterSettings(0.5, 70.0)
#: Data, gap, a short stretch, gap, data (seconds from the start).
PARTS = ((0, 20), (22, 24), (26, 60))


def write_with_two_gaps(path, parts=PARTS, channels=("a", "b")):
    rng = np.random.default_rng(5)
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        for name in channels:
            for a, b in parts:
                t = np.arange(int((b - a) * FS)) / FS
                x = 40 * np.sin(2 * np.pi * 9 * t) + 5 * rng.normal(size=t.size)
                w.write(name, np.round(x, 2), T0 + int(a * SEC), FS)
    return path


def settling_seconds():
    n, _ = dsp.settling_samples(FILTERS, FS)
    return n / FS


def test_the_two_gaps_make_a_stretch_shorter_than_the_filters_settle():
    assert 2.0 < settling_seconds() < 20.0  # the 2 s stretch is too short, the others are not


def _go_to_the_short_stretch(win, qtbot):
    win.set_timebase(15)  # a page of about 20 s
    win.go_to(T0 + 12 * SEC)  # holds the short stretch (22-24 s)
    settle(qtbot, win)


def test_the_page_with_the_short_stretch_explains_the_paler_part(make_window, tmp_path, qtbot):
    win = make_window(write_with_two_gaps(tmp_path / "g.mefd"))
    _go_to_the_short_stretch(win, qtbot)
    # nothing to explain while the filters are off
    assert win.state.filters.is_off and win.status_short.text() == ""
    win.filter_panel.set_settings(FILTERS, emit=True)
    settle(qtbot, win)
    pieces = win.controller.current_pieces()
    assert any(
        pc[0].has_unfiltered(pc[1], win.state.t0, win.state.t0 + win.page_us()) for pc in pieces
    )
    expected = vv.short_stretch_text(round(settling_seconds() * 1e6))
    assert win.status_short.text() == expected
    assert win.status_short.toolTip() == expected
    assert win.filter_panel.short_note.text() == expected
    assert win.filter_panel.short_note.isVisibleTo(win.filter_panel)
    # in words a person can read: what, why, and what is shown instead
    assert "paler" in expected.lower()
    assert "too short to filter" in expected
    assert f"shorter than {vv.format_length(round(settling_seconds() * 1e6))}" in expected
    assert expected.endswith("shown as recorded.")


def test_the_sentence_is_gone_where_the_page_has_no_short_stretch(make_window, tmp_path, qtbot):
    win = make_window(write_with_two_gaps(tmp_path / "g2.mefd"))
    win.filter_panel.set_settings(FILTERS, emit=True)
    _go_to_the_short_stretch(win, qtbot)
    assert win.status_short.text()
    win.go_to(T0 + 35 * SEC)  # long data only
    settle(qtbot, win)
    assert win.status_short.text() == ""
    assert win.filter_panel.short_note.text() == ""
    assert not win.filter_panel.short_note.isVisibleTo(win.filter_panel)
    win.go_to(T0 + 12 * SEC)
    settle(qtbot, win)
    assert win.status_short.text()


def test_turning_the_filters_off_takes_the_sentence_away(make_window, tmp_path, qtbot):
    win = make_window(write_with_two_gaps(tmp_path / "g3.mefd"))
    win.filter_panel.set_settings(FILTERS, emit=True)
    _go_to_the_short_stretch(win, qtbot)
    assert win.status_short.text()
    win.filter_panel.set_settings(dsp.FilterSettings(), emit=True)
    settle(qtbot, win)
    assert win.status_short.text() == "" and win.filter_panel.short_note.text() == ""


def test_the_figure_follows_the_filter_settings(make_window, tmp_path, qtbot):
    win = make_window(write_with_two_gaps(tmp_path / "g4.mefd"))
    win.filter_panel.set_settings(FILTERS, emit=True)
    _go_to_the_short_stretch(win, qtbot)
    first = win.status_short.text()
    # a gentler high-pass settles sooner: still shorter than the 2 s stretch? then no sentence
    easy = dsp.FilterSettings(5.0, 70.0)
    assert dsp.settling_samples(easy, FS)[0] / FS < 2.0
    win.filter_panel.set_settings(easy, emit=True)
    settle(qtbot, win)
    assert win.status_short.text() == ""
    win.filter_panel.set_settings(FILTERS, emit=True)
    settle(qtbot, win)
    assert win.status_short.text() == first


def test_a_recording_without_short_stretches_never_shows_it(make_window, tmp_path, qtbot):
    win = make_window(write(tmp_path / "plain.mefd", seconds=60))
    win.filter_panel.set_settings(FILTERS, emit=True)
    settle(qtbot, win)
    assert win.status_short.text() == ""
    assert not win.filter_panel.short_note.isVisibleTo(win.filter_panel)


def test_the_overview_says_nothing_about_filters(make_window, tmp_path, qtbot):
    win = make_window(write_with_two_gaps(tmp_path / "g5.mefd"))
    win.filter_panel.set_settings(FILTERS, emit=True)
    _go_to_the_short_stretch(win, qtbot)
    assert win.status_short.text()
    win.set_timebase(0.2)
    assert win.overview()
    settle(qtbot, win)
    assert win.status_short.text() == ""


def test_has_unfiltered_looks_only_at_the_page():
    values = np.zeros((1, 10), dtype=np.float32)
    mask = np.zeros((1, 10), dtype=bool)
    mask[0, 7:9] = True
    piece = ViewPiece(values, mask, t_first_us=0.0, dt_us=1_000_000.0)
    assert piece.has_unfiltered(0, 6.5e6, 9.5e6)
    assert not piece.has_unfiltered(0, 0, 6.5e6)
    assert not piece.has_unfiltered(0, 9.2e6, 9.9e6)
    assert piece.has_unfiltered(0, 9.5e6, 6.5e6)  # reversed ends
    paired = ViewPiece(
        np.zeros((1, 20), dtype=np.float32),
        np.repeat(mask, 2, axis=1),
        t_first_us=0.0,
        dt_us=1_000_000.0,
        paired=True,
    )
    assert paired.has_unfiltered(0, 7e6, 8e6) and not paired.has_unfiltered(0, 0, 5e6)
