"""The detail panels for the selected recording: overview, channels, timeline,
notes and events, patient details and the check for problems.

Every panel is filled from data already read (``RecordingEntry``); none of
them reads a file. Patient details, descriptions and the text of notes are
shown only after "Show" for that recording; until then they read "Hidden",
which says nothing about their length.
"""

from __future__ import annotations

from datetime import tzinfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..common.dialogs import TechnicalDetails
from ..common.pages import scaled_font
from ..common.text import technical
from ..common.theme import Theme
from ..model.recording import ACCESS_BASIC, ACCESS_FULL, note_kind_label, operation_words
from ..model.summary import DATE_OFFSET_NOTE, UNVERIFIED_NOTE
from ..model.timefmt import (
    format_count,
    format_rate,
    format_rates,
    format_size,
    format_span,
    format_time,
    zone_label,
)
from .state import RecordingEntry
from .timeline import TimelineWidget

HIDDEN = "Hidden"
LOCKED = "Locked: needs the full-access password"

DATE_OFFSET_DETAIL = (
    "It uses an older way of masking dates: a time offset stored inside the "
    "recording. The real date can be worked out from the file again, so this is not "
    "an anonymized recording. The times shown here are the true ones."
)
UNVERIFIED_DETAIL = (
    "Times marked unverified are kept after the others when the list is sorted by "
    "time. Open the recording with the full-access password to confirm them."
)
BASIC_ACCESS_NOTE = (
    "Opened with the basic password. The patient details and some settings stay "
    "locked; everything else is shown."
)
DECLARATIONS_NOTE = (
    "Some other programs may have trouble opening this recording. This app reads it "
    "correctly. Use Check for problems to see why."
)
NOTE_LEVELS_NOTE = (
    "Notes and events stored for the whole recording, with each channel, and with "
    "each part of a channel."
)


