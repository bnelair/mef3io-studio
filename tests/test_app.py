"""The main window: module switcher, menus, About, theme, persisted layout."""

import pytest
from PySide6.QtCore import Qt

from mef3io_studio import __version__
from mef3io_studio.app import about_text, build_main_window
from mef3io_studio.common import icons
from mef3io_studio.common.theme import DARK, LIGHT
from mef3io_studio.model.jobs import JobStatus


def test_window_has_module_switcher_and_menus(window):
    labels = [window.module_list.item(i).text() for i in range(window.module_list.count())]
    assert labels == ["Explore", "Anonymize"]
    menus = [a.text() for a in window.menuBar().actions()]
    assert menus == ["&File", "&View", "&Help"]
    window.module_list.setCurrentRow(1)
    assert window.pages.currentIndex() == 1
    assert window.settings.get("ui/module") == "anonymizer"


def test_about_shows_app_and_mef3io_versions(window, qtbot):
    import mef3io

    text = about_text()
    assert __version__ in text
    assert f"mef3io library&nbsp;&nbsp;</td><td>{mef3io.__version__}" in text
    window.show_about()
    box = window._about_box
    assert __version__ in box.text()
    box.close()


def test_theme_modes(window):
    window.set_theme_mode("dark")
    assert window.themes.theme is DARK
    assert window.settings.get("appearance/theme") == "dark"
    window.set_theme_mode("light")
    assert window.themes.theme is LIGHT
    assert window.palette().color(window.backgroundRole()).name() == LIGHT.window


def test_layout_is_remembered(qtbot, qapp, settings):
    first = build_main_window(qapp, settings)
    qtbot.addWidget(first)
    first.show()
    first.resize(900, 600)
    first.splitter.setSizes([250, 650])
    first.activity_dock.show()
    first.module_list.setCurrentRow(1)
    sizes = first.splitter.sizes()
    first.close()

    second = build_main_window(qapp, settings)
    qtbot.addWidget(second)
    second.show()
    assert second.splitter.sizes()[0] == sizes[0]
    assert second.pages.currentIndex() == 1
    assert second.activity_dock.isVisible()
    second.close()


def test_window_resizes_fluidly(window, qtbot):
    window.show()
    minimum = window.minimumSize()
    window.resize(minimum)
    qtbot.wait(10)
    assert window.size().width() >= minimum.width()
    window.resize(2400, 1400)
    qtbot.wait(10)
    assert window.pages.width() > window.sidebar.width()


def test_self_check_runs_as_background_job(window, qtbot):
    window.show()
    handle = window.run_self_check()
    assert window.activity_dock.isVisible()
    with qtbot.waitSignal(handle.finished, timeout=20_000):
        pass
    assert handle.record.status is JobStatus.SUCCEEDED
    assert "2 channels" in handle.record.result_text


def test_every_icon_renders():
    names = icons.icon_names()
    assert {"explorer", "anonymizer", "activity", "app"} <= set(names)
    for name in names:
        image = icons.render(name, 64, "#123456").toImage()
        assert not image.isNull()
        opaque = sum(
            image.pixelColor(x, y).alpha() > 0 for x in range(0, 64, 4) for y in range(0, 64, 4)
        )
        assert opaque > 0, name


def test_icons_are_crisp_at_high_dpi():
    icon = icons.icon("explorer", "#000000")
    sizes = {s.width() for s in icon.availableSizes()}
    assert max(sizes) >= 96  # enough for 48 px at 2x


def test_no_fixed_pixel_font_sizes():
    """Text follows the OS font settings: the style sheet never pins sizes."""
    from mef3io_studio.common.theme import style_sheet

    for theme in (LIGHT, DARK):
        assert "font-size" not in style_sheet(theme)
        assert "font:" not in style_sheet(theme)


def _slow(ctx):
    import time

    for _ in range(1000):
        ctx.check_cancelled()
        time.sleep(0.01)


def test_close_with_running_job_asks_and_can_keep_working(window, qtbot, monkeypatch):
    window.show()
    monkeypatch.setattr(window, "confirm_stop_running", lambda: False)
    handle = window.jobs.submit("Long", _slow)
    qtbot.waitUntil(lambda: handle.record.status is JobStatus.RUNNING, timeout=5000)
    assert window.close() is False
    assert window.isVisible()
    assert not handle.record.cancel_requested
    handle.cancel()
    qtbot.waitUntil(lambda: handle.is_finished, timeout=5000)


