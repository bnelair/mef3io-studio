"""Entry point and main window.

``mef3io-studio`` starts the app. ``mef3io-studio --self-test`` shows no
window: it checks that mef3io and Qt work in this installation, prints one
line starting with ``mef3io-studio self-test OK`` and exits 0 (non-zero on
failure). Packaged builds are smoke-tested with it in CI.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
from collections.abc import Sequence

from ._version import RELEASES_URL, app_version, mef3io_version, version_label

log = logging.getLogger("mef3io_studio")

#: How long the notification's tray icon stays before it is hidden again.
NOTIFY_SECONDS = 15

MODULES = (
    # (settings key, label, icon, page title, page text)
    (
        "explorer",
        "Explore",
        "explorer",
        "Explore recordings",
        "Open MEF 3.0 recordings and see what is in them: channels, sampling rates, "
        "when they start, how long they run, gaps, notes, and a check for problems. "
        "Opening a recording never changes it.",
    ),
    (
        "anonymizer",
        "Anonymize",
        "anonymizer",
        "Anonymize recordings",
        "Make copies of recordings with the patient details removed and every time "
        "moved by the same amount. Each copy is checked before it is kept, and the "
        "originals are never changed.",
    ),
    (
        "joiner",
        "Join or trim",
        "join",
        "Join or trim recordings",
        "Keep only part of a recording, or join parts of several recordings into one new "
        "recording. The new recording is checked before it is kept, and the originals are "
        "never changed.",
    ),
)


def qt_version() -> str:
    try:
        from PySide6 import QtCore

        return QtCore.qVersion()
    except Exception:
        return "not available"


def licenses_dir():
    """The bundled third-party licence texts (packaged builds only)."""
    from pathlib import Path

    base = getattr(sys, "_MEIPASS", None)
    if base:
        folder = Path(base) / "THIRD_PARTY_LICENSES"
        if folder.is_dir():
            return folder
    return None


QT_SOURCE_URL = "https://download.qt.io/official_releases/qt/"
PYSIDE_SOURCE_URL = "https://code.qt.io/cgit/pyside/pyside-setup.git/"


def about_text() -> str:
    return (
        f"<h3>mef3io-studio {app_version()}</h3>"
        "<p>Explore, anonymize, join and trim MEF 3.0 recordings, with no programming needed.</p>"
        "<p>Every file operation is done by the bundled mef3io library.</p>"
        "<table>"
        f"<tr><td>App version&nbsp;&nbsp;</td><td>{app_version()}</td></tr>"
        f"<tr><td>mef3io library&nbsp;&nbsp;</td><td>{mef3io_version()}</td></tr>"
        f"<tr><td>Qt&nbsp;&nbsp;</td><td>{qt_version()}</td></tr>"
        f"<tr><td>Python&nbsp;&nbsp;</td><td>{platform.python_version()}</td></tr>"
        "</table>"
        "<p>mef3io-studio is free software under the Apache License 2.0. Maintained by "
        "Filip Mivalt, Bioelectronics Neurophysiology and Engineering Laboratory (BNEL) "
        "at Mayo Clinic.</p>"
        "<p>It uses <b>Qt 6</b> (through PySide6) under the GNU Lesser General Public "
        "License version 3. You may replace the Qt libraries in the app's folder with "
        "your own build of them. The source code of Qt and PySide6 is available from "
        f"{QT_SOURCE_URL} and {PYSIDE_SOURCE_URL}. The full licence texts of every "
        "bundled component are in the THIRD_PARTY_LICENSES folder next to the app "
        '("Show licences" below).</p>'
    )


def latest_version_text() -> str:
    return (
        f"<p>You are using mef3io-studio <b>{version_label()}</b>.</p>"
        "<p>New versions are published on the project's page on GitHub:<br>"
        f'<a href="{RELEASES_URL}">{RELEASES_URL}</a></p>'
        "<p>To update, download the installer for your computer from there and "
        "install it over this one. Your settings are kept.</p>"
        "<p>The app does not check for new versions by itself: it never connects "
        "to the internet.</p>"
    )


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------


def _build_main_window_class():
    # Qt is imported lazily so `--version` and argument errors stay fast.
    from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, QTimer, QUrl
    from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
    from PySide6.QtWidgets import (
        QDialog,
        QDockWidget,
        QGraphicsOpacityEffect,
        QLabel,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QProgressDialog,
        QSplitter,
        QStackedWidget,
        QVBoxLayout,
        QWidget,
    )

    from .anonymizer import AnonymizerPage
    from .common import icons
    from .common.activity import ActivityIndicator, ActivityPanel
    from .common.dialogs import MessageDialog
    from .common.logging_setup import log_dir
    from .common.pages import PlaceholderPage, scaled_font
    from .common.settings import Settings, clear_qt_dialog_history
    from .common.theme import Theme, ThemeManager
    from .explorer import ExplorerPage
    from .explorer.dialogs import TimeZoneDialog
    from .joiner import JoinerPage
    from .model.errors import UserMessage
    from .model.timefmt import zone_names
    from .workers.jobs import JobManager
    from .workers.tasks import self_check

    class MainWindow(QMainWindow):
        def __init__(self, settings: Settings, themes: ThemeManager, jobs: JobManager):
            super().__init__()
            self.settings = settings
            self.themes = themes
            self.jobs = jobs
            self._closing = False
            self._stopping_dialog = None
            self.setWindowTitle(f"mef3io-studio {app_version()}")
            self.setWindowIcon(icons.app_icon())
            em = self.fontMetrics().height()
            self.setMinimumSize(QSize(em * 40, em * 26))

            # Sidebar: module switcher
            self.sidebar = QWidget(self)
            self.sidebar.setObjectName("Sidebar")
            self.sidebar.setMinimumWidth(em * 9)
            title = QLabel("mef3io-studio", self.sidebar)
            title.setObjectName("AppTitle")
            title.setFont(scaled_font(self, 1.15, bold=True))
            self.module_list = QListWidget(self.sidebar)
            self.module_list.setObjectName("ModuleList")
            self.module_list.setIconSize(QSize(round(em * 1.2), round(em * 1.2)))
            side = QVBoxLayout(self.sidebar)
            side.setContentsMargins(0, 0, 0, 0)
            side.setSpacing(0)
            self.version_label = QLabel(f"Version {version_label()}", self.sidebar)
            self.version_label.setObjectName("AppVersion")
            self.version_label.setWordWrap(True)
            self.version_label.setToolTip("Help → Get the latest version")
            side.addWidget(title)
            side.addWidget(self.module_list, 1)
            side.addWidget(self.version_label)

            self.pages = QStackedWidget(self)
            self.page_widgets: list[QWidget] = []
            self.module_keys: list[str] = []
            self.explorer = ExplorerPage(jobs, parent=self.pages)
            self.explorer.status.connect(lambda text: self.statusBar().showMessage(text, 8000))
            self.anonymizer = AnonymizerPage(jobs, self.explorer, settings, parent=self.pages)
            self.anonymizer.status.connect(lambda text: self.statusBar().showMessage(text, 8000))
            self.anonymizer.open_copy.connect(self.open_copy_in_explorer)
            self.anonymizer.settings_requested.connect(self.show_settings)
            self.anonymizer.batch_finished.connect(self.notify)
            self.anonymizer.resume_problems.connect(self._resume_refused)
            self.joiner = JoinerPage(jobs, self.explorer, parent=self.pages)
            self.joiner.status.connect(lambda text: self.statusBar().showMessage(text, 8000))
            self.joiner.open_recording.connect(self.open_in_explorer)
            self.explorer.join_requested.connect(self.join_in_joiner)
            self.explorer.view_requested.connect(self.view_signals)
            self.joiner.view_requested.connect(self.view_signals)
            self.explorer.details_visibility_changed.connect(self._viewers_follow_privacy)
            #: Open signal viewer windows.
            self.viewers: list = []
            self._tray = None
            for key, label, icon_name, page_title, page_text in MODULES:
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, icon_name)
                self.module_list.addItem(item)
                if key == "explorer":
                    page = self.explorer
                elif key == "anonymizer":
                    page = self.anonymizer
                elif key == "joiner":
                    page = self.joiner
                else:
                    page = PlaceholderPage(icon_name, page_title, page_text, self.pages)
                self.pages.addWidget(page)
                self.page_widgets.append(page)
                self.module_keys.append(key)

            self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
            self.splitter.setObjectName("MainSplitter")
            self.splitter.addWidget(self.sidebar)
            self.splitter.addWidget(self.pages)
            self.splitter.setHandleWidth(1)
            self.splitter.setCollapsible(0, False)
            self.splitter.setCollapsible(1, False)
            self.splitter.setStretchFactor(0, 0)
            self.splitter.setStretchFactor(1, 1)
            self.splitter.setSizes([em * 12, em * 50])
            self.setCentralWidget(self.splitter)

            # Activity panel (dock) and status-bar indicator
            self.activity = ActivityPanel(jobs, self)
            self.activity_dock = QDockWidget("Activity", self)
            self.activity_dock.setObjectName("ActivityDock")
            self.activity_dock.setWidget(self.activity)
            self.activity_dock.setFeatures(
                QDockWidget.DockWidgetFeature.DockWidgetClosable
                | QDockWidget.DockWidgetFeature.DockWidgetMovable
            )
            self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.activity_dock)
            self.indicator = ActivityIndicator(jobs, self)
            self.indicator.clicked.connect(self.toggle_activity)
            self.statusBar().addPermanentWidget(self.indicator)
            self.statusBar().showMessage("Ready", 3000)
            jobs.job_added.connect(lambda _h: self.show_activity())

            self._build_menus()
            self.explorer.zone_button.clicked.connect(self.choose_time_zone)
            self.module_list.currentRowChanged.connect(self._switch_module)
            themes.changed.connect(self.apply_theme)
            self.apply_theme(themes.theme)
            self.restore_preferences()
            QTimer.singleShot(0, self._startup_offers)

        def _startup_offers(self) -> None:
            if self._closing or os.environ.get("MEF3IO_STUDIO_NO_STARTUP_OFFERS"):
                return
            from .model.journal import KEY_PROBLEM, KeyUnavailable

            try:
                self.offer_pending_work()
            except KeyUnavailable:
                log.warning("the app's sealing key could not be read; nothing was removed")
                self.show_error(UserMessage("The app's notes could not be opened", KEY_PROBLEM))
            except Exception as exc:  # noqa: BLE001 - an offer never stops the app
                log.warning("could not look for interrupted work (%s)", type(exc).__name__)

        # -- menus ----------------------------------------------------------
        def _build_menus(self) -> None:
            bar = self.menuBar()
            file_menu = bar.addMenu("&File")
            self.open_action = QAction("&Open a recording…", self)
            self.open_action.setShortcut(QKeySequence.StandardKey.Open)
            self.open_action.triggered.connect(
                lambda: self._in_explorer(self.explorer.choose_folder_recording)
            )
            file_menu.addAction(self.open_action)
            self.open_file_action = QAction("Open a single-file recording…", self)
            self.open_file_action.triggered.connect(
                lambda: self._in_explorer(self.explorer.choose_file_recording)
            )
            file_menu.addAction(self.open_file_action)
            self.scan_action = QAction("&Scan a folder for recordings…", self)
            self.scan_action.triggered.connect(
                lambda: self._in_explorer(self.explorer.choose_scan_folder)
            )
            file_menu.addAction(self.scan_action)
            file_menu.addSeparator()
            self.settings_action = QAction("Settings…", self)
            self.settings_action.setMenuRole(QAction.MenuRole.PreferencesRole)
            self.settings_action.triggered.connect(self.show_settings)
            file_menu.addAction(self.settings_action)
            file_menu.addSeparator()
            self.quit_action = QAction("&Quit", self)
            self.quit_action.setShortcut(QKeySequence.StandardKey.Quit)
            self.quit_action.setMenuRole(QAction.MenuRole.QuitRole)
            self.quit_action.triggered.connect(self.close)
            file_menu.addAction(self.quit_action)

            view_menu = bar.addMenu("&View")
            appearance = view_menu.addMenu("Appearance")
            self.theme_actions: dict[str, QAction] = {}
            group = QActionGroup(self)
            for mode, label in (
                ("system", "Follow the system"),
                ("light", "Light"),
                ("dark", "Dark"),
            ):
                action = QAction(label, self, checkable=True)
                action.setChecked(mode == self.themes.mode)
                action.triggered.connect(lambda _c=False, m=mode: self.set_theme_mode(m))
                group.addAction(action)
                appearance.addAction(action)
                self.theme_actions[mode] = action
            self.time_zone_action = QAction("Time zone…", self)
            self.time_zone_action.triggered.connect(self.choose_time_zone)
            view_menu.addAction(self.time_zone_action)
            self.activity_action = self.activity_dock.toggleViewAction()
            self.activity_action.setText("Activity panel")
            view_menu.addAction(self.activity_action)

            help_menu = bar.addMenu("&Help")
            self.self_check_action = QAction("Check this installation", self)
            self.self_check_action.triggered.connect(self.run_self_check)
            help_menu.addAction(self.self_check_action)
            self.open_logs_action = QAction("Open the log folder", self)
            self.open_logs_action.triggered.connect(self.open_log_folder)
            help_menu.addAction(self.open_logs_action)
            help_menu.addSeparator()
            self.latest_action = QAction("Get the latest version…", self)
            self.latest_action.triggered.connect(self.show_latest_version)
            help_menu.addAction(self.latest_action)
            self.about_action = QAction("&About mef3io-studio", self)
            self.about_action.setMenuRole(QAction.MenuRole.AboutRole)
            self.about_action.triggered.connect(self.show_about)
            help_menu.addAction(self.about_action)

        # -- behaviour --------------------------------------------------------
        def _in_explorer(self, action) -> None:
            self.module_list.setCurrentRow(self.module_keys.index("explorer"))
            action()

        def show_settings(self) -> None:
            from .common.settings_dialog import SettingsDialog

            dialog = SettingsDialog(self.settings, self)
            dialog.finished.connect(lambda _r: self.anonymizer.output._update())
            dialog.open()
            self._settings_dialog = dialog

        def notify(self, text: str) -> None:
            """A system notification (the text names no recording)."""
            from PySide6.QtWidgets import QApplication, QSystemTrayIcon

            QApplication.alert(self)
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            if self._tray is None:
                self._tray = QSystemTrayIcon(icons.app_icon(), self)
            self._tray.show()
            self._tray.showMessage("mef3io-studio", text, QSystemTrayIcon.MessageIcon.Information)
            # Only for the message: no lasting icon in the system tray.
            QTimer.singleShot(NOTIFY_SECONDS * 1000, self._tray.hide)

        # -- interrupted work, offered at start ---------------------------------
        def offer_pending_work(self) -> None:
            """An interrupted batch can be continued; unfinished work an
            interrupted run left behind (work copies in every work folder
            ever used, temporary archives) is never kept silently."""
            from .common.settings_dialog import work_folders_used
            from .model import journal as journal_
            from .model import pipeline

            pending = journal_.unfinished()
            claimed: set[str] = set()
            for journal in pending:
                for item in journal.payload.get("items", []):
                    claimed.add(item.get("work", ""))
                    claimed |= set(item.get("temporaries", []))
                claimed.add(journal.payload.get("bundle_temporary", ""))
            if pending:
                self.offer_resume(pending[0])
            leftovers = []
            for folder in work_folders_used(self.settings):
                leftovers += pipeline.leftover_work_copies(folder)
            leftovers += [p for p in journal_.known_temporaries() if os.path.lexists(p)]
            kept = [p for p in journal_.kept_copies() if os.path.lexists(p)]
            for path in journal_.kept_copies():
                if not os.path.lexists(path):
                    journal_.forget_kept_copy(path)
            leftovers = [p for p in dict.fromkeys(leftovers) if p not in claimed and p not in kept]
            if kept:
                self.offer_kept_removal(kept)
            if leftovers:
                self.offer_leftover_removal(leftovers)

        def ask(self, title: str, text: str, detail: str, buttons) -> str:
            """A question; returns the key of the chosen button."""
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle(title)
            box.setText(text)
            box.setInformativeText(detail)
            made = {}
            for key, label, role in buttons:
                made[box.addButton(label, role)] = key
            box.exec()
            return made.get(box.clickedButton(), buttons[-1][0])

        def offer_leftover_removal(self, leftovers: list[str]) -> bool:
            from .model import journal as journal_
            from .model import pipeline

            n = len(leftovers)
            choice = self.ask(
                "Unfinished copies",
                f"{n} unfinished anonymized {'copy was' if n == 1 else 'copies or archives were'} "
                "left behind by an interrupted run.",
                "They should hold no patient details, but they were never checked: do not use "
                "or share them. Remove them now?",
                [
                    ("remove", "Remove them", QMessageBox.ButtonRole.AcceptRole),
                    ("keep", "Not now", QMessageBox.ButtonRole.RejectRole),
                ],
            )
            if choice != "remove":
                return False
            for path in leftovers:
                if os.path.basename(path).startswith(pipeline.JOB_PREFIX):
                    pipeline.remove_work_copy(path)
                else:
                    pipeline.remove_temporary_file(path)
                    journal_.forget_temporary(path)
            return True

        def offer_kept_removal(self, kept: list[str]) -> bool:
            """Checked copies the app kept because their archive did not
            match them (not the same as never-checked leftovers)."""
            from .model import journal as journal_
            from .model import pipeline

            n = len(kept)
            choice = self.ask(
                "Copies kept after a failed archive",
                f"{n} anonymized {'copy was' if n == 1 else 'copies were'} kept in the work "
                "folder because the archive made from "
                f"{'it' if n == 1 else 'them'} did not match.",
                "These copies were checked and are complete, but they were never delivered: "
                "the archive was not kept. Make the archives again with a new batch, then "
                "remove these copies. Remove them now?",
                [
                    ("remove", "Remove them", QMessageBox.ButtonRole.DestructiveRole),
                    ("keep", "Keep them for now", QMessageBox.ButtonRole.RejectRole),
                ],
            )
            if choice != "remove":
                return False
            for path in kept:
                pipeline.remove_work_copy(path)
                journal_.forget_kept_copy(path)
            return True

        def offer_resume(self, journal) -> str:
            choice = self.ask(
                "Continue the batch?",
                f"Continue the batch: {journal.done} of {journal.total} done.",
                "A batch of anonymized copies was interrupted. Continuing checks everything "
                "again first, then makes the rest. Discarding removes its unfinished work; "
                "copies already saved stay. Until you choose, a sealed note about the batch "
                "stays in the app's own data folder (no names or dates in clear).",
                [
                    ("continue", "Continue the batch", QMessageBox.ButtonRole.AcceptRole),
                    ("discard", "Discard the batch", QMessageBox.ButtonRole.DestructiveRole),
                    ("later", "Not now", QMessageBox.ButtonRole.RejectRole),
                ],
            )
            if choice == "discard":
                self.discard_batch(journal)
            elif choice == "continue":
                self.continue_batch(journal)
            return choice

        def discard_batch(self, journal) -> None:
            from .workers.tasks import discard_batch_files

            discard_batch_files(journal)
            journal.delete()

        def ask_resume_passwords(self, sources: list[int], total: int, protected: bool, names=None):
            """({position: password of that original}, (basic, full) for the
            copies) or None when the user gives up."""
            from .anonymizer.resume import ResumePasswordsDialog

            dialog = ResumePasswordsDialog(sources, total, protected, self, names=names)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return None
            return dialog.values()

        def ask_existing(self, positions: list[int], total: int) -> str:
            """Results already at the destination for recordings the journal
            does not call finished (the app stopped right after making them)."""
            which = ", ".join(f"{n + 1}" for n in positions)
            return self.ask(
                "Results already there",
                f"Results for recording(s) {which} of {total} are already at the destination.",
                "They were made just before the interruption, but the app could not confirm "
                "them. Keep them as they are (marked as not confirmed), or make them again "
                "(they are replaced by new, checked ones)?",
                [
                    ("keep", "Keep them", QMessageBox.ButtonRole.AcceptRole),
                    ("redo", "Make them again", QMessageBox.ButtonRole.DestructiveRole),
                ],
            )

        def continue_batch(self, journal):
            from .model import pipeline

            sources, protected = pipeline.needs_passwords(journal.payload)
            passwords: dict[int, str] = {}
            output = None
            if sources or protected:
                from .model.recording import recording_name

                items = journal.payload.get("items", [])
                names = {n: recording_name(items[n]["request"]["source"]) for n in sources}
                answer = self.ask_resume_passwords(sources, journal.total, protected, names)
                if answer is None:
                    return None
                passwords, output = answer
            items = journal.payload.get("items", [])
            by_source = {
                items[n]["request"]["source"]: password for n, password in passwords.items()
            }
            self.module_list.setCurrentRow(self.module_keys.index("anonymizer"))
            return self.anonymizer.resume(
                journal, lambda source: by_source.get(source, ""), output, self.ask_existing
            )

        def _resume_refused(self, text: str) -> None:
            self.show_error(
                UserMessage(
                    "The batch cannot continue yet",
                    "Before continuing, the app checked everything again and found problems. "
                    "Nothing was written. Fix them, then choose Continue again (restart the "
                    "app), or discard the batch.",
                    text,
                )
            )

        def open_copy_in_explorer(self, path: str, password: str = "") -> None:
            """Show a copy the anonymizer made, straight on its Privacy check."""
            self.module_list.setCurrentRow(self.module_keys.index("explorer"))
            self.explorer.open_for_privacy_check(path, password)

        def open_in_explorer(self, path: str, password: str = "") -> None:
            """Show a recording the app just made (a joined one) in Explore."""
            self.module_list.setCurrentRow(self.module_keys.index("explorer"))
            self.explorer.open_and_show(path, password)

        def view_signals(self, entries: list) -> None:
            """Explore's "View signals": a viewer window with exactly those
            recordings (opened by one background task)."""
            from .viewer.launch import launch

            def add(window) -> None:
                self.viewers.append(window)
                window.closed.connect(lambda w=window: self._viewer_closed(w))

            launch(
                self.jobs,
                self.explorer.source,
                entries,
                settings=self.settings,
                theme=self.themes.theme,
                zone=self.explorer.zone,
                parent=self,
                on_window=add,
                on_failed=self.show_error,
                on_revealed=self.explorer.refresh_entry,
                on_join=self.join_window_in_joiner,
            )

        def _viewers_follow_privacy(self) -> None:
            for viewer in list(self.viewers):
                viewer.refresh_privacy()

        def _viewer_closed(self, window) -> None:
            if window in self.viewers:
                self.viewers.remove(window)

        def join_window_in_joiner(self, keys: list, start: int, end: int) -> None:
            """The viewer's "Send to Join or trim": the joiner with those
            recordings and that part of them."""
            self.module_list.setCurrentRow(self.module_keys.index("joiner"))
            self.joiner.choose_window(keys, start, end)

        def join_in_joiner(self, keys: list) -> None:
            """Explore's "Join or trim these…": the joiner, with exactly
            those recordings chosen."""
            self.module_list.setCurrentRow(self.module_keys.index("joiner"))
            self.joiner.choose(keys)

        def choose_time_zone(self) -> None:
            dialog = TimeZoneDialog(self.settings.get("time/site_zone"), self)
            dialog.accepted.connect(lambda: self.set_time_zone(dialog.zone_name()))
            dialog.open()
            self._time_zone_dialog = dialog

        def set_time_zone(self, name: str) -> None:
            if name not in zone_names():
                return
            self.settings.set("time/site_zone", "" if name == "UTC" else name)
            self.explorer.set_zone_name(name)
            self.anonymizer.set_zone_name(name)
            self.joiner.set_zone_name(name)
            for viewer in self.viewers:
                viewer.set_zone(self.explorer.zone)

        def _switch_module(self, row: int) -> None:
            if row < 0:
                return
            self.pages.setCurrentIndex(row)
            self.settings.set("ui/module", self.module_keys[row])
            self._fade_in(self.pages.currentWidget())

        def _fade_in(self, widget: QWidget) -> None:
            # A short fade; the effect is removed afterwards so it costs
            # nothing once the page is shown.
            effect = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", widget)
            anim.setDuration(140)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.finished.connect(lambda: widget.setGraphicsEffect(None))
            anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

        def apply_theme(self, theme: Theme) -> None:
            for i in range(self.module_list.count()):
                item = self.module_list.item(i)
                item.setIcon(icons.icon(item.data(Qt.ItemDataRole.UserRole), theme.text))
            for page in self.page_widgets:
                page.set_theme(theme)
            self.activity.set_theme(theme)
            self.indicator.set_theme(theme)
            for viewer in getattr(self, "viewers", []):
                viewer.set_theme(theme)
            self.about_action.setIcon(icons.icon("info", theme.text))
            self.open_action.setIcon(icons.icon("folder", theme.text))
            self.self_check_action.setIcon(icons.icon("check", theme.text))
            self.open_logs_action.setIcon(icons.icon("folder", theme.text))
            self.quit_action.setIcon(icons.icon("quit", theme.text))

        def set_theme_mode(self, mode: str) -> None:
            self.settings.set("appearance/theme", mode)
            self.themes.set_mode(mode)
            self.theme_actions[mode].setChecked(True)

        def show_activity(self) -> None:
            self.activity_dock.show()

        def toggle_activity(self) -> None:
            self.activity_dock.setVisible(not self.activity_dock.isVisible())

        def run_self_check(self):
            handle = self.jobs.submit("Checking this installation", self_check)
            record = handle.record  # captured instead of the handle: no cycle

            def done(result):
                record.result_text = f"Works: wrote and read back {result.channel_count} channels"
                self.statusBar().showMessage("This installation works.", 5000)

            handle.succeeded.connect(done)
            handle.failed.connect(self.show_error)
            return handle

        def show_error(self, message) -> None:
            dialog = MessageDialog(message, self)
            dialog.open()
            self._last_dialog = dialog

        def open_log_folder(self) -> None:
            directory = log_dir()
            directory.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

        def show_about(self) -> None:
            box = QMessageBox(self)
            box.setWindowTitle("About mef3io-studio")
            box.setIconPixmap(icons.app_icon().pixmap(64, 64))
            box.setTextFormat(Qt.TextFormat.RichText)
            box.setText(about_text())
            box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            folder = licenses_dir()
            if folder is not None:
                show = box.addButton("Show licences", QMessageBox.ButtonRole.ActionRole)
                show.clicked.connect(
                    lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
                )
            box.addButton(QMessageBox.StandardButton.Close)
            box.open()
            self._about_box = box

        def show_latest_version(self) -> None:
            box = QMessageBox(self)
            box.setWindowTitle("Get the latest version")
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            box.setIconPixmap(icons.app_icon().pixmap(64, 64))
            box.setTextFormat(Qt.TextFormat.RichText)
            box.setText(latest_version_text())
            box.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.LinksAccessibleByMouse
            )
            self._make_links_open(box)
            open_page = box.addButton("Open the download page", QMessageBox.ButtonRole.ActionRole)
            open_page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(RELEASES_URL)))
            copy = box.addButton("Copy the link", QMessageBox.ButtonRole.ActionRole)
            copy.clicked.connect(lambda: self._copy_text(RELEASES_URL, "Link copied."))
            box.addButton(QMessageBox.StandardButton.Close)
            box.open()
            self._latest_box = box

        @staticmethod
        def _make_links_open(box) -> None:
            # QMessageBox's text label: links open in the user's web browser.
            for label in box.findChildren(QLabel):
                label.setOpenExternalLinks(True)

        def _copy_text(self, text: str, status: str) -> None:
            from PySide6.QtWidgets import QApplication

            QApplication.clipboard().setText(text)
            self.statusBar().showMessage(status, 5000)

        # -- preferences ------------------------------------------------------
        def restore_preferences(self) -> None:
            geometry = self.settings.get("ui/window_geometry")
            if not geometry.isEmpty():
                self.restoreGeometry(geometry)
            else:
                em = self.fontMetrics().height()
                self.resize(em * 68, em * 44)
            state = self.settings.get("ui/window_state")
            if not state.isEmpty():
                self.restoreState(state)
            splitter = self.settings.get("ui/main_splitter")
            if not splitter.isEmpty():
                self.splitter.restoreState(splitter)
            explorer_splitter = self.settings.get("ui/explorer_splitter")
            if not explorer_splitter.isEmpty():
                self.explorer.splitter.restoreState(explorer_splitter)
            self.explorer.set_zone_name(self.settings.get("time/site_zone"))
            self.anonymizer.set_zone_name(self.settings.get("time/site_zone"))
            self.joiner.set_zone_name(self.settings.get("time/site_zone"))
            self.activity_dock.setVisible(self.settings.get("ui/activity_visible"))
            key = self.settings.get("ui/module")
            self.module_list.setCurrentRow(
                self.module_keys.index(key) if key in self.module_keys else 0
            )

        def save_preferences(self) -> None:
            self.settings.set("ui/window_geometry", self.saveGeometry())
            self.settings.set("ui/window_state", self.saveState())
            self.settings.set("ui/main_splitter", self.splitter.saveState())
            self.settings.set("ui/explorer_splitter", self.explorer.splitter.saveState())
            self.settings.set("ui/activity_visible", self.activity_dock.isVisible())
            self.settings.sync()

        # -- closing ------------------------------------------------------------
        def confirm_stop_running(self) -> bool:
            """Ask before stopping running tasks. Default answer: keep working."""
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("A task is still running")
            box.setText("A task is still running. Stop it and quit?")
            box.setInformativeText(
                "Stopping removes anything the task had started to write. "
                "Your original recordings are not affected."
            )
            stop = box.addButton("Stop and quit", QMessageBox.ButtonRole.DestructiveRole)
            keep = box.addButton("Keep working", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(keep)
            box.exec()
            return box.clickedButton() is stop

        def closeEvent(self, event) -> None:  # noqa: N802
            if self._closing:
                event.accept()
                return
            if self.jobs.running_count():
                event.ignore()
                if self._stopping_dialog is None and self.confirm_stop_running():
                    self._begin_stopping()
                return
            self._finish_close()
            event.accept()

        def _begin_stopping(self) -> None:
            """Cancel every job and wait for them WITHOUT blocking the window."""
            log.info("stopping %d running job(s) before quitting", self.jobs.running_count())
            self.jobs.cancel_all()
            dialog = QProgressDialog("Stopping…", "Quit now", 0, 0, self)
            dialog.setWindowTitle("Stopping tasks")
            dialog.setMinimumDuration(0)
            dialog.setAutoClose(False)
            dialog.setAutoReset(False)
            dialog.canceled.connect(self._quit_now)
            dialog.show()
            self._stopping_dialog = dialog
            self._stop_timer = QTimer(self)
            self._stop_timer.setInterval(100)
            self._stop_timer.timeout.connect(self._check_stopped)
            self._stop_timer.start()

        def _check_stopped(self) -> None:
            if self.jobs.is_idle():
                self._stop_timer.stop()
                if self._stopping_dialog is not None:
                    self._stopping_dialog.canceled.disconnect(self._quit_now)
                    self._stopping_dialog.close()
                self._finish_close()
                self.close()

        def _quit_now(self) -> None:
            """The user would rather not wait: leave immediately."""
            from .workers.isolated import stop_all_helpers

            log.warning("quitting without waiting for tasks to stop")
            self.save_preferences()
            stop_all_helpers()
            logging.shutdown()
            os._exit(0)

        def _finish_close(self) -> None:
            self._closing = True
            self.save_preferences()
            self.jobs.dispose(timeout_ms=2000)
            clear_qt_dialog_history()

    return MainWindow


_main_window_class = None


def main_window_class():
    global _main_window_class
    if _main_window_class is None:
        _main_window_class = _build_main_window_class()
    return _main_window_class


def create_application(argv: Sequence[str] | None = None):  # noqa: ARG001
    """The QApplication with high-DPI rounding, names and theme set up."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from .common.settings import APPLICATION, ORGANIZATION

    app = QApplication.instance()
    if app is None:
        # Exact (fractional) scale factors: crisp at 125%/150% on Windows.
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        # Only the program name: no command-line argument ever reaches Qt
        # (e.g. "-platform vnc" would start a network server).
        app = QApplication(sys.argv[:1] or ["mef3io-studio"])
    app.setOrganizationName(ORGANIZATION)
    app.setApplicationName(APPLICATION)
    app.setApplicationDisplayName("mef3io-studio")
    app.setApplicationVersion(app_version())
    return app


