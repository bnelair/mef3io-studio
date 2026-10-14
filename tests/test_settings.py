import pytest
from PySide6.QtCore import QByteArray

from mef3io_studio.common.settings import PREFERENCES


def test_defaults_and_round_trip(settings):
    assert settings.get("appearance/theme") == "system"
    settings.set("appearance/theme", "dark")
    settings.set("ui/activity_visible", True)
    settings.set("ui/main_splitter", QByteArray(b"abc"))
    settings.sync()
    assert settings.get("appearance/theme") == "dark"
    assert settings.get("ui/activity_visible") is True
    assert bytes(settings.get("ui/main_splitter")) == b"abc"


def test_only_declared_preferences_can_be_stored(settings):
    with pytest.raises(KeyError):
        settings.set("recent/last_recording", "/data/patient.mefd")
    with pytest.raises(KeyError):
        settings.get("anything/else")
    with pytest.raises(ValueError):
        settings.set("appearance/theme", "neon")
    with pytest.raises(TypeError):
        settings.set("ui/activity_visible", "yes")


def test_preferences_are_exactly_the_reviewed_list():
    # Adding a preference means updating this list in review: anything that
    # could hold a path, a name or recording content does not belong here.
    assert set(PREFERENCES) == {
        "appearance/theme",
        "ui/module",
        "ui/window_geometry",
        "ui/window_state",
        "ui/main_splitter",
        "ui/activity_visible",
    }


def test_qt_file_dialog_history_is_cleared(tmp_path):
    from PySide6.QtCore import QSettings

    from mef3io_studio.common.settings import clear_qt_dialog_history

    store = QSettings(str(tmp_path / "QtProject.ini"), QSettings.Format.IniFormat)
    store.setValue("FileDialog/lastVisited", "file:///data/Jane Doe")
    store.setValue("FileDialog/history", ["file:///data/Jane Doe"])
    store.setValue("Other/keep", 1)
    clear_qt_dialog_history(store)
    assert store.allKeys() == ["Other/keep"]


def test_window_stores_only_declared_keys(window, settings):
    window.save_preferences()
    assert set(settings.stored_keys()) <= set(PREFERENCES)
