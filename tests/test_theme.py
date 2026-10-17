"""Radio buttons, checkboxes and input fields stay visible (issue #35).

Fusion outlines them in the window colour made darker: near-black on
near-black in the dark theme (1.1:1 measured), and only about 2:1 in the
light one. The app draws them itself (``theme.StudioStyle``) with the theme's
``indicator`` colour. These tests pin the colours AND measure the pixels Qt
actually renders, for plain controls (enabled and disabled) and for
checkboxes inside a list such as the channel picker (plain, alternate and
selected rows, and a disabled row), in both themes. The frames of input
fields had the same fault (1.1:1 in the dark theme) and get the same
colour: every kind the app uses, enabled, disabled and read-only, plus the
accent frame while focused.
"""

from __future__ import annotations

from collections import Counter

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStyle,
    QStyleOptionButton,
    QTableView,
    QTextEdit,
    QTimeEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from mef3io_studio.common.theme import (
    DARK,
    LIGHT,
    StudioStyle,
    ThemeManager,
    contrast_ratio,
    indicator_backgrounds,
)

#: WCAG 2.1 SC 1.4.11 (non-text contrast) for the parts of a control that
#: identify it.
MINIMUM = 3.0


@pytest.fixture
def themes(qapp):
    manager = ThemeManager(qapp, "light")
    yield manager
    manager.set_mode("system")


def _strongest(image, rect, background: str) -> float:
    """Highest contrast of any pixel inside ``rect`` against ``background``."""
    best = 1.0
    for x in range(max(0, rect.left()), min(image.width(), rect.right() + 1)):
        for y in range(max(0, rect.top()), min(image.height(), rect.bottom() + 1)):
            best = max(best, contrast_ratio(image.pixelColor(x, y).name(), background))
    return best


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=lambda t: t.name)
def test_indicator_colour_contrasts_with_every_background(theme):
    for name, background in indicator_backgrounds(theme).items():
        assert contrast_ratio(theme.indicator, background) >= MINIMUM, (theme.name, name)
    # Selected: the accent mark; disabled and selected: the muted mark.
    assert contrast_ratio(theme.accent, theme.surface) >= MINIMUM
    assert contrast_ratio(theme.muted, theme.surface) >= MINIMUM


def test_the_app_draws_the_indicators(themes, qapp):
    assert isinstance(themes.style, StudioStyle)
    themes.set_mode("dark")
    assert themes.style.theme is DARK


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("kind", [QRadioButton, QCheckBox], ids=lambda k: k.__name__)
@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "disabled"])
def test_unselected_controls_have_a_visible_outline(themes, qtbot, mode, kind, enabled):
    themes.set_mode(mode)
    theme = themes.theme
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    control = kind("Option")
    control.setEnabled(enabled)
    layout.addWidget(control)
    host.resize(240, 60)
    host.show()
    qtbot.waitExposed(host)
    image = control.grab().toImage()
    option = QStyleOptionButton()
    control.initStyleOption(option)
    element = (
        QStyle.SubElement.SE_RadioButtonIndicator
        if kind is QRadioButton
        else QStyle.SubElement.SE_CheckBoxIndicator
    )
    rect = control.style().subElementRect(element, option, control)
    # The pixel just right of the indicator is the page behind it.
    behind = image.pixelColor(rect.right() + 1, rect.top()).name()
    assert behind == theme.window
    ratio = _strongest(image, rect, behind)
    assert ratio >= MINIMUM, (mode, kind.__name__, enabled, round(ratio, 2))


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("kind", [QTreeView, QTableView], ids=lambda k: k.__name__)
def test_checkboxes_in_lists_have_a_visible_outline(themes, qtbot, mode, kind):
    """Like the channel picker (a tree) and the recordings list (a table):
    alternating rows, a selected row, a disabled row. Items carry no text,
    so the box is the only thing drawn."""
    themes.set_mode(mode)
    theme = themes.theme
    model = QStandardItemModel()
    for row in range(4):
        item = QStandardItem("")
        item.setCheckable(True)
        item.setCheckState(Qt.CheckState.Unchecked)
        if row == 3:
            item.setEnabled(False)
        model.appendRow(item)
    view = kind()
    qtbot.addWidget(view)
    if kind is QTreeView:
        view.setHeaderHidden(True)
        view.setRootIsDecorated(False)
    else:
        view.horizontalHeader().hide()
        view.verticalHeader().hide()
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setAlternatingRowColors(True)
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    view.setModel(model)
    view.resize(240, 160)
    view.show()
    qtbot.waitExposed(view)
    view.setFocus()
    view.setCurrentIndex(model.index(2, 0))
    qtbot.wait(20)
    image = view.viewport().grab().toImage()
    expected = {0: theme.surface, 1: theme.surface_alt, 2: theme.accent_soft, 3: theme.surface_alt}
    for row, background in expected.items():
        cell = view.visualRect(model.index(row, 0))
        behind = image.pixelColor(cell.right() - 2, cell.center().y()).name()
        # Fusion tints the current row very slightly.
        assert contrast_ratio(behind, background) < 1.1, (mode, row, behind)
        ratio = _strongest(image, cell, behind)
        assert ratio >= MINIMUM, (mode, kind.__name__, row, round(ratio, 2))


