"""The app's look: Qt's Fusion style, a custom light and dark palette, and a
small style sheet on top.

Why this approach: Fusion renders identically on Windows, macOS and Linux and
honours a ``QPalette`` fully, so one palette per theme gives consistent
colours everywhere with no third-party dependency (and no licence question —
the popular "Fluent" widget kits for PySide6 are GPL for free use, which does
not fit this Apache-2.0 app). The style sheet only adds what a palette cannot:
rounded corners, the sidebar, spacing. Font sizes are never set in pixels, so
text follows the operating system's font settings.

The theme follows the operating system's light/dark setting live, unless the
user picked one under View > Appearance.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication


@dataclass(frozen=True)
class Theme:
    name: str
    dark: bool
    window: str
    surface: str
    surface_alt: str
    sidebar: str
    text: str
    muted: str
    border: str
    hover: str
    accent: str
    accent_text: str
    accent_soft: str
    success: str
    danger: str


LIGHT = Theme(
    name="light", dark=False,
    window="#f5f6f8", surface="#ffffff", surface_alt="#f0f2f5", sidebar="#eceff3",
    text="#1d2228", muted="#5b6573", border="#d6dbe1", hover="#e2e7ee",
    accent="#2f6feb", accent_text="#ffffff", accent_soft="#dbe6fd",
    success="#1f883d", danger="#cf222e",
)  # fmt: skip

DARK = Theme(
    name="dark", dark=True,
    window="#15171c", surface="#1c1f25", surface_alt="#22262d", sidebar="#191c21",
    text="#e5e8ec", muted="#98a2b0", border="#2e333c", hover="#272c34",
    accent="#5b93ff", accent_text="#0d1117", accent_soft="#1f3157",
    success="#3fb950", danger="#f85149",
)  # fmt: skip

_QSS = Template("""
QMainWindow, QDialog { background: $window; }
QWidget#Sidebar { background: $sidebar; border-right: 1px solid $border; }
QListWidget#ModuleList { background: transparent; border: none; outline: 0; padding: 8px 6px; }
QListWidget#ModuleList::item { padding: 9px 10px; margin: 2px 0; border-radius: 8px; color: $text; }
QListWidget#ModuleList::item:hover { background: $hover; }
QListWidget#ModuleList::item:selected { background: $accent_soft; color: $text; }
QLabel#AppTitle { color: $text; padding: 14px 14px 4px 14px; }
QLabel#Muted, QLabel#Caption { color: $muted; }
QLabel#Pill { background: $accent_soft; color: $text; border-radius: 10px; padding: 3px 10px; }
QFrame#Card { background: $surface; border: 1px solid $border; border-radius: 12px; }
QStatusBar { background: $window; border-top: 1px solid $border; color: $muted; }
QStatusBar::item { border: none; }
QDockWidget::title { background: $window; padding: 6px 10px; border-top: 1px solid $border; }
QTableView { background: $surface; alternate-background-color: $surface_alt;
             border: 1px solid $border; border-radius: 8px; gridline-color: transparent;
             selection-background-color: $accent_soft; selection-color: $text; }
QHeaderView::section { background: $surface_alt; color: $muted; border: none;
                       border-bottom: 1px solid $border; padding: 5px 8px; }
QPushButton, QToolButton { border: 1px solid $border; border-radius: 7px; padding: 5px 12px;
                           background: $surface; color: $text; }
QPushButton:hover, QToolButton:hover { background: $hover; }
QPushButton:pressed, QToolButton:pressed { background: $accent_soft; }
QPushButton:disabled, QToolButton:disabled { color: $muted; }
QPushButton[primary="true"] { background: $accent; color: $accent_text; border-color: $accent; }
QToolButton#StatusButton { border: none; padding: 2px 6px; background: transparent; }
QToolButton#StatusButton:hover { background: $hover; }
QProgressBar { border: none; border-radius: 4px; background: $surface_alt; max-height: 8px; }
QProgressBar::chunk { border-radius: 4px; background: $accent; }
QMenu { background: $surface; border: 1px solid $border; padding: 4px; }
QMenu::item { padding: 5px 22px 5px 22px; border-radius: 5px; }
QMenu::item:selected { background: $accent_soft; color: $text; }
QSplitter::handle { background: $border; }
QSplitter::handle:horizontal { width: 1px; }
QToolTip { background: $surface; color: $text; border: 1px solid $border; padding: 4px 6px; }
""")


def build_palette(theme: Theme) -> QPalette:
    c = QColor
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: theme.window,
        QPalette.ColorRole.WindowText: theme.text,
        QPalette.ColorRole.Base: theme.surface,
        QPalette.ColorRole.AlternateBase: theme.surface_alt,
        QPalette.ColorRole.Text: theme.text,
        QPalette.ColorRole.Button: theme.surface,
        QPalette.ColorRole.ButtonText: theme.text,
        QPalette.ColorRole.ToolTipBase: theme.surface,
        QPalette.ColorRole.ToolTipText: theme.text,
        QPalette.ColorRole.PlaceholderText: theme.muted,
        QPalette.ColorRole.Highlight: theme.accent,
        QPalette.ColorRole.HighlightedText: theme.accent_text,
        QPalette.ColorRole.Link: theme.accent,
        QPalette.ColorRole.Mid: theme.border,
        QPalette.ColorRole.Midlight: theme.hover,
        QPalette.ColorRole.Dark: theme.border,
        QPalette.ColorRole.Light: theme.surface,
        QPalette.ColorRole.Shadow: "#000000",
        QPalette.ColorRole.BrightText: theme.danger,
    }
    for role, color in roles.items():
        p.setColor(role, c(color))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        p.setColor(QPalette.ColorGroup.Disabled, role, c(theme.muted))
    return p


def style_sheet(theme: Theme) -> str:
    return _QSS.substitute(theme.__dict__)


def system_prefers_dark() -> bool:
    app = QGuiApplication.instance()
    if app is None:
        return False
    return app.styleHints().colorScheme() == Qt.ColorScheme.Dark


MODES = ("system", "light", "dark")


class ThemeManager(QObject):
    """Applies the theme and re-applies it when the OS switches light/dark."""

    changed = Signal(object)  # Theme

    def __init__(self, app: QApplication, mode: str = "system"):
        super().__init__(app)
        self._app = app
        self._mode = mode if mode in MODES else "system"
        self.theme: Theme = LIGHT
        app.setStyle("Fusion")
        app.styleHints().colorSchemeChanged.connect(self._on_system_changed)
        self.apply()

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        self._mode = mode
        self.apply()

    def resolve(self) -> Theme:
        if self._mode == "dark" or (self._mode == "system" and system_prefers_dark()):
            return DARK
        return LIGHT

    def apply(self) -> None:
        self.theme = self.resolve()
        self._app.setPalette(build_palette(self.theme))
        self._app.setStyleSheet(style_sheet(self.theme))
        self.changed.emit(self.theme)

    def _on_system_changed(self, *_args) -> None:
        if self._mode == "system":
            self.apply()
