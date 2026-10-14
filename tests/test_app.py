"""The main window: module switcher, menus, About, theme, persisted layout."""

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


def test_close_saves_and_cancels_jobs(window, qtbot):
    import time

    def slow(ctx):
        for _ in range(1000):
            ctx.check_cancelled()
            time.sleep(0.01)

    handle = window.jobs.submit("Long", slow)
    qtbot.waitUntil(lambda: handle.record.status is JobStatus.RUNNING, timeout=5000)
    window.close()
    qtbot.waitUntil(lambda: handle.is_finished, timeout=5000)
    assert handle.record.status is JobStatus.CANCELLED
    assert window.settings.stored_keys()
    assert Qt  # keep import used
