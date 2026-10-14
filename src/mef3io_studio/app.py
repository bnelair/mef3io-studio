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

from ._version import app_version, mef3io_version

log = logging.getLogger("mef3io_studio")

MODULES = (
    # (settings key, label, icon, page title, page text)
    (
        "explorer",
        "Explore",
        "explorer",
        "Explore recordings",
        "Open MEF 3.0 recordings and see what is in them: channels, sampling rates, "
        "start, duration, gaps, annotations and an integrity check. Nothing is "
        "changed and no signal is loaded.",
    ),
    (
        "anonymizer",
        "Anonymize",
        "anonymizer",
        "Anonymize recordings",
        "Make copies of recordings with the patient details removed and every time "
        "shifted. Each copy is checked before it is kept, and the originals are "
        "never changed.",
    ),
)


def qt_version() -> str:
    try:
        from PySide6 import QtCore

        return QtCore.qVersion()
    except Exception:
        return "not available"


def about_text() -> str:
    return (
        f"<h3>mef3io-studio {app_version()}</h3>"
        "<p>Explore and anonymize MEF 3.0 recordings, with no programming needed.</p>"
        "<p>Every file operation is done by the bundled mef3io library.</p>"
        "<table>"
        f"<tr><td>App version&nbsp;&nbsp;</td><td>{app_version()}</td></tr>"
        f"<tr><td>mef3io library&nbsp;&nbsp;</td><td>{mef3io_version()}</td></tr>"
        f"<tr><td>Qt&nbsp;&nbsp;</td><td>{qt_version()}</td></tr>"
        f"<tr><td>Python&nbsp;&nbsp;</td><td>{platform.python_version()}</td></tr>"
        "</table>"
        "<p>Apache License 2.0. Developed at the Mayo Clinic Bioelectronics "
        "Neurophysiology and Engineering Lab (BNEL).</p>"
    )


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------


def _build_main_window_class():
    # Qt is imported lazily so `--version` and argument errors stay fast.
    from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, QUrl
    from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
    from PySide6.QtWidgets import (
        QDockWidget,
        QGraphicsOpacityEffect,
        QLabel,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QSplitter,
        QStackedWidget,
        QVBoxLayout,
        QWidget,
    )

    from .common import icons
    from .common.activity import ActivityIndicator, ActivityPanel
    from .common.logging_setup import log_dir
    from .common.pages import PlaceholderPage, scaled_font
    from .common.settings import Settings
    from .common.theme import Theme, ThemeManager
    from .workers.jobs import JobManager
    from .workers.tasks import self_check

    class MainWindow(QMainWindow):
        def __init__(self, settings: Settings, themes: ThemeManager, jobs: JobManager):
            super().__init__()
            self.settings = settings
            self.themes = themes
            self.jobs = jobs
            self.setWindowTitle("mef3io-studio")
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
            side.addWidget(title)
            side.addWidget(self.module_list, 1)

            self.pages = QStackedWidget(self)
            self.page_widgets: list[PlaceholderPage] = []
            self.module_keys: list[str] = []
            for key, label, icon_name, page_title, page_text in MODULES:
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, icon_name)
                self.module_list.addItem(item)
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
            self.module_list.currentRowChanged.connect(self._switch_module)
            themes.changed.connect(self.apply_theme)
            self.apply_theme(themes.theme)
            self.restore_preferences()

        # -- menus ----------------------------------------------------------
        def _build_menus(self) -> None:
            bar = self.menuBar()
            file_menu = bar.addMenu("&File")
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
            self.about_action = QAction("&About mef3io-studio", self)
            self.about_action.setMenuRole(QAction.MenuRole.AboutRole)
            self.about_action.triggered.connect(self.show_about)
            help_menu.addAction(self.about_action)

        # -- behaviour --------------------------------------------------------
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
            self.about_action.setIcon(icons.icon("info", theme.text))
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

            def done(result):
                handle.record.result_text = (
                    f"Works: wrote and read back {result.channel_count} channels"
                )
                self.statusBar().showMessage("This installation works.", 5000)

            handle.succeeded.connect(done)
            handle.failed.connect(self.show_error)
            return handle

        def show_error(self, message) -> None:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle(message.title)
            box.setText(message.text)
            if message.detail:
                box.setDetailedText(message.detail)
            box.open()

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
            box.open()
            self._about_box = box

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
            self.activity_dock.setVisible(self.settings.get("ui/activity_visible"))
            key = self.settings.get("ui/module")
            self.module_list.setCurrentRow(
                self.module_keys.index(key) if key in self.module_keys else 0
            )

        def save_preferences(self) -> None:
            self.settings.set("ui/window_geometry", self.saveGeometry())
            self.settings.set("ui/window_state", self.saveState())
            self.settings.set("ui/main_splitter", self.splitter.saveState())
            self.settings.set("ui/activity_visible", self.activity_dock.isVisible())
            self.settings.sync()

        def closeEvent(self, event) -> None:  # noqa: N802
            self.save_preferences()
            if self.jobs.running_count():
                log.info("closing with %d running job(s); cancelling", self.jobs.running_count())
            self.jobs.shutdown()
            super().closeEvent(event)

    return MainWindow


_main_window_class = None


def main_window_class():
    global _main_window_class
    if _main_window_class is None:
        _main_window_class = _build_main_window_class()
    return _main_window_class


def create_application(argv: Sequence[str] | None = None):
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
        app = QApplication(list(argv) if argv is not None else sys.argv[:1])
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


def self_test(report_path: str | None = None) -> int:
    """Run the installation check without UI. Returns the exit code."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from .common.settings import Settings
        from .selftest import run_self_test

        result = run_self_test()
        # Also prove the bundled Qt works: build the main window offscreen,
        # with throwaway settings so the user's preferences are untouched.
        import tempfile

        app = create_application([sys.argv[0]])
        with tempfile.TemporaryDirectory() as tmp:
            window = build_main_window(app, Settings(os.path.join(tmp, "s.ini")))
            app.processEvents()
            window.jobs.shutdown()
            window.deleteLater()
            app.processEvents()
        line = (
            f"mef3io-studio self-test OK: wrote and read {result.channel_count} channels "
            f"(app {app_version()}, mef3io {result.mef3io_version}, Qt {qt_version()})"
        )
        code = 0
    except Exception as exc:  # report, never crash silently
        line = f"mef3io-studio self-test FAILED: {type(exc).__name__}: {exc}"
        log.exception("self-test failed")
        code = 1
    _emit(line)
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
    log.info("self-test %s", "passed" if code == 0 else "failed")
    return code


def parse_args(argv: Sequence[str] | None):
    parser = argparse.ArgumentParser(
        prog="mef3io-studio",
        description="Explore and anonymize MEF 3.0 recordings.",
    )
    parser.add_argument("--version", action="version", version=f"mef3io-studio {app_version()}")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="check that this installation works (no window), print OK and exit",
    )
    parser.add_argument(
        "--self-test-report",
        metavar="FILE",
        help="also write the self-test result line to FILE",
    )
    # Qt and macOS add their own arguments (e.g. -psn_...); ignore them.
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)

    from .common.logging_setup import setup_logging

    setup_logging()
    log.info(
        "mef3io-studio %s starting (mef3io %s, Python %s, %s)",
        app_version(),
        mef3io_version(),
        platform.python_version(),
        platform.system(),
    )

    if args.self_test:
        return self_test(args.self_test_report)

    app = create_application([sys.argv[0], *argv])
    window = build_main_window(app)
    window.show()
    code = app.exec()
    log.info("mef3io-studio exiting")
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