def _scroll(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setObjectName("Panel")
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(inner)
    return area


def _notice(text: str, warning: bool = False, detail: str = "") -> QFrame:
    frame = QFrame()
    frame.setObjectName("Warning" if warning else "Notice")
    layout = QVBoxLayout(frame)
    em = frame.fontMetrics().height()
    layout.setContentsMargins(int(em * 0.7), int(em * 0.5), int(em * 0.7), int(em * 0.5))
    label = QLabel(text, frame)
    label.setWordWrap(True)
    if warning:
        label.setFont(scaled_font(frame, 1.0, bold=True))
    layout.addWidget(label)
    if detail:
        more = QLabel(detail, frame)
        more.setWordWrap(True)
        layout.addWidget(more)
    return frame


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


class _SortItem(QTableWidgetItem):
    """A table cell that sorts by a value, not by its text."""

    def __init__(self, text: str, key):
        super().__init__(text)
        self.key = key
        self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def __lt__(self, other):
        try:
            return self.key < other.key
        except TypeError:
            return str(self.key) < str(other.key)


def _table(columns: tuple[str, ...]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(list(columns))
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.verticalHeader().hide()
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------


class OverviewPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        inner = QWidget()
        self.notices = QVBoxLayout()
        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        box = QVBoxLayout(inner)
        box.addLayout(self.notices)
        box.addLayout(self.form)
        box.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(inner))
        self.values: dict[str, str] = {}

    def _row(self, label: str, value: str) -> None:
        field = QLabel(value)
        field.setWordWrap(True)
        field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.form.addRow(f"{label}:", field)
        self.values[label] = value

    def show_entry(self, entry: RecordingEntry, zone: tzinfo) -> None:
        _clear(self.notices)
        while self.form.rowCount():
            self.form.removeRow(0)
        self.values = {}
        details = entry.details
        if details is None:
            return
        if details.uses_time_offset:
            self.notices.addWidget(_notice(DATE_OFFSET_NOTE, True, DATE_OFFSET_DETAIL))
        if not details.times_verified:
            self.notices.addWidget(_notice(UNVERIFIED_NOTE, False, UNVERIFIED_DETAIL))
        if details.access == ACCESS_BASIC:
            self.notices.addWidget(_notice(BASIC_ACCESS_NOTE))
        if details.declarations_incomplete:
            self.notices.addWidget(_notice(DECLARATIONS_NOTE))

        mark = "" if details.times_verified else " (unverified)"
        self._row("Recording", entry.name)
        self._row(
            "Kind",
            "Packed into one file" if entry.outcome.packed else "Folder",
        )
        self._row("Start", format_time(details.start, zone) + mark)
        self._row("End", format_time(details.end, zone) + mark)
        self._row("Times shown in", zone_label(zone))
        self._row("Duration", format_span(details.duration_seconds))
        self._row("Channels", str(len(details.channels)))
        self._row("Sampling rate", format_rates(details.sampling_rates))
        self._row("Parts", self._parts_text(details))
        self._row("Gaps", self._gaps_text(details))
        self._row("Size on disk", format_size(entry.outcome.size_bytes) or "Unknown")
        self._row("Password", self._access_text(details.access))
        self._row(
            "Notes and events",
            "Could not be read"
            if details.notes_problem is not None
            else format_count(len(details.notes), "item"),
        )

    @staticmethod
    def _parts_text(details) -> str:
        counts = {len(c.parts) for c in details.channels}
        if len(counts) <= 1:
            return format_count(details.part_count, "part")
        return f"{min(counts)} to {max(counts)} per channel"

    @staticmethod
    def _gaps_text(details) -> str:
        gaps = details.gaps()
        if gaps:
            seconds = [(b - a) / 1e6 for a, b in gaps]
            text = (
                f"{format_count(len(gaps), 'gap')}, {format_span(sum(seconds))} in total, "
                f"the longest {format_span(max(seconds))}"
            )
        else:
            text = "None between parts"
        inside = details.parts_with_gaps_inside
        if inside:
            text += (
                f". About {format_span(details.missing_inside_seconds)} missing inside "
                f"{format_count(inside, 'part')}"
            )
        return text

    @staticmethod
    def _access_text(access: str) -> str:
        if access == ACCESS_FULL:
            return "Yes. Opened with the full-access password"
        if access == ACCESS_BASIC:
            return "Yes. Opened with the basic password"
        return "No"


# --------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------


class ChannelsPanel(QWidget):
    COLUMNS = (
        "Channel",
        "Sampling rate",
        "Units",
        "Samples",
        "Parts",
        "First sample",
        "Last sample",
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.table = _table(self.COLUMNS)
        self.table.setSortingEnabled(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self.table)

    def show_entry(self, entry: RecordingEntry, zone: tzinfo) -> None:
        details = entry.details
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        if details is None:
            return
        self.table.setRowCount(len(details.channels))
        for row, c in enumerate(details.channels):
            cells = (
                _SortItem(c.name, c.name.casefold()),
                _SortItem(format_rate(c.sampling_frequency), c.sampling_frequency),
                _SortItem(c.units, c.units),
                _SortItem(f"{c.samples:,}", c.samples),
                _SortItem(str(len(c.parts)), len(c.parts)),
                _SortItem(format_time(c.start, zone), c.start),
                _SortItem(format_time(c.end, zone), c.end),
            )
            for col, item in enumerate(cells):
                if col in (1, 3, 4):
                    item.setTextAlignment(
                        int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    )
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)


# --------------------------------------------------------------------------
# Timeline
# --------------------------------------------------------------------------


class TimelinePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.caption = QLabel(
            "Each bar is a channel. Filled: recorded. Empty: a gap. Striped: a part "
            "with gaps inside it. Drawn from the recording's details; no signal is read. "
            "Point at a part to see its times.",
            self,
        )
        self.caption.setObjectName("Muted")
        self.caption.setWordWrap(True)
        self.timeline = TimelineWidget()
        layout = QVBoxLayout(self)
        layout.addWidget(self.caption)
        layout.addWidget(_scroll(self.timeline), 1)

    def show_entry(self, entry: RecordingEntry, zone: tzinfo) -> None:
        self.timeline.set_details(entry.details, zone)

    def set_theme(self, theme: Theme) -> None:
        self.timeline.set_theme(theme)


# --------------------------------------------------------------------------
# Notes and events
# --------------------------------------------------------------------------


class NotesPanel(QWidget):
    COLUMNS = ("Time", "Stored with", "Kind", "Text", "Length")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.levels = QLabel(NOTE_LEVELS_NOTE, self)
        self.levels.setObjectName("Muted")
        self.levels.setWordWrap(True)
        self.hidden_note = QLabel(
            "The text of notes can name people, so it is hidden until you choose Show "
            "under Patient details.",
            self,
        )
        self.hidden_note.setObjectName("Muted")
        self.hidden_note.setWordWrap(True)
        self.empty = QLabel("This recording has no notes or events.")
        self.empty.setObjectName("Muted")
        self.empty.setWordWrap(True)
        self.table = _table(self.COLUMNS)
        self.table.setSortingEnabled(True)
        self.problem_slot = QVBoxLayout()
        layout = QVBoxLayout(self)
        layout.addWidget(self.levels)
        layout.addLayout(self.problem_slot)
        layout.addWidget(self.hidden_note)
        layout.addWidget(self.empty)
        layout.addWidget(self.table, 1)

    def show_entry(self, entry: RecordingEntry, zone: tzinfo) -> None:
        details = entry.details
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        notes = details.notes if details is not None else ()
        _clear(self.problem_slot)
        problem = details.notes_problem if details is not None else None
        if problem is not None:
            self.problem_slot.addWidget(
                _notice(
                    "The notes and events could not be read. " + problem.text,
                    warning=True,
                )
            )
            if problem.detail:
                self.problem_slot.addWidget(TechnicalDetails(problem.detail))
        self.empty.setVisible(details is not None and not notes and problem is None)
        self.table.setVisible(bool(notes))
        self.hidden_note.setVisible(bool(notes) and not entry.revealed)
        self.table.setRowCount(len(notes))
        for row, note in enumerate(notes):
            text = note.text if entry.revealed else (HIDDEN if note.text else "")
            length = format_span(note.duration / 1e6) if note.duration else ""
            cells = (
                _SortItem(format_time(note.time, zone), note.time),
                _SortItem(note.where, (note.channel or "", note.segment or -1)),
                _SortItem(note_kind_label(note.kind), note.kind),
                _SortItem(text, text),
                _SortItem(length, note.duration or 0),
            )
            for col, item in enumerate(cells):
                self.table.setItem(row, col, item)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0)


