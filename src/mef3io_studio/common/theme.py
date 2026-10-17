"""The app's look: Qt's Fusion style, a custom light and dark palette, and a
small style sheet on top.

Why this approach: Fusion renders identically on Windows, macOS and Linux and
honours a ``QPalette`` fully, so one palette per theme gives consistent
colours everywhere with no third-party dependency (and no licence question —
the popular "Fluent" widget kits for PySide6 are GPL for free use, which does
not fit this Apache-2.0 app). The style sheet only adds what a palette cannot:
rounded corners, the sidebar, spacing. Font sizes are never set in pixels, so
text follows the operating system's font settings.

Radio buttons and checkboxes are drawn by the app (``StudioStyle``), not by
Fusion: Fusion outlines them in the window colour made darker, which is almost
invisible on the dark theme (near-black on near-black) and too faint on the
light one. Ours use the theme's ``indicator`` colour (>= 3:1, a test renders them
and measures), also when disabled and inside lists such as the channel picker.
The frames of input fields (text, number, date and time fields, lists to
choose from, text boxes) had the same fault and get the same colour, with an
accent frame while focused.

The theme follows the operating system's light/dark setting live, unless the
user picked one under View > Appearance.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleOption


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
    #: Outline of interactive controls and progress tracks: >= 3:1 against
    #: window, surface and surface_alt (WCAG 1.4.11; a test checks it).
    border: str
    #: Outline of unselected radio buttons and checkboxes, and the frame of
    #: input fields: >= 3:1 against
    #: every background they sit on, list rows included (``indicator_
    #: backgrounds``; a test renders them and measures). Stronger than
    #: ``border`` because a small ring reads weaker than a long edge.
    indicator: str
    #: Decorative separators between panels (no contrast requirement).
    divider: str
    hover: str
    accent: str
    accent_text: str
    accent_soft: str
    success: str
    danger: str


LIGHT = Theme(
    name="light", dark=False,
    window="#f5f6f8", surface="#ffffff", surface_alt="#f0f2f5", sidebar="#eceff3",
    text="#1d2228", muted="#5b6573", border="#848b95", indicator="#68707b",
    divider="#d6dbe1", hover="#e2e7ee",
    accent="#2f6feb", accent_text="#ffffff", accent_soft="#dbe6fd",
    success="#1f883d", danger="#cf222e",
)  # fmt: skip

DARK = Theme(
    name="dark", dark=True,
    window="#15171c", surface="#1c1f25", surface_alt="#22262d", sidebar="#191c21",
    text="#e5e8ec", muted="#98a2b0", border="#6b7380", indicator="#868e9a",
    divider="#2e333c", hover="#272c34",
    accent="#5b93ff", accent_text="#0d1117", accent_soft="#1f3157",
    success="#3fb950", danger="#f85149",
)  # fmt: skip

# Spacing is in em (relative to the system font), so it scales with the
# user's text size; no font sizes are set at all. Every control that can take
# keyboard focus gets a visible accent outline.
_QSS = Template("""
QMainWindow, QDialog { background: $window; }
QWidget#Sidebar { background: $sidebar; border-right: 1px solid $divider; }
QListWidget#ModuleList { background: transparent; border: none; padding: 0.5em 0.4em; }
QListWidget#ModuleList::item { padding: 0.55em 0.7em; margin: 0.12em 0; border-radius: 8px;
                               color: $text; border: 2px solid transparent; }
QListWidget#ModuleList::item:hover { background: $hover; }
QListWidget#ModuleList::item:selected { background: $accent_soft; color: $text; }
QListWidget#ModuleList:focus { outline: none; }
QListWidget#ModuleList::item:focus { border: 2px solid $accent; }
QLabel#AppTitle { color: $text; padding: 0.9em 0.9em 0.25em 0.9em; }
QLabel#Muted, QLabel#Caption { color: $muted; }
QLabel#Danger { color: $danger; }
QLabel#Good, QLabel#PrivacyGood { color: $success; }
QLabel#Bad, QLabel#PrivacyBad { color: $danger; }
QLabel#Caution, QLabel#PrivacyUnknown { color: $accent; }
QFrame#StepBar { background: transparent; }
QLabel#StepDone { color: $muted; }
QLabel#StepCurrent { color: $text; border-bottom: 2px solid $accent; }
QLabel#StepTodo { color: $muted; }
QFrame#Notice { background: $accent_soft; border-radius: 8px; }
QFrame#Warning { background: $surface; border: 1px solid $danger; border-radius: 8px; }
QFrame#DropZone { background: $surface; border: 2px dashed $border; border-radius: 12px; }
QTabWidget::pane { border: 1px solid $divider; border-radius: 8px; background: $surface; top: -1px; }
QTabBar::tab { padding: 0.35em 0.9em; color: $muted; border: 2px solid transparent; }
QTabBar::tab:selected { color: $text; border-bottom: 2px solid $accent; }
QTabBar::tab:focus { border: 2px solid $accent; }
QScrollArea#Panel { background: transparent; border: none; }
QLabel#Pill { background: $accent_soft; color: $text; border-radius: 10px; padding: 0.2em 0.7em; }
QFrame#Card { background: $surface; border: 1px solid $divider; border-radius: 12px; }
QStatusBar { background: $window; border-top: 1px solid $divider; color: $muted; }
QStatusBar::item { border: none; }
QDockWidget::title { background: $window; padding: 0.4em 0.7em; border-top: 1px solid $divider; }
QTableView { background: $surface; alternate-background-color: $surface_alt;
             border: 1px solid $border; border-radius: 8px; gridline-color: transparent;
             selection-background-color: $accent_soft; selection-color: $text; }
