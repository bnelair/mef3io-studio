"""Radio buttons and checkboxes stay visible when unselected (issue #35).

Fusion outlines them in the window colour made darker: near-black on
near-black in the dark theme (1.1:1 measured), and only about 2:1 in the
light one. The app draws them itself (``theme.StudioStyle``) with the theme's
``indicator`` colour. These tests pin the colours AND measure the pixels Qt
actually renders, for plain controls (enabled and disabled) and for
checkboxes inside a list such as the channel picker (plain, alternate and
selected rows, and a disabled row), in both themes.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QRadioButton,
    QStyle,
    QStyleOptionButton,
    QTableView,
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
