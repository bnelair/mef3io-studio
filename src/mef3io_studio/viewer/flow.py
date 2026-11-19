"""A row of controls that wraps: Qt's usual "flow layout", written here (no dependency).

The viewer's controls (navigation, notes, export, display settings, filters ...) used to sit
in toolbars. A toolbar wider than its window loses its LAST controls to a ">>" menu, and how
wide it is depends on the font, which cannot be known. A flow layout cannot lose anything:
the controls keep their order and wrap onto as many rows as the width needs (two or three on a
wide screen, depending on the font, more on a narrow one), and ``heightForWidth`` tells the
window how tall that is, so the signal gets the rest.

A *group* (a label and its control, a button pair) is one item, so it is never split between
two rows.
"""

from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QHBoxLayout, QLayout, QSizePolicy, QWidget

#: Space between two groups on a row / between two rows (px).
H_SPACING = 12
V_SPACING = 4


class FlowLayout(QLayout):
    """Items left to right; a full row wraps onto the next."""

    def __init__(self, parent: QWidget | None = None, margin: int = 4):
        super().__init__(parent)
        self._items: list = []
        self.setContentsMargins(margin, margin, margin, margin)

    # -- the QLayout interface ----------

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._lay_out(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._lay_out(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        """The widest single item (a row can always hold one) and its height."""
        size = QSize(0, 0)
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        m: QMargins = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    # -- the work ----------

    def _lay_out(self, rect: QRect, apply: bool) -> int:
        """Place the items inside ``rect``; returns the height used (margins included)."""
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, row_height = area.x(), area.y(), 0
        for item in self._items:
            if item.isEmpty():  # a hidden widget takes no room
                continue
            hint = item.sizeHint()
            if x > area.x() and x + hint.width() > area.right() + 1:  # does not fit: next row
                x = area.x()
                y += row_height + V_SPACING
                row_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + H_SPACING
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + m.top() + m.bottom()


def group(*widgets: QWidget, parent: QWidget | None = None) -> QWidget:
    """Widgets that stay together on one row (a label and its control): one flow item. The
    group is shown when any of its widgets is (a group whose widgets are all hidden is
    hidden by :func:`sync_group`, as the flow layout skips only hidden items)."""
    box = QWidget(parent)
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)
    for w in widgets:
        row.addWidget(w)
    box.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return box


def sync_group(box: QWidget) -> None:
    """Show a group only while one of its widgets is shown."""
    box.setVisible(any(not w.isHidden() for w in box.findChildren(QWidget) if w.parent() is box))


class Controls(QWidget):
    """The viewer's controls: a :class:`FlowLayout` in a widget that says how tall it is for
    the width it is given (``heightForWidth``), so a layout around it gives the rest to the
    signal."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ViewerControls")
        self.flow = FlowLayout(self)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def add(self, *widgets: QWidget) -> QWidget:
        """One flow item: a single widget, or several kept together."""
        item = widgets[0] if len(widgets) == 1 else group(*widgets, parent=self)
        self.flow.addWidget(item)
        return item

    def rows(self) -> int:
        """How many rows the controls take now (for tests and the layout)."""
        ys = {
            w.geometry().y()
            for w in self.findChildren(QWidget)
            if w.parent() is self and not w.isHidden()
        }
        return len(ys)
