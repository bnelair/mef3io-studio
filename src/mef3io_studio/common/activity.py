"""The activity panel and the status-bar indicator: every running and
finished background job, with progress, time left, result and Cancel.

The table is a ``QAbstractTableModel`` + ``QTableView``: only visible rows are
painted, so hundreds of jobs cost nothing.
"""

from __future__ import annotations

import time

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QStackedLayout,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..model.jobs import JobRecord, JobStatus
from ..model.progress import format_duration
from ..workers.jobs import JobHandle, JobManager
from . import icons
from .theme import LIGHT, Theme

COLUMNS = ("Task", "Progress", "Time left", "Result")
RecordRole = Qt.ItemDataRole.UserRole + 1


def progress_text(record: JobRecord) -> str:
    p = record.progress
    phase = p.phase or record.status.value
    if record.status.finished:
        return record.status.value
    if p.determinate:
        return f"{phase} — {round(100 * (p.fraction or 0))}%"
    return phase


class JobTableModel(QAbstractTableModel):
    def __init__(self, manager: JobManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self._handles: list[JobHandle] = list(manager.handles)
        manager.job_added.connect(self._on_added)
        manager.job_changed.connect(self._on_changed)

    # -- Qt model API ---------------------------------------------------
    def rowCount(self, parent=QModelIndex()):  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._handles)

    def columnCount(self, parent=QModelIndex()):  # noqa: B008, N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        record = self._handles[index.row()].record
        col = index.column()
        if role == RecordRole:
            return record
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return record.title
            if col == 1:
                return progress_text(record)
            if col == 2:
                return format_duration(record.eta_seconds)
            if col == 3:
                return record.result_text if record.status.finished else ""
        if role == Qt.ItemDataRole.ToolTipRole and col == 3 and record.status is JobStatus.FAILED:
            handle = self._handles[index.row()]
            return handle.error.text if handle.error else None
        return None

    # -- helpers ----------------------------------------------------------
    def handle_at(self, row: int) -> JobHandle:
        return self._handles[row]

    def _on_added(self, handle: JobHandle) -> None:
        row = len(self._handles)
        self.beginInsertRows(QModelIndex(), row, row)
        self._handles.append(handle)
        self.endInsertRows()

    def _on_changed(self, handle: JobHandle) -> None:
        try:
            row = self._handles.index(handle)
        except ValueError:
            return
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def clear_finished(self) -> None:
        self.beginResetModel()
        self._manager.clear_finished()
        self._handles = list(self._manager.handles)
        self.endResetModel()

    def has_indeterminate_running(self) -> bool:
        return any(
            h.record.status is JobStatus.RUNNING and not h.record.progress.determinate
            for h in self._handles
        )