# --------------------------------------------------------------------------
# Patient details
# --------------------------------------------------------------------------


class PatientPanel(QWidget):
    reveal_changed = Signal(bool)

    FIELDS = (
        ("First name", "first_name", True),
        ("Last name", "last_name", True),
        ("Patient ID", "patient_id", True),
        ("Location", "location", True),
        ("Recording description", "recording_description", False),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        inner = QWidget()
        self.intro = QLabel(
            "Patient details are hidden until you choose Show. Showing them affects only "
            "this recording, only while the app is open. They never go into the app's "
            "log, and Copy summary keeps them hidden unless they are shown here.",
            inner,
        )
        self.intro.setObjectName("Muted")
        self.intro.setWordWrap(True)
        self.button = QPushButton("Show", inner)
        self.button.setCheckable(True)
        self.button.toggled.connect(self._toggled)
        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        row = QHBoxLayout()
        row.addWidget(self.button)
        row.addStretch(1)
        box = QVBoxLayout(inner)
        box.addWidget(self.intro)
        box.addLayout(row)
        box.addLayout(self.form)
        box.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(inner))
        self.values: dict[str, str] = {}
        self._updating = False

    def _toggled(self, shown: bool) -> None:
        self.button.setText("Hide" if shown else "Show")
        if not self._updating:
            self.reveal_changed.emit(shown)

    def show_entry(self, entry: RecordingEntry, zone: tzinfo) -> None:  # noqa: ARG002
        self._updating = True
        self.button.setChecked(entry.revealed)
        self.button.setText("Hide" if entry.revealed else "Show")
        self._updating = False
        while self.form.rowCount():
            self.form.removeRow(0)
        self.values = {}
        details = entry.details
        if details is None:
            self.button.setEnabled(False)
            return
        self.button.setEnabled(True)
        patient = details.patient
        for label, attr, needs_full in self.FIELDS:
            if needs_full and not patient.names_available:
                text = LOCKED
            elif not entry.revealed:
                text = HIDDEN
            else:
                text = getattr(patient, attr) or "(empty)"
            self._add(label, text)
        if entry.revealed:
            described = [c for c in details.channels if c.description]
            text = "; ".join(f"{c.name}: {c.description}" for c in described) or "(empty)"
        else:
            text = HIDDEN
        self._add("Channel descriptions", text)

    def _add(self, label: str, text: str) -> None:
        field = QLabel(text)
        field.setWordWrap(True)
        field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if text in (HIDDEN, LOCKED):
            field.setObjectName("Muted")
        self.form.addRow(f"{label}:", field)
        self.values[label] = text


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


