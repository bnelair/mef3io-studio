"""The viewer's side panel: a Recordings tab and a Notes and events tab
(docs/design/viewer.md, "Layout" item 6).

Names and note texts are shown only for recordings the caller says are
shown; otherwise the letter only and "(hidden)".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import tzinfo

from PySide6.QtCore import Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..model import timefmt
from ..model import viewer_marks as vm
from .recordings import readable_on

ALL_TYPES = "All types"


def _table(columns: Sequence[str], parent: QWidget) -> QTableWidget:
    table = QTableWidget(0, len(columns), parent)
    table.setHorizontalHeaderLabels(list(columns))
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    return table


class RecordingsTab(QWidget):
    """Letter (in the recording's colour), name or "(hidden)", span, channels."""

    go_to = Signal(object)  # the recording's start (µs)

    COLUMNS = ("Letter", "Recording", "From", "To", "Channels")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.table = _table(self.COLUMNS, self)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.table)
        self._starts: list[int] = []
        self.table.cellClicked.connect(lambda r, _c: self.go_to.emit(self._starts[r]))

    def refresh(self, recordings, colour_of: Callable[[int], str], shown, zone: tzinfo) -> None:
        self._starts = [r.start_us for r in recordings]
        self.table.setRowCount(len(recordings))
        for row, r in enumerate(recordings):
            colour = colour_of(r.index)
            letter = QTableWidgetItem(r.letter)
            letter.setBackground(QBrush(QColor(colour)))
            letter.setForeground(QBrush(vm_text_colour(colour)))
            name = r.name if shown(r.index) and r.name else vm.HIDDEN_TEXT
            cells = (
                letter,
                QTableWidgetItem(name),
                QTableWidgetItem(timefmt.format_time(r.start_us, zone)),
                QTableWidgetItem(timefmt.format_time(r.end_us, zone)),
                QTableWidgetItem(str(len(r.channels))),
            )
            for col, item in enumerate(cells):
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()

    def cell_text(self, row: int, col: int) -> str:
        item = self.table.item(row, col)
        return item.text() if item else ""


def vm_text_colour(colour: str) -> QColor:
    return readable_on(colour)