def test_close_with_running_job_stops_it_without_blocking(window, qtbot, monkeypatch):
    import time

    window.show()
    monkeypatch.setattr(window, "confirm_stop_running", lambda: True)
    handle = window.jobs.submit("Long", _slow)
    qtbot.waitUntil(lambda: handle.record.status is JobStatus.RUNNING, timeout=5000)
    start = time.monotonic()
    window.close()  # returns at once; the window stays while the task stops
    assert time.monotonic() - start < 0.5
    assert window._stopping_dialog is not None
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=10_000)
    assert handle.record.status is JobStatus.CANCELLED
    assert window.settings.stored_keys()


def test_close_without_jobs_saves(window, qtbot):
    window.show()
    assert window.close()
    assert window.settings.stored_keys()


def test_borders_and_focus_are_visible():
    """Control outlines and progress tracks: >= 3:1 contrast (WCAG 1.4.11);
    keyboard focus always has a visible outline."""
    from mef3io_studio.common.theme import contrast_ratio, style_sheet

    for theme in (LIGHT, DARK):
        for background in (theme.window, theme.surface, theme.surface_alt):
            assert contrast_ratio(theme.border, background) >= 3.0, (theme.name, background)
        assert contrast_ratio(theme.accent, theme.surface) >= 3.0
        assert contrast_ratio(theme.text, theme.surface) >= 7.0
        assert contrast_ratio(theme.muted, theme.window) >= 4.5
        qss = style_sheet(theme)
        assert "outline: 0" not in qss
        for selector in ("QPushButton:focus", "::item:focus", "QTableView:focus"):
            assert selector in qss


def test_theme_tells_qt_the_colour_scheme(window, monkeypatch):
    # The offscreen platform ignores the request, so record what is asked.
    from PySide6.QtGui import QStyleHints

    asked = []
    monkeypatch.setattr(QStyleHints, "setColorScheme", lambda self, s: asked.append(s))
    for mode in ("dark", "light", "system"):
        window.set_theme_mode(mode)
    assert asked == [Qt.ColorScheme.Dark, Qt.ColorScheme.Light, Qt.ColorScheme.Unknown]


def test_about_states_the_lgpl_terms():
    text = about_text()
    assert "Lesser General Public" in text and "replace the Qt libraries" in text
    assert "https://download.qt.io" in text


def test_error_dialog_hides_technical_details(window, qtbot):
    from mef3io_studio.model.errors import UserMessage

    window.show_error(UserMessage("Title", "Plain words.", "IoError: /x/y.tdat"))
    dialog = window._last_dialog
    assert dialog.details is not None
    assert not dialog.details.box.isVisible()
    assert dialog.details.toggle.text() == "Technical details"
    dialog.details.toggle.setChecked(True)
    assert dialog.details.box.isVisibleTo(dialog)
    dialog.close()


def test_version_is_shown_in_the_window(window):
    from mef3io_studio._version import version_label

    assert __version__ in window.windowTitle()
    assert window.version_label.text() == f"Version {version_label()}"


@pytest.mark.parametrize(
    ("version", "label"),
    [
        ("0.1.0.dev3", "0.1.0.dev3 (test version)"),
        ("0.2.0rc1", "0.2.0rc1 (test version)"),
        ("0.1.0", "0.1.0"),
    ],
)
def test_version_label(version, label):
    from mef3io_studio._version import version_label

    assert version_label(version) == label


def test_latest_version_dialog_links_the_releases_page(window, qtbot, monkeypatch):
    from PySide6.QtGui import QDesktopServices

    from mef3io_studio._version import RELEASES_URL

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    help_menu = window.menuBar().actions()[-1].menu()
    action = next(a for a in help_menu.actions() if a.text() == "Get the latest version…")
    action.trigger()
    box = window._latest_box
    assert RELEASES_URL in box.text() and __version__ in box.text()
    assert "never connects" in box.text()
    by_text = {b.text(): b for b in box.buttons()}
    by_text["Open the download page"].click()
    assert opened == [RELEASES_URL]
    box.close()


def test_copy_the_releases_link(window, qtbot):
    from PySide6.QtWidgets import QApplication

    from mef3io_studio._version import RELEASES_URL

    window.show_latest_version()
    box = window._latest_box
    next(b for b in box.buttons() if b.text() == "Copy the link").click()
    assert QApplication.clipboard().text() == RELEASES_URL
