"""Colours of your own for the signal viewer (docs/design/viewer.md, "Colour
profiles"): the derivation is pure (model/viewer_colours.py), and the window
applies it by repainting only. Synthetic recordings in tmp_path only."""

# ruff: noqa: F811
from __future__ import annotations

import pytest
from PySide6.QtGui import QColor
from test_viewer_ui import make_window, one, settle, write  # noqa: F401

from mef3io_studio.common.settings import PREFERENCES
from mef3io_studio.model import viewer_colours as vc
from mef3io_studio.model import viewer_view as vv
from mef3io_studio.viewer.window import ViewerWindow, default_state

WHITE, BLACK, GREY = "#ffffff", "#000000", "#808080"

#: Light, dark, mid-grey and saturated backgrounds, each with a trace that
#: can be told apart from it.
BACKGROUNDS = {
    "white": ("#ffffff", "#14181d"),
    "off-white": ("#f0faf0", "#0c2a0c"),
    "black": ("#000000", "#ffffff"),
    "dark": ("#13161b", "#dde3ea"),
    "mid-grey": ("#808080", "#000000"),
    "mid-grey-light-trace": ("#777777", "#ffffff"),
    "red": ("#ff0000", "#000000"),
    "blue": ("#0000ff", "#ffffff"),
    "green": ("#00ff00", "#000000"),
    "yellow": ("#ffff00", "#000000"),
    "saturated-teal": ("#008b8b", "#ffffff"),
    "purple": ("#6a0dad", "#ffffff"),
}


def c(text):
    return vc.parse_hex(text)


def ratio(palette, colour):
    return vc.contrast(c(colour), c(palette.background))


# --- the pure derivation -----------------------------------------------------------


def test_hex_strings_are_validated():
    assert vc.parse_hex("#A1b2C3") == (0xA1, 0xB2, 0xC3)
    assert vc.normalise(" #A1B2C3 ") == "#a1b2c3"
    for bad in ("", "red", "#fff", "#12345", "#1234567", "#gggggg", "a1b2c3", None, 5, "#12 456"):
        assert vc.parse_hex(bad) is None
        assert vc.normalise(bad) is None
    assert vc.clean_pair("#ffffff", "#000000") == ("#ffffff", "#000000")
    assert vc.clean_pair("#ffffff", "nope") is None
    assert vc.clean_pair("", "#000000") is None


def test_contrast_is_the_wcag_ratio():
    assert vc.contrast(c(WHITE), c(BLACK)) == pytest.approx(21.0)
    assert vc.contrast(c(WHITE), c(WHITE)) == pytest.approx(1.0)
    assert vc.contrast(c(WHITE), c(GREY)) == pytest.approx(3.95, abs=0.02)


@pytest.mark.parametrize("name", sorted(BACKGROUNDS))
def test_every_colour_keeps_three_to_one_against_any_background(name):
    bg, trace = BACKGROUNDS[name]
    pal = vc.custom_palette(bg, trace)
    assert pal.name == "custom" and pal.background == bg and pal.trace == trace
    for field in ("trace", "trace_dim", "text", "muted", "cursor", "selection"):
        assert ratio(pal, getattr(pal, field)) >= vc.MIN_CONTRAST, (name, field)
    # the text is the strongest it can be: at least 7:1 where the background allows
    best = max(vc.contrast(c(WHITE), c(bg)), vc.contrast(c(BLACK), c(bg)))
    assert ratio(pal, pal.text) >= min(vc.TEXT_CONTRAST, best) - 0.01
    assert ratio(pal, pal.muted) <= ratio(pal, pal.text) + 1e-9
    assert pal.grid_minor is None and pal.grid_major is None
    # the decorative shades stay quieter than the text
    for field in ("time_grid", "gap"):
        assert ratio(pal, getattr(pal, field)) < ratio(pal, pal.muted)


