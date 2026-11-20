"""The confirmation before the notes are written to the recording, and the list of changes.

Counts first (and per recording when several are involved), then every change, one row each:
what (added, changed, removed), where, when, its type and its text; a changed note shows old
to new, a removed note that is stored more than once says whether one copy or all go. The
list is a table, so hundreds of rows scroll and stay readable. "Show changes" opens the same
dialog without the button that writes.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import tzinfo

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
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
from ..model.write_changes import ADDED, CHANGED, REMOVED, ChangeSummary, counts_text

WORDS = {ADDED: "Added", CHANGED: "Changed", REMOVED: "Removed"}
COLUMNS = ("Change", "Recording", "Time", "Type", "Text")

#: Said before anything that is already in the recording is changed or removed.
CANNOT_UNDO = (
    "Removing or changing notes that are already in the recording cannot be undone from this "
    "app once they are written."
)


def write_label(n: int) -> str:
    return f"Write {n} change{'s' if n != 1 else ''} to the file"


class WriteDialog(QDialog):
    """``can_write`` False: the list only ("Show changes")."""

    def __init__(
        self,
        summary: ChangeSummary,
        label_of: Callable[[int | None], str],
        shown_of: Callable[[int | None], bool],
        zone: tzinfo,
        parent=None,
        can_write: bool = True,
    ):
        super().__init__(parent)
        self.summary = summary
        self.setWindowTitle(
            "Write the changes to the file?" if can_write else "Changes not written"
        )
        self.setObjectName("WriteDialog")
        self.setMinimumSize(720, 460)
        box = QVBoxLayout(self)
        self.heading = QLabel(self)
        self.heading.setWordWrap(True)
        font = QFont(self.heading.font())
        font.setBold(True)
        self.heading.setFont(font)
        total = summary.total
        self.heading.setText(
            f"{summary.counts_text()}. "
            + (
                "These changes are not in the recording yet."
                if not can_write
                else "Nothing has been written yet: this is what “Write to file” will do."
            )
        )
        box.addWidget(self.heading)
        self.per_recording = QLabel(self)
        self.per_recording.setWordWrap(True)
        lines = []
        if (
            len([r for r in summary.per_recording if r is not None]) > 1
            or None in summary.per_recording
        ):
            for rec in sorted(summary.per_recording, key=lambda r: (r is None, r or 0)):
                a, c, r = summary.per_recording[rec]
                where = label_of(rec) if rec is not None else "Recording to be chosen when writing"
                lines.append(f"{where}: {counts_text(a, c, r)}")
        self.per_recording.setText("\n".join(lines))
        self.per_recording.setVisible(bool(lines))
        box.addWidget(self.per_recording)
        self.warning = QLabel(CANNOT_UNDO, self)
        self.warning.setWordWrap(True)
        self.warning.setFont(font)
        self.warning.setVisible(can_write and bool(summary.changed or summary.removed))
        box.addWidget(self.warning)
        self.table = QTableWidget(len(summary.rows), len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self._fill(label_of, shown_of, zone)
        box.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.keep_button = QPushButton("Keep editing" if can_write else "Close", self)
        self.write_button = None
        if can_write:
            self.write_button = QPushButton(write_label(total), self)
            self.write_button.setObjectName("WriteConfirm")
            self.write_button.clicked.connect(self.accept)
            row.addWidget(self.write_button)
        self.keep_button.clicked.connect(self.reject)
        row.addWidget(self.keep_button)
        box.addLayout(row)
        # Nothing is written by an accidental Enter when something is removed or changed.
        risky = bool(summary.removed or summary.changed)
        self.keep_button.setDefault(not can_write or risky)
        if self.write_button is not None:
            self.write_button.setDefault(not risky)
            self.write_button.setAutoDefault(not risky)
        self.keep_button.setAutoDefault(not can_write or risky)
        (self.keep_button if (not can_write or risky) else self.write_button).setFocus()

    def _fill(self, label_of, shown_of, zone) -> None:
        tint = {REMOVED: QColor(200, 40, 40, 45), CHANGED: QColor(220, 170, 30, 55)}
        for r, row in enumerate(self.summary.rows):
            ok = shown_of(row.recording)
            kind = vm.kind_of(row.code, row.text, ok)[1]
            when = timefmt.format_time(row.time, zone, 3)
            if row.kind == CHANGED and row.old_time is not None and row.old_time != row.time:
                when = f"{timefmt.format_time(row.old_time, zone, 3)} → {when}"
            if not ok:
                text = vm.HIDDEN_TEXT
            elif row.kind == CHANGED:
                text = f"{row.old_text}  →  {row.text}"
            else:
                text = row.text
            if row.copies:
                text = f"{text}   ({row.copies})"
            where = label_of(row.recording) if row.recording is not None else "To be chosen"
            cells = (WORDS[row.kind], where, when, kind, text)
            for c, value in enumerate(cells):
                item = QTableWidgetItem(value)
                if row.kind in tint:
                    item.setBackground(QBrush(tint[row.kind]))
                if c == 0:
                    f = QFont(item.font())
                    f.setBold(True)
                    item.setFont(f)
                if c == 4:
                    item.setToolTip(value)
                self.table.setItem(r, c, item)
        for c, w in enumerate((90, 170, 210, 140)):
            self.table.setColumnWidth(c, w)

    def rows_text(self) -> list[tuple[str, ...]]:
        """The table as plain text (tests, and a copy of the list)."""
        return [
            tuple(self.table.item(r, c).text() for c in range(len(COLUMNS)))
            for r in range(self.table.rowCount())
        ]

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)