def build_main_window(app, settings=None):
    from .common import icons
    from .common.settings import Settings
    from .common.theme import ThemeManager
    from .workers.jobs import JobManager

    settings = settings or Settings()
    themes = ThemeManager(app, settings.get("appearance/theme"))
    app.setWindowIcon(icons.app_icon())
    jobs = JobManager(app)
    return main_window_class()(settings, themes, jobs)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _emit(line: str) -> None:
    """Print a line even from a windowed (no-console) Windows build, where
    ``sys.stdout`` is None but a parent may still have redirected stdout."""
    if sys.stdout is not None:
        print(line, flush=True)
        return
    if sys.platform == "win32":  # pragma: no cover - windowed builds only
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if not handle or handle == -1:
            if kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
                handle = kernel32.GetStdHandle(-11)
        if handle and handle != -1:
            data = (line + "\r\n").encode("utf-8")
            written = ctypes.c_ulong(0)
            kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None)


ALLOWED_PLATFORMS = {
    "linux": {"xcb", "wayland", "wayland-egl", "offscreen"},
    "win32": {"windows", "offscreen"},
    "darwin": {"cocoa", "offscreen"},
}
FORBIDDEN_ARGS = (
    "-platform",
    "-platformpluginpath",
    "-platformtheme",
    "-plugin",
    "-qwindowgeometry",
    "-display",
)