QTableView:focus { border: 2px solid $accent; }
QTreeView { selection-background-color: $accent_soft; selection-color: $text; }
QHeaderView::section { background: $surface_alt; color: $muted; border: none;
                       border-bottom: 1px solid $divider; padding: 0.3em 0.55em; }
QPushButton, QToolButton { border: 1px solid $border; border-radius: 7px; padding: 0.3em 0.8em;
                           background: $surface; color: $text; }
QPushButton:hover, QToolButton:hover { background: $hover; }
QPushButton:pressed, QToolButton:pressed { background: $accent_soft; }
QPushButton:focus, QToolButton:focus { border: 2px solid $accent; }
QPushButton:disabled, QToolButton:disabled { color: $muted; }
QToolButton:checked { background: $accent_soft; border: 1px solid $accent; }
QToolButton:checked:focus { border: 2px solid $accent; }
QPushButton[primary="true"] { background: $accent; color: $accent_text; border-color: $accent; }
QToolButton#StatusButton { border: 2px solid transparent; padding: 0.1em 0.4em; background: transparent; }
QToolButton#StatusButton:hover { background: $hover; }
QToolButton#StatusButton:focus { border: 2px solid $accent; }
QToolButton#DetailsToggle { border: none; background: transparent; color: $accent; padding: 0.1em 0; }
QPlainTextEdit { background: $surface_alt; color: $text; border: 1px solid $indicator; border-radius: 6px; }
QTextEdit { background: $surface; color: $text; border: 1px solid $indicator; border-radius: 6px; }
QPlainTextEdit:focus, QTextEdit:focus { border: 2px solid $accent; }
QProgressBar { border: 1px solid $border; border-radius: 4px; background: $surface; max-height: 0.6em; }
QProgressBar::chunk { border-radius: 3px; background: $accent; }
QMenu { background: $surface; border: 1px solid $divider; padding: 0.25em; }
QMenu::item { padding: 0.3em 1.4em; border-radius: 5px; }
QMenu::item:selected { background: $accent_soft; color: $text; }
QSplitter::handle { background: $divider; }
QToolTip { background: $surface; color: $text; border: 1px solid $border; padding: 0.25em 0.4em; }
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


def contrast_ratio(a: str, b: str) -> float:
    """WCAG 2 contrast ratio between two #rrggbb colours."""

    def luminance(color: str) -> float:
        c = QColor(color)
        channels = []
        for v in (c.redF(), c.greenF(), c.blueF()):
            channels.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


#: Backgrounds an unselected radio button or checkbox can sit on: pages,
#: panels, alternating and hovered list rows, selected list rows. The
#: indicator's outline (``indicator``) must reach 3:1 against every one of them.
def indicator_backgrounds(theme: Theme) -> dict[str, str]:
    return {
        "window": theme.window,
        "surface": theme.surface,
        "surface_alt": theme.surface_alt,
        "sidebar": theme.sidebar,
        "hover": theme.hover,
        "selected row": theme.accent_soft,
    }


_PE = QStyle.PrimitiveElement
_INDICATORS = (
    _PE.PE_IndicatorRadioButton,
    _PE.PE_IndicatorCheckBox,
    _PE.PE_IndicatorItemViewItemCheck,
)


class StudioStyle(QProxyStyle):
    """Fusion, with radio buttons, checkboxes and the frames of input fields
    drawn in the theme's colours.

    Only the indicator and the frame are painted here (sizes, labels, arrows
    and buttons stay Fusion's), so the controls keep their size and layout
    everywhere. Indicators: unselected = the theme's surface inside an
    ``indicator`` outline, enabled or not; selected = accent; disabled and
    selected = muted; hover and keyboard focus = accent outline. Text, number,
    date and time fields and lists to choose from (enabled, disabled,
    read-only): an ``indicator`` outline, accent (2 px) while focused. Text
    boxes get the same through the style sheet.
    """

    def __init__(self, theme: Theme):
        super().__init__("Fusion")
        self.theme = theme

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802 (Qt)
        if element in _INDICATORS:
            paint_indicator(self.theme, element, option, painter)
            return
        if element == _PE.PE_FrameLineEdit:
            paint_field_frame(self.theme, option, painter)
            return
        super().drawPrimitive(element, option, painter, widget)

    def drawComplexControl(self, control, option, painter, widget=None):  # noqa: N802 (Qt)
        super().drawComplexControl(control, option, painter, widget)
        if control in _FRAMED_CONTROLS and getattr(option, "frame", True):
            # Fusion's own outline (window colour made darker) is painted
            # over with ours, at the same place. A field set to have no
            # frame (setFrame(False)) gets none.
            paint_field_frame(self.theme, option, painter)


