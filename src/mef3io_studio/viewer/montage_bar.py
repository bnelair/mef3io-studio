"""The montage choice in the toolbar and the channel chooser panel."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..model.viewer_view import MONTAGE_NAMES, MONTAGE_REFERENCE, MONTAGES


class MontageBar(QWidget):
    """As recorded / Referenced to <channel> / Lead bipolar."""

    #: (montage key, reference channel or None)
    changed = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.combo = QComboBox(self)
        for key in MONTAGES:
            self.combo.addItem(MONTAGE_NAMES[key], key)
        self.reference = QComboBox(self)
        self.reference.setToolTip("The channel every other channel is shown against")
        self.reference.setMaxVisibleItems(20)
        self.reference.setVisible(False)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Montage", self))
        row.addWidget(self.combo)
        row.addWidget(self.reference)
        self._busy = False
        self.combo.currentIndexChanged.connect(self._emit)
        self.reference.currentIndexChanged.connect(self._emit)

    def set_channels(self, names: Sequence[str]) -> None:
        self._busy = True
        self.reference.clear()
        self.reference.addItems(list(names))
        self._busy = False

    @property
    def montage(self) -> str:
        return self.combo.currentData()

    @property
    def reference_channel(self) -> str | None:
        return self.reference.currentText() or None

    def set_state(self, montage: str, reference: str | None) -> None:
        self._busy = True
        self.combo.setCurrentIndex(MONTAGES.index(montage))
        if reference is not None:
            self.reference.setCurrentText(reference)
        self._busy = False
        self._show_reference()

    def _show_reference(self) -> None:
        self.reference.setVisible(self.montage == MONTAGE_REFERENCE)
        self.combo.setItemText(
            MONTAGES.index(MONTAGE_REFERENCE),
            "Referenced to"
            if self.montage == MONTAGE_REFERENCE
            else MONTAGE_NAMES[MONTAGE_REFERENCE],
        )

    def _emit(self, *_args) -> None:
        if self._busy:
            return
        self._show_reference()
        self.changed.emit(
            self.montage, self.reference_channel if self.montage == MONTAGE_REFERENCE else None
        )


class ChannelChooser(QWidget):
    """Tick the channels to show; search; Show all."""

    hidden_changed = Signal(object)  # frozenset of hidden channel names

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("Channels")
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search channels")
        self.search.setClearButtonEnabled(True)
        self.list = QListWidget(self)
        self.show_all = QPushButton("Show all", self)
        self.count = QLabel("", self)
        self.count.setObjectName("Muted")
        box = QVBoxLayout(self)
        box.addWidget(self.search)
        box.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.addWidget(self.count, 1)
        row.addWidget(self.show_all)
        box.addLayout(row)
        self.setMinimumSize(self.fontMetrics().height() * 16, self.fontMetrics().height() * 22)
        self._busy = False
        self.search.textChanged.connect(self._filter)
        self.list.itemChanged.connect(self._item_changed)
        self.show_all.clicked.connect(self._show_all)

    def set_channels(self, names: Sequence[str], hidden: frozenset) -> None:
        self._busy = True
        self.list.clear()
        for n in names:
            item = QListWidgetItem(n)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if n in hidden else Qt.CheckState.Checked)
            self.list.addItem(item)
        self._busy = False
        self._filter(self.search.text())
        self._count()

    def hidden(self) -> frozenset:
        return frozenset(
            self.list.item(i).text()
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.CheckState.Unchecked
        )

    def _count(self) -> None:
        total = self.list.count()
        shown = total - len(self.hidden())
        self.count.setText(f"{shown} of {total} shown")

    def _filter(self, text: str) -> None:
        needle = text.strip().casefold()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(bool(needle) and needle not in item.text().casefold())

    def _item_changed(self, _item) -> None:
        if self._busy:
            return
        self._count()
        self.hidden_changed.emit(self.hidden())

    def _show_all(self) -> None:
        self._busy = True
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Checked)
        self._busy = False
        self._count()
        self.hidden_changed.emit(frozenset())