def test_the_dark_flag_follows_what_reads_better_on_the_background():
    assert vc.custom_palette("#000000", "#ffffff").dark
    assert vc.custom_palette("#13161b", "#dde3ea").dark
    assert not vc.custom_palette("#ffffff", "#000000").dark
    assert not vc.custom_palette("#f0faf0", "#0c2a0c").dark
    # mid-grey: whichever of white and black reads better decides, never a coin flip
    for grey in range(0x60, 0xA0, 8):
        bg = f"#{grey:02x}{grey:02x}{grey:02x}"
        pal = vc.custom_palette(bg, "#ffffff" if vc.contrast(c(WHITE), c(bg)) >= 3 else "#000000")
        want = vc.contrast(c(WHITE), c(bg)) >= vc.contrast(c(BLACK), c(bg))
        assert pal.dark == want, bg


def test_the_dimmed_trace_is_paler_but_still_readable():
    pal = vc.custom_palette("#ffffff", "#000000")
    assert 3.0 <= ratio(pal, pal.trace_dim) < ratio(pal, pal.trace)
    pal = vc.custom_palette("#000000", "#ffffff")
    assert 3.0 <= ratio(pal, pal.trace_dim) < ratio(pal, pal.trace)


def test_colours_too_close_are_refused_in_plain_words():
    assert vc.problem("#ffffff", "#000000") is None
    for bg, tr in (
        ("#ffffff", "#ffffff"),
        ("#808080", "#888888"),
        ("#ffffff", "#bbbbbb"),  # 1.9:1
        ("#000000", "#222222"),
    ):
        assert vc.contrast(c(bg), c(tr)) < 3
        assert vc.problem(bg, tr) == vc.TOO_CLOSE
        with pytest.raises(ValueError, match="too close"):
            vc.custom_palette(bg, tr)
    assert vc.problem("#ffffff", "nope")  # not colours at all: also a sentence
    assert "Choose" in vc.TOO_CLOSE and "3 to 1" in vc.TOO_CLOSE


def test_the_limit_is_exactly_three_to_one():
    just_under = "#959595"  # on white: 2.99
    just_over = "#949494"  # on white: 3.03
    assert vc.contrast(c(WHITE), c(just_under)) < 3 <= vc.contrast(c(WHITE), c(just_over))
    assert vc.problem(WHITE, just_under) == vc.TOO_CLOSE
    assert vc.problem(WHITE, just_over) is None


def test_a_missing_or_bad_pair_gives_the_white_profile():
    white = vv.PROFILES["white"]
    assert vc.resolve_palette("custom", True, None) is white
    assert vc.resolve_palette("custom", True, ("nope", "#000000")) is white
    assert vc.resolve_palette("custom", True, ("#ffffff", "#fefefe")) is white
    assert vc.resolve_palette("custom", False, ("#000000", "#ffffff")).name == "custom"
    # the built-in ones are not touched
    assert vc.resolve_palette("dark", False, ("#000000", "#ffffff")) is vv.PROFILES["dark"]
    assert vc.resolve_palette("auto", True, None) is vv.PROFILES["dark"]


def test_the_palette_for_two_colours_is_one_object():
    assert vc.custom_palette("#102030", "#e0e0e0") is vc.custom_palette("#102030", "#E0E0E0")


@pytest.mark.parametrize("name", sorted(BACKGROUNDS))
def test_recording_note_and_strip_colours_stay_readable_on_any_background(name):
    bg, trace = BACKGROUNDS[name]
    pal = vc.custom_palette(bg, trace)
    colours = [vv.recording_colour(i, pal.dark) for i in range(8)]
    colours += [vv.STRIP_DATA[int(pal.dark)], vv.STRIP_GAP[int(pal.dark)]]
    from mef3io_studio.model import viewer_marks as vm

    keys = [vm.TYPE_PREFIX + code for code in (*vm.MEF_TYPES, "XXXX")]
    keys += [vm.QUICK_PREFIX + name for name in vm.QUICK_TYPES]
    colours += [vm.kind_colour(k, pal.dark) for k in keys]
    for colour in colours:
        assert ratio(pal, vc.legible_on(pal, colour)) >= vc.MIN_CONTRAST - 0.01, (name, colour)


