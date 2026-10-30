"""The list of export ranges (docs/design/viewer.md, "Export range"): kept
beside the current range, saved as a table (CSV or JSON), never written into
a recording."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import tzinfo

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..model import timefmt
from ..model import viewer_marks as vm

IDENTIFYING_NOTE = (
    "The file holds real dates and times, and the names of the recordings. "
    "It can identify the recordings, so store and share it with the same care."
)


class RangeListDialog(QDialog):
    """The kept ranges. The window does the saving (it checks that the
    recordings are shown first)."""

    save_requested = Signal(str)  # "csv" | "json"
    removed = Signal(int)
    chosen = Signal(int)

    COLUMNS = ("Number", "From", "To", "Length", "Recordings")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export ranges")
        self.table = QTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.note = QLabel(IDENTIFYING_NOTE, self)
        self.note.setObjectName("Muted")
        self.note.setWordWrap(True)
        self.csv_button = QPushButton("Save as CSV…", self)
        self.json_button = QPushButton("Save as JSON…", self)
        self.remove_button = QPushButton("Remove", self)
        self.close_button = QPushButton("Close", self)
        row = QHBoxLayout()
        row.addWidget(self.remove_button)
        row.addStretch(1)
        row.addWidget(self.csv_button)
        row.addWidget(self.json_button)
        row.addWidget(self.close_button)
        box = QVBoxLayout(self)
        box.addWidget(self.table, 1)
        box.addWidget(self.note)
        box.addLayout(row)
        self.resize(self.fontMetrics().height() * 44, self.fontMetrics().height() * 20)
        self.csv_button.clicked.connect(lambda: self.save_requested.emit("csv"))
        self.json_button.clicked.connect(lambda: self.save_requested.emit("json"))
        self.remove_button.clicked.connect(self._remove)
        self.close_button.clicked.connect(self.close)
        self.table.cellDoubleClicked.connect(lambda r, _c: self.chosen.emit(r))

    def set_ranges(
        self,
        ranges: Sequence[tuple[int, int]],
        zone: tzinfo,
        letters_of: Callable[[int, int], str],
    ) -> None:
        self.table.setRowCount(len(ranges))
        for i, (a, b) in enumerate(ranges):
            cells = (
                str(i + 1),
                timefmt.format_time(a, zone, 3),
                timefmt.format_time(b, zone, 3),
                vm.range_length_text(a, b),
                letters_of(a, b),
            )
            for col, text in enumerate(cells):
                self.table.setItem(i, col, QTableWidgetItem(text))
        for button in (self.csv_button, self.json_button):
            button.setEnabled(bool(ranges))
        self.remove_button.setEnabled(bool(ranges))
        self.table.resizeColumnsToContents()

    def _remove(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.removed.emit(row)