def allowed_platforms() -> set[str]:
    key = "linux" if sys.platform.startswith("linux") else sys.platform
    return ALLOWED_PLATFORMS.get(key, {"offscreen"})


def sanitize_qt_environment() -> None:
    """Ignore a QT_QPA_PLATFORM outside the allow-list (e.g. "vnc", which
    would serve the window over the network)."""
    value = os.environ.get("QT_QPA_PLATFORM")
    if not value:
        return
    names = {part.split(":")[0].strip().lower() for part in value.split(";") if part.strip()}
    if not names <= allowed_platforms():
        del os.environ["QT_QPA_PLATFORM"]
        log.warning("ignored an unsupported display setting (QT_QPA_PLATFORM)")


def forbidden_argument(argv: Sequence[str]) -> str | None:
    for arg in argv:
        name = arg.split("=", 1)[0].lower()
        if name in FORBIDDEN_ARGS or name.lstrip("-") in {a.lstrip("-") for a in FORBIDDEN_ARGS}:
            return arg.split("=", 1)[0]
    return None


def _finish_self_test(line: str, code: int, report_path: str | None) -> int:
    _emit(line)
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
    log.info("self-test %s", "passed" if code == 0 else "failed")
    return code


def _explore_test_recording(app, window, folder: str, timeout: float = 120.0) -> str:
    """Open the synthetic recording in the Explorer, as a user would, and
    wait (without blocking the event loop) until its details are shown."""
    import time
    from pathlib import Path

    from .selftest import CHANNELS, write_test_recording

    path = Path(folder) / "explore-test.mefd"
    write_test_recording(path)
    page = window.explorer
    page.open_paths([str(path)], interactive=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        entries = page.entries()
        if entries and not entries[0].busy and entries[0].outcome is not None:
            break
        time.sleep(0.02)
    else:
        raise RuntimeError("the Explorer did not open the test recording in time")
    outcome = page.entries()[0].outcome
    if outcome.details is None:
        raise RuntimeError(f"the Explorer could not open the test recording: {outcome.error}")
    channels = len(outcome.details.channels)
    if channels != len(CHANNELS) or page.detail_stack.currentWidget() is not page.tabs:
        raise RuntimeError(f"the Explorer showed {channels} channels, expected {len(CHANNELS)}")
    return f"{channels} channels in the Explorer"


def self_test(report_path: str | None = None, gui: bool = False) -> int:
    """Run the installation check. Returns the exit code.

    Without ``gui``: no window, Qt's offscreen platform (works on a headless
    machine). With ``gui``: the platform's real display plugin; the main
    window is shown for a moment, which proves the bundled plugin loads.
    """
    import tempfile

    if gui:
        if os.environ.get("QT_QPA_PLATFORM", "").startswith("offscreen"):
            return _finish_self_test(
                "mef3io-studio self-test FAILED: --self-test-gui needs a real display, "
                "but QT_QPA_PLATFORM=offscreen is set",
                1,
                report_path,
            )
    else:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtCore import QTimer

        from .common.settings import Settings
        from .selftest import run_self_test

        result = run_self_test()
        app = create_application()
        with tempfile.TemporaryDirectory() as tmp:
            # Throwaway settings, so the user's preferences are untouched.
            window = build_main_window(app, Settings(os.path.join(tmp, "s.ini")))
            shown = True
            if gui:
                state = {"shown": False}

                def check_and_quit():
                    # Checked before quitting: in Qt 6, quit() closes windows.
                    handle = window.windowHandle()
                    state["shown"] = bool(
                        window.isVisible() and handle is not None and handle.isExposed()
                    )
                    app.quit()

                window.show()
                QTimer.singleShot(800, check_and_quit)
                app.exec()
                shown = state["shown"]
            else:
                app.processEvents()
            explored = _explore_test_recording(app, window, tmp)
            platform_name = app.platformName()
            window.jobs.dispose()
            window.hide()
            window.deleteLater()
            app.processEvents()
        if not shown:
            raise RuntimeError(
                "the main window could not be shown (a locked screen, or a computer "
                "without a display, can cause this)"
            )
        line = (
            f"mef3io-studio self-test OK: wrote and read {result.channel_count} channels, "
            f"explored {explored} "
            f"(app {app_version()}, mef3io {result.mef3io_version}, Qt {qt_version()}, "
            f"display {platform_name})"
        )
        code = 0
    except Exception as exc:  # report, never crash silently
        line = f"mef3io-studio self-test FAILED: {type(exc).__name__}: {exc}"
        log.exception("self-test failed")
        code = 1
    return _finish_self_test(line, code, report_path)


def parse_args(argv: Sequence[str] | None):
    parser = argparse.ArgumentParser(
        prog="mef3io-studio",
        description="Explore, anonymize, join and trim MEF 3.0 recordings.",
    )
    parser.add_argument("--version", action="version", version=f"mef3io-studio {app_version()}")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="check that this installation works (no window), print OK and exit",
    )
    parser.add_argument(
        "--self-test-gui",
        action="store_true",
        help="like --self-test, but show the window briefly using the real display",
    )
    parser.add_argument(
        "--self-test-report",
        metavar="FILE",
        help="also write the self-test result line to FILE",
    )
    # macOS may add its own arguments (e.g. -psn_...); ignore them.
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # A helper-process start must never open the app (freeze_support in the
    # packaged launcher normally handles these before we get here).
    if any(a.startswith("--multiprocessing-fork") for a in argv):
        return 0
    bad = forbidden_argument(argv)
    if bad:
        print(f"mef3io-studio: the option {bad} is not supported", file=sys.stderr)
        return 2
    args = parse_args(argv)

    from .common.logging_setup import close_crash_file, install_qt_message_handler, setup_logging

    setup_logging(hooks=True)
    sanitize_qt_environment()
    install_qt_message_handler()
    log.info(
        "mef3io-studio %s starting (mef3io %s, Python %s, %s)",
        app_version(),
        mef3io_version(),
        platform.python_version(),
        platform.system(),
    )

    try:
        if args.self_test or args.self_test_gui:
            return self_test(args.self_test_report, gui=args.self_test_gui)

        from .common.settings import clear_qt_dialog_history

        clear_qt_dialog_history()
        from .model.anonymize import sweep_old_name_check_folders

        if sweep_old_name_check_folders():  # left by development builds before mef3io 1.4
            log.info("removed leftover name-check folders of an older version")
        app = create_application()
        window = build_main_window(app)
        window.show()
        code = app.exec()
        log.info("mef3io-studio exiting")
        return int(code)
    finally:
        close_crash_file()


if __name__ == "__main__":
    raise SystemExit(main())
