"""A layout that places widgets in a row and wraps them onto the next line
when the row runs out of room, so a row of buttons never forces the window
to be wide (laptops, large text sizes)."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QWidgetItem


class FlowLayout(QLayout):
    def __init__(self, parent=None, spacing: int = -1):
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def addWidget(self, widget) -> None:  # noqa: N802
        self.addChildWidget(widget)
        self.addItem(QWidgetItem(widget))

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.sizeHint().expandedTo(item.minimumSize()))
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _gap(self) -> int:
        if self._spacing >= 0:
            return self._spacing
        parent = self.parentWidget()
        return round(parent.fontMetrics().height() * 0.4) if parent is not None else 6

    def _arrange(self, rect: QRect, apply: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line = area.x(), area.y(), 0
        gap = self._gap()
        for item in self._items:
            if item.isEmpty():  # a hidden widget takes no room
                continue
            hint = item.sizeHint().expandedTo(item.minimumSize())
            next_x = x + hint.width() + gap
            if next_x - gap > area.right() + 1 and line > 0:
                x = area.x()
                y = y + line + gap
                next_x = x + hint.width() + gap
                line = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line = max(line, hint.height())
        return y + line - rect.y() + margins.bottom()
