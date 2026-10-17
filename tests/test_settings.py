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
        "ui/explorer_splitter",
        # The site's IANA time zone: a place, chosen once per app profile
        # (docs/design/explorer.md); never a path or a recording's details.
        "time/site_zone",
        # The anonymizer's work folder: SEALED with the app's per-user key,
        # never a path in clear (test_work_folder_is_stored_sealed).
        "anonymizer/work_folder",
        "anonymizer/keep_awake",
    }


def test_work_folder_is_stored_sealed_and_persists(tmp_path):
    from mef3io_studio.common.settings import Settings
    from mef3io_studio.common.settings_dialog import set_work_folder, work_folder

    folder = tmp_path / "Jane Doe work"
    folder.mkdir()
    store = tmp_path / "s.ini"
    settings = Settings(store)
    set_work_folder(settings, str(folder))
    settings.sync()
    assert work_folder(Settings(store)) == str(folder)  # a new session reads it back
    raw = store.read_text(encoding="utf-8", errors="replace")
    assert "Jane" not in raw and str(tmp_path) not in raw
    set_work_folder(settings, None)
    from mef3io_studio.model.pipeline import default_work_folder

    assert work_folder(settings) == default_work_folder()


def test_the_settings_dialog_refuses_a_work_folder_inside_a_recording(qtbot, settings, tmp_path):
    from mef3io_studio.common.settings_dialog import SettingsDialog, work_folder

    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)
    inside = tmp_path / "rec.mefd" / "w"
    assert dialog.set_folder(str(inside)) is False
    assert "inside a recording" in dialog.problem.text()
    good = tmp_path / "work"
    assert dialog.set_folder(str(good)) is True
    assert work_folder(settings) == str(good)
    dialog.awake.setChecked(False)
    assert settings.get("anonymizer/keep_awake") is False


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