class HistoryPanel(QWidget):
    """What the mef3io library recorded about creating and changing the
    files: versions and kinds of change, never times."""

    def __init__(self, parent=None):
        super().__init__(parent)
        inner = QWidget()
        intro = QLabel(
            "What has been done to this recording, as the mef3io library recorded it in "
            "the files: which version created each part, which version last changed it, "
            "and what was done. No dates are recorded. Programs other than mef3io leave "
            "this blank.",
            inner,
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        self.groups = QVBoxLayout()
        box = QVBoxLayout(inner)
        box.addWidget(intro)
        box.addLayout(self.groups)
        box.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(inner))
        self.texts: list[str] = []

    def show_entry(self, entry: RecordingEntry, zone: tzinfo) -> None:  # noqa: ARG002
        _clear(self.groups)
        self.texts = []
        details = entry.details
        if details is None:
            return
        for history, parts in details.history_groups():
            count = format_count(parts, "part")
            if history is None:
                text = f"{count}: written by another program. No history is recorded in them."
                self._add(text, "")
                continue
            done = ", ".join(operation_words(op) for op in history.operations) or "unknown"
            heading = f"{count}: created by mef3io {history.created_by or '(unknown version)'}"
            lines = [f"Last changed by mef3io {history.last_modified_by or '(unknown version)'}."]
            lines.append(f"What was done: {done}.")
            if history.modification_count:
                lines.append(
                    f"Changed {format_count(history.modification_count, 'time')} since it "
                    "was created."
                )
            self._add(heading, " ".join(lines))

    def _add(self, heading: str, detail: str) -> None:
        self.groups.addWidget(_notice(heading, detail=detail))
        self.texts.append(f"{heading} {detail}".strip())


# --------------------------------------------------------------------------
# Check for problems
# --------------------------------------------------------------------------


class CheckPanel(QWidget):
    check_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        inner = QWidget()
        intro = QLabel(
            "Compares what the recording says about itself with its data, using the "
            "mef3io library's checks. It reads the recording's details and the list of "
            "where its signal is stored, and never changes the recording. On a large "
            "recording it can take a while; you can stop it at any time.",
            inner,
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        self.button = QPushButton("Check now", inner)
        self.button.setProperty("primary", True)
        self.button.clicked.connect(self.check_requested)
        self.progress_slot = QVBoxLayout()
        self.headline = QLabel("", inner)
        self.headline.setFont(scaled_font(self, 1.15, bold=True))
        self.headline.setWordWrap(True)
        self.results = QVBoxLayout()
        row = QHBoxLayout()
        row.addWidget(self.button)
        row.addStretch(1)
        box = QVBoxLayout(inner)
        box.addWidget(intro)
        box.addLayout(row)
        box.addLayout(self.progress_slot)
        box.addWidget(self.headline)
        box.addLayout(self.results)
        box.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(inner))
        self.progress_widget = None

    def set_progress(self, widget: QWidget | None) -> None:
        """Show a running check's progress (the page owns the widget)."""
        if self.progress_widget is widget:
            return
        if self.progress_widget is not None:
            self.progress_slot.removeWidget(self.progress_widget)
            self.progress_widget.hide()
        self.progress_widget = widget
        if widget is not None:
            self.progress_slot.addWidget(widget)
            widget.show()

    def show_entry(self, entry: RecordingEntry, zone: tzinfo) -> None:  # noqa: ARG002
        _clear(self.results)
        self.button.setEnabled(not entry.busy and not entry.needs_password)
        report = entry.check
        if entry.check_error is not None and report is None:
            self.headline.setText(entry.check_error.title)
            text = QLabel(entry.check_error.text)
            text.setWordWrap(True)
            self.results.addWidget(text)
            if entry.check_error.detail:
                self.results.addWidget(TechnicalDetails(entry.check_error.detail))
            return
        if report is None:
            self.headline.setText("" if entry.busy else "Not checked yet")
            return
        self.headline.setText(report.headline())
        if report.parts_skipped:
            self.results.addWidget(
                _notice(
                    f"{format_count(report.parts_skipped, 'part')} could not be checked "
                    "fully, because their details look damaged. Keep a copy of the "
                    "original as it is."
                )
            )
        for group in report.groups:
            frame = _notice(
                f"{group.severity_word}: {group.title}",
                warning=group.severity == "error",
                detail=f"{group.explanation} Found in {format_count(group.parts, 'part')}.",
            )
            self.results.addWidget(frame)
        if report.ok and not report.groups:
            self.results.addWidget(
                QLabel(
                    f"Everything the recording says about itself matches its data "
                    f"({format_count(report.parts_checked, 'part')} checked)."
                )
            )
        self.results.addWidget(TechnicalDetails(technical(report.technical)))


__all__ = [
    "CheckPanel",
    "ChannelsPanel",
    "HistoryPanel",
    "NotesPanel",
    "OverviewPanel",
    "PatientPanel",
    "TimelinePanel",
]