class NotesTab(QWidget):
    """The notes and events, sorted by time, with search and a type filter."""

    go_to = Signal(object, int)  # time (µs), recording index
    show_requested = Signal(int)  # a recording whose details are hidden
    changed = Signal()
    #: Edit mode: a row was clicked (the Mark) / the edit buttons.
    picked = Signal(object)
    add_requested = Signal()
    edit_requested = Signal()
    remove_requested = Signal()
    import_requested = Signal()
    export_requested = Signal(str, str)  # kind ("csv" | "json"), scope ("listed" | "all")

    COLUMNS = ("Time", "Rec.", "Type", "Where", "Text")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search notes and events")
        self.search.setClearButtonEnabled(True)
        self.type_box = QComboBox(self)
        self.type_box.setToolTip("Show one type only")
        self.locked = QLabel("", self)
        self.locked.setObjectName("Muted")
        self.locked.setWordWrap(True)
        self.locked.setVisible(False)
        self.failed = QLabel("The notes and events could not be read.", self)
        self.failed.setObjectName("Danger")
        self.failed.setWordWrap(True)
        self.failed.setVisible(False)
        self.hidden_box = QWidget(self)
        self.hidden_layout = QVBoxLayout(self.hidden_box)
        self.hidden_layout.setContentsMargins(0, 0, 0, 0)
        self.table = _table(self.COLUMNS, self)
        self.count = QLabel("", self)
        self.count.setObjectName("Muted")
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.type_box)
        # Edit mode: add / change / take out / import; Export is always there.
        self.edit_bar = QWidget(self)
        bar = QHBoxLayout(self.edit_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        self.add_button = QPushButton("Add…", self.edit_bar)
        self.add_button.setToolTip("Add a note or event at the pointer (A)")
        self.edit_button = QPushButton("Change…", self.edit_bar)
        self.edit_button.setToolTip("Change the selected note or event")
        self.remove_button = QPushButton("Take out", self.edit_bar)
        self.remove_button.setToolTip("Take the selected note or event out of the draft")
        self.import_button = QPushButton("Import…", self.edit_bar)
        self.import_button.setToolTip("Add notes and events from a CSV or JSON file")
        for b in (self.add_button, self.edit_button, self.remove_button, self.import_button):
            bar.addWidget(b)
        bar.addStretch(1)
        self.edit_bar.setVisible(False)
        self.export_button = QToolButton(self)
        self.export_button.setText("Export…")
        self.export_button.setToolTip("Save the notes and events as a CSV or JSON file")
        self.export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.export_button)
        self.export_actions = {}
        for scope, scope_text in (("listed", "the listed ones"), ("all", "all of them")):
            for kind in ("csv", "json"):
                act = menu.addAction(
                    f"Save {scope_text} as {kind.upper()}…",
                    lambda k=kind, sc=scope: self.export_requested.emit(k, sc),
                )
                self.export_actions[(kind, scope)] = act
        self.export_button.setMenu(menu)
        bar.addWidget(self.export_button)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addLayout(top)
        box.addWidget(self.edit_bar)
        box.addWidget(self.locked)
        box.addWidget(self.failed)
        box.addWidget(self.hidden_box)
        box.addWidget(self.table, 1)
        box.addWidget(self.count)
        #: Recording index -> its "Show" button (only while it is needed).
        self.show_buttons: dict[int, QPushButton] = {}
        #: The marks listed now, in row order.
        self.rows: list[vm.Mark] = []
        self._busy = False
        self.search.textChanged.connect(lambda _t: self.changed.emit())
        self.type_box.currentIndexChanged.connect(
            lambda _i: None if self._busy else self.changed.emit()
        )
        self.table.cellClicked.connect(self._clicked)
        self.add_button.clicked.connect(self.add_requested)
        self.edit_button.clicked.connect(self.edit_requested)
        self.remove_button.clicked.connect(self.remove_requested)
        self.import_button.clicked.connect(self.import_requested)
        self.selected_mark = None

    def set_editing(self, on: bool) -> None:
        self.edit_bar.setVisible(on)

    def select_mark(self, mark) -> None:
        """Highlight the row of ``mark`` (edit mode), or none."""
        self.selected_mark = mark
        self.table.clearSelection()
        for row, m in enumerate(self.rows):
            if mark is not None and m == mark and m.state == mark.state and m.uid == mark.uid:
                self.table.selectRow(row)
                return

    def selected_type(self) -> str | None:
        return self.type_box.currentData()

    def set_types(self, types: Sequence[tuple[str, str, int]]) -> None:
        self._busy = True
        keep = self.type_box.currentData()
        self.type_box.clear()
        self.type_box.addItem(ALL_TYPES, None)
        for key, label, count in types:
            self.type_box.addItem(f"{label} ({count})", key)
        i = self.type_box.findData(keep)
        self.type_box.setCurrentIndex(max(0, i))
        self._busy = False

    def set_failed(self, failed: bool) -> None:
        self.failed.setVisible(failed)
        if failed:
            self.count.setText("")

    def set_locked(self, letters: Sequence[str]) -> None:
        if letters:
            who = vm.join_letters(list(letters))
            noun = "Recording" if len(letters) == 1 else "Recordings"
            self.locked.setText(
                f"{noun} {who} {'is' if len(letters) == 1 else 'are'} protected with a "
                "full-access password that was not given, so their notes and events cannot be "
                "read. Open them in Explore with that password to see them here."
            )
        self.locked.setVisible(bool(letters))

    def set_hidden_recordings(self, recordings: Sequence[tuple[int, str]]) -> None:
        """A Show button for each ``(index, letter)`` whose details are hidden."""
        while self.hidden_layout.count():
            item = self.hidden_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.show_buttons = {}
        for index, letter in recordings:
            row = QWidget(self.hidden_box)
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            label = QLabel(f"Recording {letter}: the text of its notes is hidden.", row)
            label.setObjectName("Muted")
            button = QPushButton("Show", row)
            button.setToolTip(f"Show the details of recording {letter} (as in Explore)")
            button.clicked.connect(lambda _c=False, i=index: self.show_requested.emit(i))
            lay.addWidget(label, 1)
            lay.addWidget(button)
            self.hidden_layout.addWidget(row)
            self.show_buttons[index] = button

    def set_rows(self, marks: Sequence[vm.Mark], shown, letter_of, zone: tzinfo) -> None:
        self.rows = list(marks)
        self.table.setRowCount(len(marks))
        for row, m in enumerate(marks):
            ok = shown(m.recording)
            _, label = vm.kind_for(m, ok)
            where = m.channel if m.channel else "Whole recording"
            cells = (
                timefmt.format_time(m.time, zone, 3),
                letter_of(m.recording),
                label,
                where,
                vm.display_text(m, ok),
            )
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 4 and not ok:
                    item.setForeground(QBrush(QColor("#6b7280")))
                if m.state:  # edit mode: a draft row is italic, a taken-out one struck through
                    font = item.font()
                    font.setItalic(True)
                    font.setStrikeOut(m.state == "removed")
                    item.setFont(font)
                    if m.state == "removed":
                        item.setForeground(QBrush(QColor("#6b7280")))
                    if col == 0 and m.state != "removed":
                        item.setText(text + ("  (new)" if m.state == "new" else "  (changed)"))
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        self.count.setText(f"{len(marks)} listed" if marks else "Nothing to list.")

    def cell_text(self, row: int, col: int) -> str:
        item = self.table.item(row, col)
        return item.text() if item else ""

    def _clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self.rows):
            m = self.rows[row]
            self.picked.emit(m)
            self.go_to.emit(m.time, m.recording)


class SidePanel:
    """Helper that builds the two tabs into the window's tab widget."""

    @staticmethod
    def build(tabs) -> tuple[RecordingsTab, NotesTab]:
        rec, notes = RecordingsTab(tabs), NotesTab(tabs)
        tabs.addTab(rec, "Recordings")
        tabs.addTab(notes, "Notes and events")
        tabs.setMinimumWidth(tabs.fontMetrics().height() * 22)
        return rec, notes
