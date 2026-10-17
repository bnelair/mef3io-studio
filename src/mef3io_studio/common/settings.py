"""User preferences, stored with ``QSettings`` (the registry on Windows, a
plist on macOS, an ini file on Linux).

**Preferences only.** Nothing about a recording is ever stored here: no paths,
names, dates or contents. A recent-files list, for example, would leak which
recordings someone opened, so there is none. The wrapper enforces this with an
allowlist: a key that is not declared in :data:`PREFERENCES` cannot be written.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QSettings

ORGANIZATION = "BNEL"
APPLICATION = "mef3io-studio"


@dataclass(frozen=True)
class Preference:
    key: str
    default: Any
    type: type
    choices: tuple | None = None


PREFERENCES: dict[str, Preference] = {
    p.key: p
    for p in (
        # "system" follows the operating system's light/dark setting.
        Preference("appearance/theme", "system", str, ("system", "light", "dark")),
        Preference("ui/module", "explorer", str, ("explorer", "anonymizer")),
        Preference("ui/window_geometry", QByteArray(), QByteArray),
        Preference("ui/window_state", QByteArray(), QByteArray),
        Preference("ui/main_splitter", QByteArray(), QByteArray),
        Preference("ui/activity_visible", False, bool),
        Preference("ui/explorer_splitter", QByteArray(), QByteArray),
        # The recording site's IANA time zone ("" = show UTC). One per app
        # profile; the explorer validates it against the tz database.
        Preference("time/site_zone", "", str),
        # The anonymizer's work folder, SEALED with the app's per-user key
        # (model.journal.seal): never a path in clear. "" = the default
        # folder in the app's data area.
        Preference("anonymizer/work_folder", "", str),
        # Keep the computer from sleeping while a batch runs.
        Preference("anonymizer/keep_awake", True, bool),
    )
}


class Settings:
    """Typed access to :data:`PREFERENCES`.

    ``path`` selects an ini file instead of the platform store (tests use it so
    they never touch the real preferences).
    """

    def __init__(self, path: str | Path | None = None) -> None:
        if path is not None:
            self._qs = QSettings(str(path), QSettings.Format.IniFormat)
        else:
            self._qs = QSettings(
                QSettings.Format.NativeFormat,
                QSettings.Scope.UserScope,
                ORGANIZATION,
                APPLICATION,
            )

    @staticmethod
    def _pref(key: str) -> Preference:
        try:
            return PREFERENCES[key]
        except KeyError:
            raise KeyError(
                f"{key!r} is not a declared preference; only preferences may be stored"
            ) from None

    def get(self, key: str) -> Any:
        pref = self._pref(key)
        value = self._qs.value(key, pref.default)
        if pref.type is bool:
            value = value if isinstance(value, bool) else str(value).lower() in ("1", "true")
        elif pref.type is QByteArray:
            value = value if isinstance(value, QByteArray) else QByteArray()
        else:
            try:
                value = pref.type(value)
            except (TypeError, ValueError):
                value = pref.default
        if pref.choices is not None and value not in pref.choices:
            value = pref.default
        return value

    def set(self, key: str, value: Any) -> None:
        pref = self._pref(key)
        if pref.choices is not None and value not in pref.choices:
            raise ValueError(f"{value!r} is not one of {pref.choices} for {key!r}")
        if not isinstance(value, pref.type):
            raise TypeError(f"{key!r} expects {pref.type.__name__}")
        self._qs.setValue(key, value)

    def sync(self) -> None:
        self._qs.sync()

    def stored_keys(self) -> list[str]:
        return list(self._qs.allKeys())


def clear_qt_dialog_history(store: QSettings | None = None) -> None:
    """Remove the folder history Qt's own (non-native) file dialog keeps in
    the shared "QtProject" settings — it would record where recordings live.

    The app uses the platform's native file dialogs, without saving any
    history of its own; this is a safety net in case a Qt dialog is ever
    shown (e.g. on a platform without a native one). Called at start and exit.
    """
    store = store or QSettings(
        QSettings.Format.NativeFormat, QSettings.Scope.UserScope, "QtProject"
    )
    for key in list(store.allKeys()):
        top = key.split("/")[0]
        if top in ("FileDialog", "Qt") and any(
            word in key for word in ("lastVisited", "history", "FileDialog")
        ):
            store.remove(key)
    store.sync()
