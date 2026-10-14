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


def test_preferences_are_ui_only():
    # A guard against someone adding a key that could hold recording content.
    for key in PREFERENCES:
        assert key.split("/")[0] in {"appearance", "ui"}, key


def test_window_stores_only_declared_keys(window, settings):
    window.save_preferences()
    assert set(settings.stored_keys()) <= set(PREFERENCES)
