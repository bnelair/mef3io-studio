"""The recordings list: one row per recording, sortable by any column and
sorted by start time by default.

A ``QAbstractTableModel`` + ``QTableView``: only the visible rows are drawn,
so hundreds of recordings stay fast. Sorting compares plain Python keys (the
microsecond start time, the byte size, ...), never the displayed text, and
puts rows without a value (still loading, locked, unreadable) last.
"""

from __future__ import annotations

from datetime import UTC, tzinfo

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt

from ..model.recording import ACCESS_BASIC
from ..model.summary import unverified_note
from ..model.timefmt import format_rates, format_size, format_span, format_time, zone_label
from .state import RecordingEntry

COL_NAME, COL_START, COL_END, COL_DURATION, COL_CHANNELS, COL_RATES = range(6)
COL_SIZE, COL_PASSWORD, COL_PROBLEMS = range(6, 9)
COLUMNS = (
    "Recording",
    "Start",
    "End",
    "Duration",
    "Channels",
    "Sampling rate",
    "Size on disk",
    "Password",
    "Problems",
)
EntryRole = Qt.ItemDataRole.UserRole + 1
_NUMERIC = {COL_DURATION, COL_CHANNELS, COL_SIZE}


def status_text(entry: RecordingEntry) -> str:
    """Why a row has no times: shown in the Start column."""
    if entry.outcome is None or entry.busy == "Reading…":
        return "Reading…"
    if entry.outcome.needs_password:
        return "Wrong password" if entry.outcome.wrong_password else "Password needed"
    if entry.outcome.error is not None:
        return "Could not be opened"
    return ""


class RecordingTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries: list[RecordingEntry] = []
        self._rows: dict[str, int] = {}
        self.zone: tzinfo = UTC

    # -- Qt model API ---------------------------------------------------
    def rowCount(self, parent=QModelIndex()):  # noqa: B008, N802
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()):  # noqa: B008, N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            if section in (COL_START, COL_END):
                return f"{COLUMNS[section]} ({zone_label(self.zone)})"
            return COLUMNS[section]
        if role == Qt.ItemDataRole.ToolTipRole and section in (COL_START, COL_END):
            return f"Times are shown in {zone_label(self.zone)}. Change it under View > Time zone."
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        entry = self.entries[index.row()]
        col = index.column()
        if role == EntryRole:
            return entry
        if role == Qt.ItemDataRole.DisplayRole:
            return self.display(entry, col)
        if role == Qt.ItemDataRole.TextAlignmentRole and col in _NUMERIC:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.tooltip(entry, col)
        return None

    # -- what each cell says ------------------------------------------------
    def display(self, entry: RecordingEntry, col: int) -> str:
        outcome = entry.outcome
        details = entry.details
        if col == COL_NAME:
            return entry.name
        if col == COL_SIZE:
            return format_size(outcome.size_bytes) if outcome else ""
        if col == COL_PASSWORD:
            if outcome is None:
                return ""
            if outcome.needs_password:
                return "Yes, locked"
            if details is None:
                return ""
            if details.access == ACCESS_BASIC:
                return "Yes, partly unlocked"
            return "Yes" if details.password_protected else "No"
        if col == COL_PROBLEMS:
            if entry.busy and entry.outcome is not None:
                return entry.busy
            if entry.check is not None:
                return entry.check.short_status()
            if entry.check_error is not None:
                return "Could not be checked"
            return "Not checked yet"
        if details is None or entry.busy == "Reading…":
            return status_text(entry) if col == COL_START else ""
        if col == COL_START:
            text = format_time(details.start, self.zone)
            return text if details.times_verified else f"{text} (unverified)"
        if col == COL_END:
            text = format_time(details.end, self.zone)
            return text if details.times_verified else f"{text} (unverified)"
        if col == COL_DURATION:
            return format_span(details.duration_seconds)
        if col == COL_CHANNELS:
            return str(len(details.channels))
        if col == COL_RATES:
            return format_rates(details.sampling_rates)
        return ""

    def tooltip(self, entry: RecordingEntry, col: int) -> str | None:
        if col == COL_NAME:
            return entry.location
        details = entry.details
        if col in (COL_START, COL_END) and details is not None:
            notes = []
            if not details.times_verified:
                notes.append("Unverified. " + unverified_note(details))
            if details.uses_time_offset:
                notes.append(
                    "This recording hides its real date in a way that can be undone; "
                    "treat it as identifiable."
                )
            return "\n".join(notes) or None
        if col == COL_START and entry.outcome is not None and entry.outcome.error is not None:
            return entry.outcome.error.text
        return None

    def sort_key(self, row: int, col: int):
        """A comparable key: ``(0, value)`` for rows with a value, ``(1, ...)``
        for rows without one, so those sort last."""
        entry = self.entries[row]
        outcome, details = entry.outcome, entry.details
        missing = (1, entry.name.casefold())
        if col == COL_NAME:
            return (0, entry.name.casefold())
        if col == COL_SIZE:
            if outcome is None or outcome.size_bytes is None:
                return missing
            return (0, outcome.size_bytes)
        if col == COL_PASSWORD:
            return (0, self.display(entry, col))
        if col == COL_PROBLEMS:
            if entry.check is None:
                return missing
            return (0, -entry.check.problem_count, entry.check.short_status())
        if details is None:
            return missing
        if col == COL_START:
            # Unverified times are kept out of the absolute order: after the
            # rest (docs/design/explorer.md).
            return (0 if details.times_verified else 1, details.start or 0, entry.name)
        if col == COL_END:
            return (0 if details.times_verified else 1, details.end or 0, entry.name)
        if col == COL_DURATION:
            return (0, details.duration_seconds or 0.0)
        if col == COL_CHANNELS:
            return (0, len(details.channels))
        if col == COL_RATES:
            return (0, details.sampling_rates)
        return missing

    # -- changing the list ------------------------------------------------
    def entry_for(self, key: str) -> RecordingEntry | None:
        row = self._rows.get(key)
        return self.entries[row] if row is not None else None

    def row_of(self, entry: RecordingEntry) -> int:
        return self._rows.get(entry.key, -1)

    def add(self, entries: list[RecordingEntry]) -> None:
        new = [e for e in entries if e.key not in self._rows]
        if not new:
            return
        first = len(self.entries)
        self.beginInsertRows(QModelIndex(), first, first + len(new) - 1)
        for entry in new:
            self._rows[entry.key] = len(self.entries)
            self.entries.append(entry)
        self.endInsertRows()

    def remove(self, entries: list[RecordingEntry]) -> None:
        doomed = {e.key for e in entries}
        if not doomed:
            return
        self.beginResetModel()
        self.entries = [e for e in self.entries if e.key not in doomed]
        self._rows = {e.key: i for i, e in enumerate(self.entries)}
        self.endResetModel()

    def refresh(self, entry: RecordingEntry) -> None:
        row = self.row_of(entry)
        if row >= 0:
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def set_zone(self, zone: tzinfo) -> None:
        self.zone = zone
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, len(COLUMNS) - 1)
        if self.entries:
            self.dataChanged.emit(
                self.index(0, 0), self.index(len(self.entries) - 1, len(COLUMNS) - 1)
            )


class RecordingSortProxy(QSortFilterProxyModel):
    """Sorts by :meth:`RecordingTableModel.sort_key`, not by the text."""

    def lessThan(self, left, right):  # noqa: N802
        model = self.sourceModel()
        try:
            return model.sort_key(left.row(), left.column()) < model.sort_key(
                right.row(), right.column()
            )
        except TypeError:
            return str(model.sort_key(left.row(), left.column())) < str(
                model.sort_key(right.row(), right.column())
            )