_CC = QStyle.ComplexControl
_FRAMED_CONTROLS = (_CC.CC_SpinBox, _CC.CC_ComboBox)


def paint_field_frame(theme: Theme, option: QStyleOption, painter: QPainter) -> None:
    """The outline of an input field: ``indicator``, accent while focused."""
    state = option.state
    focus = bool(state & QStyle.StateFlag.State_HasFocus) and bool(
        state & QStyle.StateFlag.State_Enabled
    )
    pen_width = 2.0 if focus else 1.0
    inset = pen_width / 2
    rect = QRectF(option.rect).adjusted(inset, inset, -inset, -inset)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(QColor(theme.accent if focus else theme.indicator), pen_width))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect, 3.0, 3.0)
    painter.restore()


def paint_indicator(theme: Theme, element, option: QStyleOption, painter: QPainter) -> None:
    state = option.state
    S = QStyle.StateFlag
    enabled = bool(state & S.State_Enabled)
    radio = element == _PE.PE_IndicatorRadioButton
    on = bool(state & S.State_On)
    partial = bool(state & S.State_NoChange) and not radio
    focus = bool(state & S.State_HasFocus) and bool(state & S.State_KeyboardFocusChange)
    hover = enabled and bool(state & S.State_MouseOver)
    filled = (on or partial) and not radio
    mark = QColor(theme.accent if enabled else theme.muted)

    side = min(option.rect.width(), option.rect.height())
    box = QRectF(
        option.rect.x() + (option.rect.width() - side) / 2,
        option.rect.y() + (option.rect.height() - side) / 2,
        side,
        side,
    )
    pen_width = 2.0 if focus else 1.0
    inset = pen_width / 2
    ring = box.adjusted(inset, inset, -inset, -inset)
    if focus or (hover and not filled):
        outline = QColor(theme.accent)
    elif filled or (on and radio):
        outline = mark
    else:
        outline = QColor(theme.indicator)
    if filled:
        fill = mark
    elif enabled and state & S.State_Sunken:
        fill = QColor(theme.hover)
    else:
        fill = QColor(theme.surface)

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(outline, pen_width))
    painter.setBrush(fill)
    if radio:
        painter.drawEllipse(ring)
        if on:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(mark)
            r = side * 0.25
            painter.drawEllipse(box.center(), r, r)
    else:
        corner = max(2.0, side * 0.18)
        painter.drawRoundedRect(ring, corner, corner)
        if filled:
            tick = QPen(QColor(theme.accent_text if enabled else theme.surface))
            tick.setWidthF(max(1.5, side / 8))
            tick.setCapStyle(Qt.PenCapStyle.RoundCap)
            tick.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(tick)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            x, y, w, h = box.x(), box.y(), box.width(), box.height()
            if partial and not on:
                painter.drawLine(QPointF(x + w * 0.27, y + h / 2), QPointF(x + w * 0.73, y + h / 2))
            else:
                path = QPainterPath(QPointF(x + w * 0.25, y + h * 0.52))
                path.lineTo(QPointF(x + w * 0.43, y + h * 0.70))
                path.lineTo(QPointF(x + w * 0.76, y + h * 0.31))
                painter.drawPath(path)
    painter.restore()


MODES = ("system", "light", "dark")


class ThemeManager(QObject):
    """Applies the theme and re-applies it when the OS switches light/dark."""

    changed = Signal(object)  # Theme

    def __init__(self, app: QApplication, mode: str = "system"):
        super().__init__(app)
        self._app = app
        self._mode = mode if mode in MODES else "system"
        self.theme: Theme = LIGHT
        # Kept referenced here as well: the app owns it, Python must not
        # collect the wrapper while Qt still paints with it.
        self.style = StudioStyle(self.theme)
        app.setStyle(self.style)
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
        # Tell Qt (and through it the OS) which scheme is in use, so native
        # title bars and dialogs follow the choice too. "system" = Unknown,
        # which hands the decision back to the OS.
        scheme = {
            "light": Qt.ColorScheme.Light,
            "dark": Qt.ColorScheme.Dark,
        }.get(self._mode, Qt.ColorScheme.Unknown)
        hints = self._app.styleHints()
        if hasattr(hints, "setColorScheme"):
            hints.setColorScheme(scheme)
        self.theme = self.resolve()
        self.style.theme = self.theme
        self._app.setPalette(build_palette(self.theme))
        self._app.setStyleSheet(style_sheet(self.theme))
        self.changed.emit(self.theme)

    def _on_system_changed(self, *_args) -> None:
        if self._mode == "system":
            self.apply()