class ProgressDelegate(QStyledItemDelegate):
    """Draws a slim rounded bar under the phase text. Unknown totals get a
    moving segment (driven by the panel's animation timer)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme: Theme = LIGHT

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        record: JobRecord = index.data(RecordRole)
        super().paint(painter, option, index)
        if record is None or record.status.finished:
            return
        rect = option.rect.adjusted(8, option.rect.height() - 9, -8, -4)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QPainterPath()
        track.addRoundedRect(QRectF(rect), 2.5, 2.5)
        painter.fillPath(
            track, QColor(self.theme.surface_alt if not self.theme.dark else "#2a2f37")
        )
        p = record.progress
        if p.determinate:
            chunk = QRectF(rect)
            chunk.setWidth(max(4.0, rect.width() * (p.fraction or 0)))
        else:
            seg = rect.width() * 0.3
            phase = (time.monotonic() * 0.8) % 1.0
            x = rect.left() - seg + (rect.width() + seg) * phase
            chunk = QRectF(max(rect.left(), x), rect.top(), seg, rect.height())
            chunk.setRight(min(chunk.right(), rect.right()))
        path = QPainterPath()
        path.addRoundedRect(chunk, 2.5, 2.5)
        painter.fillPath(path, QColor(self.theme.accent))
        painter.restore()


class ActivityPanel(QWidget):
    """Lists every job of this session. Select one and press Cancel to stop it."""

    def __init__(self, manager: JobManager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.model = JobTableModel(manager, self)
        self.delegate = ProgressDelegate(self)

        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(1, self.delegate)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        row_height = self.fontMetrics().height() + 18
        self.table.verticalHeader().setDefaultSectionSize(row_height)

        self.empty = QLabel("Nothing has run yet. Long tasks appear here while they work.", self)
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)

        self.cancel_button = QPushButton("Cancel task", self)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_selected)
        self.clear_button = QPushButton("Clear finished", self)
        self.clear_button.clicked.connect(self.clear_finished)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.cancel_button)

        self._stack = QStackedLayout()
        self._stack.addWidget(self.empty)
        self._stack.addWidget(self.table)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 10)
        layout.addLayout(self._stack, 1)
        layout.addLayout(buttons)

        self._anim = QTimer(self)
        self._anim.setInterval(33)
        self._anim.timeout.connect(self._tick)

        self.table.selectionModel().selectionChanged.connect(self._update_buttons)
        manager.job_added.connect(self._on_jobs_changed)
        manager.job_changed.connect(self._on_jobs_changed)
        self._on_jobs_changed()

    def set_theme(self, theme: Theme) -> None:
        self.delegate.theme = theme
        self.cancel_button.setIcon(icons.icon("cancel", theme.text, theme.muted))
        self.clear_button.setIcon(icons.icon("clear", theme.text, theme.muted))
        self.table.viewport().update()

    def selected_handle(self) -> JobHandle | None:
        rows = self.table.selectionModel().selectedRows()
        return self.model.handle_at(rows[0].row()) if rows else None

    def cancel_selected(self) -> None:
        handle = self.selected_handle()
        if handle is not None:
            handle.cancel()
        self._update_buttons()

    def clear_finished(self) -> None:
        self.model.clear_finished()
        self._on_jobs_changed()

    def _on_jobs_changed(self, *_args) -> None:
        self._stack.setCurrentWidget(self.table if self.model.rowCount() else self.empty)
        if self.model.has_indeterminate_running():
            if not self._anim.isActive():
                self._anim.start()
        else:
            self._anim.stop()
        self._update_buttons()

    def _update_buttons(self, *_args) -> None:
        handle = self.selected_handle()
        self.cancel_button.setEnabled(
            handle is not None and not handle.is_finished and not handle.record.cancel_requested
        )
        self.clear_button.setEnabled(any(h.is_finished for h in self.manager.handles))

    def _tick(self) -> None:
        if self.isVisible():
            first = self.model.index(0, 1)
            last = self.model.index(self.model.rowCount() - 1, 1)
            self.model.dataChanged.emit(first, last, [Qt.ItemDataRole.DisplayRole])


class ActivityIndicator(QWidget):
    """Status-bar summary: "2 tasks running" and an overall bar. Clicking it
    shows the activity panel."""

    clicked = Signal()

    def __init__(self, manager: JobManager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.button = QToolButton(self)
        self.button.setObjectName("StatusButton")
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.clicked.connect(self.clicked)
        self.button.setToolTip("Show or hide the list of tasks")
        self.bar = QProgressBar(self)
        self.bar.setTextVisible(False)
        self.bar.setFixedWidth(self.fontMetrics().horizontalAdvance("M") * 10)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.addWidget(self.bar)
        layout.addWidget(self.button)
        manager.running_changed.connect(self.refresh)
        manager.job_changed.connect(self.refresh)
        self.refresh()

    def set_theme(self, theme: Theme) -> None:
        self.button.setIcon(icons.icon("activity", theme.muted))

    def refresh(self, *_args) -> None:
        running = [h for h in self.manager.handles if not h.is_finished]
        if not running:
            self.button.setText("No tasks running")
            self.bar.hide()
            return
        n = len(running)
        self.button.setText(f"{n} task{'s' if n != 1 else ''} running")
        self.bar.show()
        fractions = [h.record.progress.fraction for h in running]
        if any(f is None for f in fractions):
            self.bar.setRange(0, 0)  # busy
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(round(1000 * sum(fractions) / n))


class JobProgressWidget(QWidget):
    """One job's progress, for embedding in a dialog or wizard step: phase
    label, bar (determinate when the total is known), time left, Cancel."""

    def __init__(self, handle: JobHandle, parent=None):
        super().__init__(parent)
        self.handle = handle
        self.label = QLabel(handle.record.title, self)
        self.eta = QLabel("", self)
        self.eta.setObjectName("Muted")
        self.bar = QProgressBar(self)
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 0)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.clicked.connect(handle.cancel)
        top = QHBoxLayout()
        top.addWidget(self.label, 1)
        top.addWidget(self.eta)
        row = QHBoxLayout()
        row.addWidget(self.bar, 1)
        row.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addLayout(row)
        handle.changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        record = self.handle.record
        self.label.setText(
            progress_text(record) if not record.status.finished else record.result_text
        )
        self.eta.setText(format_duration(record.eta_seconds))
        if record.status.finished:
            self.bar.setRange(0, 1)
            self.bar.setValue(1)
            self.cancel_button.setEnabled(False)
        elif record.progress.determinate:
            self.bar.setRange(0, 1000)
            self.bar.setValue(round(1000 * (record.progress.fraction or 0)))
        else:
            self.bar.setRange(0, 0)
        if record.cancel_requested:
            self.cancel_button.setEnabled(False)


__all__ = [
    "ActivityIndicator",
    "ActivityPanel",
    "JobProgressWidget",
    "JobTableModel",
]
