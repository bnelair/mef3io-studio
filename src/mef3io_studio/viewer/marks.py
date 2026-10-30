"""Notes and events in the viewer: the chips row above the traces and the
panel that hides or shows types (docs/design/viewer.md, "Annotations").

Widgets only: what to show (colours, labels, which recording's text may be
read) is decided by the window with ``model/viewer_marks``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..model.viewer_view import Palette
from .recordings import readable_on

#: Above this many chips on one page, they are drawn as dots only.
MAX_LABELLED = 60


@dataclass(frozen=True)
class Chip:
    """One note or event on the chips row."""

    time: int
    end: int | None
    colour: str
    label: str
    tip: str
    letter: str = ""
    kind: str = ""
    channel: str | None = None


class ChipsRow(QWidget):
    """A row of chips above the traces, one per note or event on the page,
    in the type's colour. A chip that would cover its neighbour is drawn as
    a dot. Click a chip to centre the page on it."""

    clicked = Signal(object)  # the chip's time (µs)

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self.setObjectName("ChipsRow")
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(int(self.fontMetrics().height() * 1.7))
        self.palette_ = palette
        self.t0, self.t1, self.gutter = 0, 1, 84
        self.chips: list[Chip] = []
        #: What was drawn at the last paint: (rect, chip, labelled).
        self.drawn: list[tuple[QRectF, Chip, bool]] = []

    def set_palette(self, palette: Palette) -> None:
        self.palette_ = palette
        self.update()

    def set_view(self, t0: int, t1: int, gutter: int) -> None:
        self.t0, self.t1, self.gutter = t0, max(t1, t0 + 1), gutter
        self._layout()
        self.update()

    def set_chips(self, chips: Sequence[Chip]) -> None:
        self.chips = sorted(chips, key=lambda c: c.time)
        self._layout()
        self.update()

    def x_of(self, t: float) -> float:
        return self.gutter + (t - self.t0) / (self.t1 - self.t0) * max(
            1, self.width() - self.gutter
        )

    def _layout(self) -> None:
        fm = self.fontMetrics()
        h = self.height() - 4
        self.drawn = []
        right = self.gutter - 1.0
        limit = float(self.width())
        crowded = len(self.chips) > MAX_LABELLED
        for chip in self.chips:
            x = max(float(self.gutter), self.x_of(max(chip.time, self.t0)))
            if x > limit:
                continue
            text = chip.label
            w = fm.horizontalAdvance(text) + 14
            labelled = not crowded and x >= right + 2 and x + w <= limit
            if labelled:
                rect = QRectF(x, 2, w, h)
                right = x + w
            else:
                rect = QRectF(x - 3, 2 + (h - 7) / 2, 7, 7)
            self.drawn.append((rect, chip, labelled))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout()

    def chip_at(self, pos: QPointF) -> Chip | None:
        for rect, chip, _ in reversed(self.drawn):
            if rect.adjusted(-2, -2, 2, 2).contains(pos):
                return chip
        return None

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        chip = self.chip_at(event.position())
        self.setToolTip(chip.tip if chip else "")
        self.setCursor(Qt.CursorShape.PointingHandCursor if chip else Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        chip = self.chip_at(event.position())
        if chip is not None and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(chip.time)

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = self.palette_
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor(pal.background))
        small = QFont(self.font())
        small.setPointSizeF(max(6.0, small.pointSizeF() * 0.85))
        p.setFont(small)
        fm = p.fontMetrics()
        p.setPen(QColor(pal.muted))
        p.drawText(QPointF(4, (self.height() + fm.ascent() - fm.descent()) / 2), "Notes")
        p.setFont(self.font())
        for rect, chip, labelled in self.drawn:
            colour = QColor(chip.colour)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(colour)
            if labelled:
                p.drawRoundedRect(rect, 4, 4)
                p.setPen(readable_on(chip.colour))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, chip.label)
            else:
                p.drawEllipse(rect)
            if chip.end is not None and chip.end > chip.time:  # a span: a line to its end
                x1 = min(float(self.width()), self.x_of(chip.end))
                if x1 > rect.right():
                    p.setPen(QPen(colour, 2))
                    p.drawLine(
                        QPointF(rect.right(), self.height() - 3), QPointF(x1, self.height() - 3)
                    )
        p.end()


def swatch(colour: str, size: int = 12) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(QColor(colour))
    return QIcon(pm)


class TypesPanel(QWidget):
    """Tick the types of notes and events to show (per window)."""

    hidden_changed = Signal(object)  # frozenset of hidden type keys

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("Types of notes and events")
        self.list = QListWidget(self)
        self.list.setIconSize(QSize(12, 12))
        self.show_all = QPushButton("Show all", self)
        self.empty = QLabel("There are no notes or events yet.", self)
        self.empty.setObjectName("Muted")
        box = QVBoxLayout(self)
        box.addWidget(self.empty)
        box.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.show_all)
        box.addLayout(row)
        self.setMinimumSize(self.fontMetrics().height() * 16, self.fontMetrics().height() * 14)
        self._busy = False
        self.list.itemChanged.connect(self._changed)
        self.show_all.clicked.connect(self._show_all)

    def set_types(self, types: Sequence[tuple[str, str, int, str]], hidden: frozenset) -> None:
        """``(key, label, count, colour)`` per type."""
        self._busy = True
        self.list.clear()
        for key, label, count, colour in types:
            item = QListWidgetItem(swatch(colour), f"{label} ({count})")
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if key in hidden else Qt.CheckState.Checked)
            self.list.addItem(item)
        self.empty.setVisible(not types)
        self._busy = False

    def hidden(self) -> frozenset:
        return frozenset(
            self.list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.CheckState.Unchecked
        )

    def set_hidden_key(self, key: str, hidden: bool) -> None:
        """Tick or untick one type (as a click would)."""
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == key:
                item.setCheckState(Qt.CheckState.Unchecked if hidden else Qt.CheckState.Checked)

    def _changed(self, _item) -> None:
        if not self._busy:
            self.hidden_changed.emit(self.hidden())

    def _show_all(self) -> None:
        self._busy = True
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Checked)
        self._busy = False
        self.hidden_changed.emit(frozenset())