def test_colours_that_already_read_well_are_left_alone():
    pal = vc.custom_palette("#ffffff", "#000000")
    assert vc.legible_on(pal, "#15803d") == "#15803d"
    # and the built-in profiles never change a colour at all
    for name, builtin in vv.PROFILES.items():
        assert vc.legible_on(builtin, "#facc15") == "#facc15", name


def test_the_preferences_are_declared_and_hold_words_only():
    for key in ("viewer/custom_background", "viewer/custom_trace"):
        assert PREFERENCES[key].type is str and PREFERENCES[key].default == ""
    assert "custom" in PREFERENCES["viewer/profile"].choices
    assert set(vv.PROFILE_NAMES) == set(PREFERENCES["viewer/profile"].choices)


# --- stored colours ----------------------------------------------------------------------


def test_stored_colours_are_read_back_and_bad_ones_mean_white(settings):
    settings.set("viewer/profile", "custom")
    settings.set("viewer/custom_background", "#102030")
    settings.set("viewer/custom_trace", "#E0E0E0")
    state = default_state(settings, 0)
    assert state.profile == "custom" and state.custom == ("#102030", "#e0e0e0")
    for bg, tr in (
        ("", ""),  # never chosen
        ("#102030", "garbage"),  # edited by hand
        ("#102030", "#102031"),  # not readable
        ("<script>", "#ffffff"),
    ):
        settings.set("viewer/custom_background", bg)
        settings.set("viewer/custom_trace", tr)
        state = default_state(settings, 0)
        assert state.profile == "white" and state.custom is None, (bg, tr)
    # another profile with a bad pair stored is simply that profile
    settings.set("viewer/profile", "dark")
    assert default_state(settings, 0).profile == "dark"


# --- in the window -------------------------------------------------------------------------


def _pixel(win, x, y):
    img = win.canvas.grab_traces().toImage()
    dpr = img.devicePixelRatio()
    return QColor(img.pixel(int(x * dpr), int(y * dpr)))


def _count_near(win, colour, tol=40):
    img = win.canvas.grab_traces().toImage()
    want = QColor(colour)
    n = 0
    plot = win.canvas.plot_rect()
    dpr = img.devicePixelRatio()
    for x in range(int(plot.left() * dpr) + 4, int(plot.right() * dpr) - 4, 2):
        for y in range(0, int(plot.height() * dpr), 1):
            px = QColor(img.pixel(x, y))
            if (
                abs(px.red() - want.red())
                + abs(px.green() - want.green())
                + abs(px.blue() - want.blue())
                < tol
            ):
                n += 1
    return n


def _same(a: QColor, b: QColor, tol=2):
    return all(abs(x - y) <= tol for x, y in zip(a.getRgb()[:3], b.getRgb()[:3], strict=True))


def test_own_colours_paint_the_background_and_the_traces(one, qtbot):
    win = one
    assert win.profile_box.findData("custom") >= 0
    assert win.profile_box.itemText(win.profile_box.findData("custom")) == "Your own colours…"
    assert win.set_custom_colours("#10243a", "#ffd23f") is True
    assert win.state.profile == "custom" and win.state.custom == ("#10243a", "#ffd23f")
    assert win.profile_box.currentData() == "custom"
    pal = win.canvas.palette_
    assert pal.name == "custom" and pal.dark
    # the plot area's background and the traces really use the two colours
    plot = win.canvas.plot_rect()
    assert _same(_pixel(win, plot.right() - 3, plot.bottom() - 3), QColor("#10243a"))
    assert _same(_pixel(win, 3, 3), QColor("#10243a"))  # the gutter, too
    assert _count_near(win, "#ffd23f") > 200
    # the other parts follow the same background
    assert win.strip.palette_ is pal and win.band.palette_ is pal and win.chips.palette_ is pal
    assert not win.colour_note.text() and not win.colour_note.isVisible()