def _fields():
    """Every kind of framed input the app uses, by name."""

    def line(enabled=True, read_only=False):
        w = QLineEdit()
        w.setEnabled(enabled)
        w.setReadOnly(read_only)
        return w

    def combo(editable):
        w = QComboBox()
        w.setEditable(editable)
        w.addItems(["", "B"])
        return w

    def text(cls, read_only=False):
        w = cls()
        w.setReadOnly(read_only)
        return w

    return {
        "line edit": line,
        "disabled line edit": lambda: line(enabled=False),
        "read-only line edit": lambda: line(read_only=True),
        "spin box": QSpinBox,
        "date": QDateEdit,
        "time": QTimeEdit,
        "list to choose from": lambda: combo(False),
        "editable list": lambda: combo(True),
        "disabled list": lambda: (lambda w: (w.setEnabled(False), w)[1])(combo(False)),
        "plain text": lambda: text(QPlainTextEdit),
        "read-only plain text": lambda: text(QPlainTextEdit, read_only=True),
        "text": lambda: text(QTextEdit),
    }


def _frame_ratio(qtbot, themes, make, focused=False):
    """Strongest contrast on the outer two pixels of a field, against the
    page behind it (the field is grabbed with a margin of page around it).
    Focused: also that the frame is the accent colour."""
    theme = themes.theme
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    layout.setContentsMargins(6, 6, 6, 6)
    # Something else to hold the focus: the first field of a window takes
    # it otherwise (an accent frame).
    other = QPushButton("Other")
    layout.addWidget(other)
    field = make()
    field.setFixedSize(160, 90 if isinstance(field, (QPlainTextEdit, QTextEdit)) else 28)
    layout.addWidget(field)
    host.show()
    qtbot.waitExposed(host)
    if focused:
        field.setFocus(Qt.FocusReason.TabFocusReason)
        qtbot.waitUntil(field.hasFocus)
    else:
        other.setFocus()
        qtbot.waitUntil(other.hasFocus)
    qtbot.wait(10)
    image = host.grab().toImage()
    g = field.geometry()
    assert image.pixelColor(g.left() - 3, g.center().y()).name() == theme.window
    best = 1.0
    for x in range(g.left(), g.right() + 1):
        for y in (g.top(), g.top() + 1, g.bottom() - 1, g.bottom()):
            best = max(best, contrast_ratio(image.pixelColor(x, y).name(), theme.window))
    for y in range(g.top(), g.bottom() + 1):
        for x in (g.left(), g.left() + 1, g.right() - 1, g.right()):
            best = max(best, contrast_ratio(image.pixelColor(x, y).name(), theme.window))
    if focused:
        top = Counter(image.pixelColor(x, g.top()).name() for x in range(g.left(), g.right()))
        assert top.most_common(1)[0][0] == theme.accent, top.most_common(3)
    return best


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("name", list(_fields()))
def test_every_input_field_has_a_visible_frame(themes, qtbot, mode, name):
    """Issue #35's sibling: text fields, number/date/time fields, lists to
    choose from and text boxes, enabled, disabled and read-only."""
    themes.set_mode(mode)
    ratio = _frame_ratio(qtbot, themes, _fields()[name])
    assert ratio >= MINIMUM, (mode, name, round(ratio, 2))


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("name", ["line edit", "spin box", "list to choose from", "plain text"])
def test_a_focused_field_has_an_accent_frame(themes, qtbot, mode, name):
    themes.set_mode(mode)
    ratio = _frame_ratio(qtbot, themes, _fields()[name], focused=True)
    assert ratio >= MINIMUM, (mode, name, round(ratio, 2))