def test_choosing_a_built_in_profile_again_restores_it(one, qtbot):
    win = one
    before = win.canvas.palette_
    win.set_custom_colours("#10243a", "#ffd23f")
    index = win.profile_box.findData("green")
    win.profile_box.setCurrentIndex(index)
    win.profile_box.activated.emit(index)
    settle(qtbot, win)
    assert win.state.profile == "green" and win.canvas.palette_ is vv.PROFILES["green"]
    assert _same(_pixel(win, 3, 3), QColor(vv.PROFILES["green"].background))
    index = win.profile_box.findData("auto")
    win.profile_box.setCurrentIndex(index)
    win.profile_box.activated.emit(index)
    assert win.canvas.palette_ == before
    assert _same(_pixel(win, 3, 3), QColor(before.background))
    assert win.state.custom == ("#10243a", "#ffd23f")  # remembered for next time


def test_colours_too_close_are_refused_beside_the_choice_and_nothing_changes(one, qtbot):
    win = one
    before = win.canvas.palette_
    assert win.set_custom_colours("#ffffff", "#f0f0f0") is False
    assert (
        win.canvas.palette_ is before and win.state.profile == "auto" and win.state.custom is None
    )
    assert win.colour_note.text() and win.colour_note.isVisible()
    assert win.colour_note.toolTip() == vc.TOO_CLOSE
    assert win.status_message.text() == vc.TOO_CLOSE
    # a good pair afterwards clears the note, and so does choosing another profile
    assert win.set_custom_colours("#ffffff", "#202020") is True
    assert not win.colour_note.text() and not win.colour_note.isVisible()
    assert win.status_message.text() == ""
    win.set_custom_colours("#ffffff", "#f0f0f0")
    assert win.colour_note.text()
    index = win.profile_box.findData("dark")
    win.profile_box.setCurrentIndex(index)
    win.profile_box.activated.emit(index)
    assert not win.colour_note.text()


def test_the_colour_dialogs_ask_for_the_background_then_the_trace(one, qtbot, monkeypatch):
    win = one
    asked = []

    def pick(title, initial):
        asked.append((title, initial))
        return {"Background colour": "#0a0a30", "Trace colour": "#00ff88"}[title]

    monkeypatch.setattr(win, "pick_colour", pick)
    index = win.profile_box.findData("custom")
    win.profile_box.setCurrentIndex(index)
    win.profile_box.activated.emit(index)
    assert [t for t, _ in asked] == ["Background colour", "Trace colour"]
    assert win.state.custom == ("#0a0a30", "#00ff88") and win.state.profile == "custom"
    # choosing it again offers what is in use now
    asked.clear()
    win.profile_box.activated.emit(index)
    assert asked == [("Background colour", "#0a0a30"), ("Trace colour", "#00ff88")]


def test_stopping_a_colour_dialog_changes_nothing(one, qtbot, monkeypatch):
    win = one
    before = win.canvas.palette_
    for answers in ([None], ["#0a0a30", None]):
        queue = list(answers)
        monkeypatch.setattr(win, "pick_colour", lambda _t, _i, q=queue: q.pop(0))
        index = win.profile_box.findData("custom")
        win.profile_box.setCurrentIndex(index)
        win.profile_box.activated.emit(index)
        assert win.canvas.palette_ is before and win.state.profile == "auto"
        assert win.profile_box.currentData() == "auto"  # the list shows what is used


def test_changing_colours_repaints_without_reading_or_recomputing(one, qtbot, monkeypatch):
    win = one
    qtbot.waitUntil(lambda: win.marks_loaded, timeout=10_000)
    settle(qtbot, win)
    # The one automatic read of the index comes after the page, whenever the machine gets
    # to it: let it end first, so that only reads caused by the colours are counted.
    qtbot.waitUntil(lambda: all(f.done() for f in win.controller._strip_futures), timeout=30_000)
    qtbot.wait(50)
    reads, shows = [], []
    for name in ("read_tile", "envelope"):
        real = getattr(win.data, name)
        monkeypatch.setattr(
            win.data, name, lambda *a, _r=real, _n=name, **k: (reads.append(_n), _r(*a, **k))[1]
        )
    real_show = win.controller.show
    monkeypatch.setattr(
        win.controller, "show", lambda *a, **k: (shows.append(1), real_show(*a, **k))[1]
    )
    real_request = win.controller.scheduler.request
    monkeypatch.setattr(
        win.controller.scheduler,
        "request",
        lambda *a, **k: (reads.append("request"), real_request(*a, **k))[1],
    )
    pieces = win.controller.current_pieces()
    renders = []
    real_render = win.canvas._render
    monkeypatch.setattr(win.canvas, "_render", lambda: (renders.append(1), real_render())[1])
    for bg, tr in (("#10243a", "#ffd23f"), ("#fff7e0", "#402000"), ("#808080", "#000000")):
        assert win.set_custom_colours(bg, tr)
        win.canvas.repaint()
    index = win.profile_box.findData("dark")
    win.profile_box.setCurrentIndex(index)
    win.profile_box.activated.emit(index)
    win.canvas.repaint()
    qtbot.wait(50)
    settle(qtbot, win)
    after = win.controller.current_pieces()
    # the rows are drawn from the very same prepared blocks: no new read, no new block
    assert len(after) == len(pieces) and all(
        a is not None and b is not None and a[0] is b[0] for a, b in zip(after, pieces, strict=True)
    )
    assert not reads, reads
    assert not shows  # not even a page refresh: the choice only repaints
    assert len(renders) >= 4  # it did paint again, each time
    assert win.canvas.palette_.name == "dark"


def test_custom_colours_survive_closing_and_a_bad_stored_pair_means_white(
    make_window, tmp_path, settings
):
    win = make_window(write(tmp_path / "s.mefd"), settings=settings)
    assert win.set_custom_colours("#10243a", "#FFD23F")
    win.close()
    assert settings.get("viewer/profile") == "custom"
    assert settings.get("viewer/custom_background") == "#10243a"
    assert settings.get("viewer/custom_trace") == "#ffd23f"
    again = make_window(write(tmp_path / "s2.mefd"), settings=settings)
    assert again.state.profile == "custom" and again.canvas.palette_.name == "custom"
    assert again.profile_box.currentData() == "custom"
    assert _same(_pixel(again, 3, 3), QColor("#10243a"))
    again.close()
    # a value edited by hand into something that is not a colour: the white profile
    settings.set("viewer/custom_trace", "javascript:alert(1)")
    third = make_window(write(tmp_path / "s3.mefd"), settings=settings)
    assert third.state.profile == "white" and third.canvas.palette_ is vv.PROFILES["white"]
    assert third.profile_box.currentData() == "white"


def test_only_a_chosen_colour_is_saved(make_window, tmp_path, settings):
    win = make_window(write(tmp_path / "n.mefd"), settings=settings)
    win.set_custom_colours("#ffffff", "#f0f0f0")  # refused: nothing to keep
    win.close()
    assert settings.get("viewer/custom_background") == ""
    assert settings.get("viewer/profile") == "auto"


def test_recording_and_note_colours_are_adjusted_on_a_mid_grey_background(make_window, tmp_path):
    a = write(tmp_path / "a.mefd")
    b = write(tmp_path / "b.mefd", start=946_684_800_000_000 + 120_000_000, seed=2)
    win = make_window(a, b)
    win.set_custom_colours("#808080", "#000000")
    pal = win.canvas.palette_
    for i in range(2):
        assert ratio(pal, win._rec_colour(i)) >= vc.MIN_CONTRAST - 0.01
        assert win.canvas.letter_colours[win.data.recordings[i].letter] == win._rec_colour(i)
    win.profile_box.setCurrentIndex(win.profile_box.findData("white"))
    win.profile_box.activated.emit(win.profile_box.currentIndex())
    # the built-in profile uses the table's own colours again
    assert win._rec_colour(0) == vv.recording_colour(0, False)


def test_a_window_without_settings_still_offers_the_choice(make_window, tmp_path):
    win = make_window(write(tmp_path / "x.mefd"))
    assert isinstance(win, ViewerWindow) and win.state.custom is None
