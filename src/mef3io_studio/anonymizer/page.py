"""The Anonymize page: a step-by-step wizard that makes anonymized copies.

Steps (mef3io-studio#5, docs/design/anonymizer.md): recordings, channels,
dates, identity, passwords, review, run; then the result. Back and Next move
between them, and NOTHING is written before "Make the copies" on the review
step. Every copy is made by mef3io in a helper process (``workers.tasks``),
with real progress and Stop; a stopped or failed copy leaves nothing behind,
and a batch goes on after one recording fails.

The recordings come from the Explore list (what the Explorer already read:
no recording is opened twice). The time shift is planned by
``model.shiftplan``; the channel selection by ``model.channels``. The widgets
never import mef3io.

Privacy: original dates and names are shown on screen (the user is looking
at their own recordings) but never logged; the date key, which maps the
copies back to the originals, is shown at the end and saved only when the
user asks, to a file the user chooses.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field, replace
from datetime import UTC, date, time, tzinfo

from PySide6.QtCore import QAbstractTableModel, QDate, QModelIndex, Qt, QTime, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QStackedWidget,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..common.activity import JobProgressWidget
from ..common.dialogs import TechnicalDetails
from ..common.flow import FlowLayout
from ..common.pages import scaled_font
from ..common.text import technical
from ..common.theme import Theme
from ..explorer.state import RecordingEntry
from ..model.anonymize import (
    CLEAR_DESCRIPTIONS,
    CLEAR_LOCATION,
    CLEAR_PATIENT,
    TEXT_DROP,
    TEXT_KEEP,
    CopyRequest,
    CopyResult,
    DateKeyRow,
    DestinationReview,
    Leftover,
    blocker_words,
    date_key_too_close,
    destination_for,
    estimate_bytes,
    inside_any_recording,
    library_supports_anonymize,
    name_problem,
    password_may_not_work_elsewhere,
    review_destination,
    shift_words,
    write_date_key,
)
from ..model.channels import ChannelRow, ChannelSelection
from ..model.recording import ACCESS_BASIC
from ..model.shiftplan import (
    Blocker,
    Mode,
    RefusalCode,
    ShiftPlan,
    ShiftSettings,
    TimeOfDay,
    WarningCode,
    plan_shifts,
    recording_times,
)
from ..model.timefmt import (
    format_count,
    format_rate,
    format_size,
    format_time,
    resolve_zone,
    zone_names,
)
from ..workers.jobs import JobManager
from ..workers.tasks import anonymize_recordings, check_copy_names
from .picker import ChannelPicker

#: Wait this long after the last change of the Explore list before listing
#: the recordings again (a scan delivers them in quick bursts).
REFRESH_DELAY_MS = 150

STEPS = (
    "Recordings",
    "Channels",
    "Dates",
    "Identity",
    "Passwords",
    "Review",
    "Make the copies",
)
(
    STEP_RECORDINGS,
    STEP_CHANNELS,
    STEP_DATES,
    STEP_IDENTITY,
    STEP_PASSWORDS,
    STEP_REVIEW,
    STEP_RUN,
) = range(len(STEPS))

NOT_AVAILABLE = (
    "This version of the app cannot make anonymized copies yet: the mef3io library it "
    "uses does not have the anonymizer. Nothing is written. Install a newer test version "
    "of the app."
)

KEEP_TEXT_WARNING = (
    "Keeping the text of notes and events: the patient's names, ID and location, the "
    "recording's old name and the names of channels you leave out are taken out, but "
    "dates, ages, other people's names and anything else written in free text are NOT "
    "found. Read the text yourself before sharing the copies, or choose Remove."
)

PASSWORD_EXPLANATION = (
    "A password-protected copy needs two passwords. The basic password opens the "
    "recording settings and descriptions; the full-access password also opens the "
    "patient details and the text of notes and events. Passwords do not hide the "
    "signal, the recording's name or its timing: anyone with the files can still read "
    "those. There is no way to get a forgotten password back, and the app never "
    "stores passwords. The original's passwords are never carried over."
)

DATE_KEY_WARNING = (
    "The date key links each copy back to its original recording and its real dates: "
    "with it, anyone can undo the anonymization. It is shown here only, never kept by "
    "the app and never written into the copies. Save it only if you need it, and store "
    "it like the original recordings, never next to the copies."
)


def _label(text: str = "", muted: bool = False, wrap: bool = True, bold: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(wrap)
    if muted:
        label.setObjectName("Muted")
    if bold:
        label.setFont(scaled_font(label, 1.0, bold=True))
    return label


def _scroll(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setObjectName("Panel")
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(inner)
    return area


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


def _table(columns: tuple[str, ...]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(list(columns))
    table.verticalHeader().hide()
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(True)
    return table


def _item(text: str, editable: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
    if editable:
        flags |= Qt.ItemFlag.ItemIsEditable
    item.setFlags(flags)
    return item


def refusal_words(plan: ShiftPlan, names: dict[str, str]) -> list[str]:
    """Why the planned dates cannot be used, in plain words."""
    out = []
    for r in plan.refusals:
        who = names.get(r.recording_id or "", "")
        prefix = f"{who}: " if who else ""
        if r.code is RefusalCode.NEEDS_SITE_ZONE:
            out.append(
                "To keep the local time of day, choose the time zone where the recordings "
                "were made (or keep the UTC time of day instead)."
            )
        elif r.code is RefusalCode.UNKNOWN_ZONE:
            out.append("That time zone is not known. Choose one from the list.")
        elif r.code is RefusalCode.NEW_DATE_OUT_OF_RANGE:
            out.append("Choose a new date between 1970 and 9999.")
        elif r.code is RefusalCode.BELOW_FLOOR:
            days = -(-(r.limit_us or 0) // 86_400_000_000)
            out.append(
                f"{prefix}The new date is too close to the real dates. The dates must move by "
                f"at least {days:,} days (the time the recordings span, plus one day), so that "
                "no moved date can be mistaken for a real one."
            )
        elif r.code is RefusalCode.TOO_EARLY:
            out.append(f"{prefix}The new date is too early: moved dates must stay after 1970.")
        elif r.code is RefusalCode.TOO_LATE:
            out.append(f"{prefix}The new date is too late for this recording.")
        elif r.code is RefusalCode.REFERENCE_BLOCKED:
            out.append("The chosen reference recording cannot be anonymized. Choose another.")
        elif r.code is RefusalCode.NOTHING_TO_PLAN:
            out.append("None of the chosen recordings can be anonymized.")
    return list(dict.fromkeys(out))


def warning_words(plan: ShiftPlan, recording_id: str, names: dict[str, str]) -> list[str]:
    planned = plan.by_id.get(recording_id)
    if planned is None:
        return []
    out = []
    for w in planned.warnings:
        if w.code is WarningCode.ALIGNED_OVERLAP:
            others = ", ".join(names.get(o, "") for o in w.overlaps)
            out.append(f"Overlaps in time with: {others}.")
        elif w.code is WarningCode.DST_CROSSING:
            out.append(
                "Crosses a daylight-saving change. Elapsed time stays exact, so after the "
                "change the copy's times are an hour away from what the wall clock read."
            )
        elif w.code is WarningCode.OTHER_SEASON:
            hours = abs(w.time_of_day_off_us) / 3_600_000_000
            text = (
                f"Made in the other daylight-saving season: its time of day in the copy is "
                f"{hours:g} hour{'s' if hours != 1 else ''} off."
            )
            if w.weekday_flips:
                text += " That moves it across midnight, so its date and weekday change too."
            out.append(text)
    return out


BLOCKER_PLAN_WORDS = {
    Blocker.LOCKED: "It needs its full-access password. Open it again in Explore with it.",
    Blocker.TIMES_UNVERIFIED: (
        "Its times cannot be read without doubt (an older way of hiding dates is set in a "
        "way that cannot be undone safely)."
    ),
    Blocker.TIMES_INVALID: "Its start and end times look damaged.",
}


@dataclass
class Choice:
    """One recording in this run."""

    entry: RecordingEntry
    name: str = ""
    pseudonym: str = ""
    names_message: object = None  # UserMessage from the name check, or None

    @property
    def key(self) -> str:
        return self.entry.key


@dataclass
class RunState:
    requests: list[CopyRequest] = field(default_factory=list)
    results: dict[str, CopyResult] = field(default_factory=dict)
    #: Unfinished copies that could not be removed after a stop or a crash.
    leftovers: list = field(default_factory=list)
    finished: bool = False
    stopped: bool = False


# --------------------------------------------------------------------------
# The steps


class RecordingsModel(QAbstractTableModel):
    """The Explore list as the Recordings step shows it. Texts are worked
    out when a row is painted, and why a recording cannot be anonymized is
    cached per recording, so a list of thousands rebuilds in a moment."""

    COLUMNS = ("Recording", "Channels", "Start", "Can it be anonymized?")

    def __init__(self, step: RecordingsStep):
        super().__init__(step)
        self.step = step
        self.rows: list[RecordingEntry] = []

    def rowCount(self, parent=QModelIndex()):  # noqa: B008, N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: B008, N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        entry = self.rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            kept = entry.key in self.step.ticked
            return Qt.CheckState.Checked if kept else Qt.CheckState.Unchecked
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        details = entry.details
        if col == 0:
            return entry.name
        if col == 1:
            return str(len(details.channels)) if details else ""
        if col == 2:
            return (
                format_time(details.start, self.step.page.zone) if details and details.start else ""
            )
        return self.step.reason(entry) or "Yes"

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsSelectable
        if self.step.reason(self.rows[index.row()]) is None:
            flags |= Qt.ItemFlag.ItemIsEnabled
            if index.column() == 0:
                flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):  # noqa: N802
        if role != Qt.ItemDataRole.CheckStateRole or not index.isValid():
            return False
        entry = self.rows[index.row()]
        state = Qt.CheckState(value) if not isinstance(value, Qt.CheckState) else value
        self.step.tick(entry.key, state == Qt.CheckState.Checked)
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True

    def set_rows(self, rows: list[RecordingEntry]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def text(self, row: int, col: int) -> str:
        return str(self.data(self.index(row, col)) or "")


class RecordingsStep(QWidget):
    changed = Signal()

    COLUMNS = RecordingsModel.COLUMNS

    def __init__(self, page: AnonymizerPage):
        super().__init__(page)
        self.page = page
        self.ticked: set[str] = set()
        intro = _label(
            "Choose the recordings to anonymize, from the list in Explore. Each one gets "
            "its own anonymized copy; the originals are never changed.",
            muted=True,
        )
        self.open_button = QPushButton("Open a recording…")
        self.open_button.clicked.connect(lambda: page.explorer.choose_folder_recording())
        self.open_file_button = QPushButton("Open a single-file recording…")
        self.open_file_button.clicked.connect(lambda: page.explorer.choose_file_recording())
        self.scan_button = QPushButton("Scan a folder…")
        self.scan_button.clicked.connect(lambda: page.explorer.choose_scan_folder())
        self.all_button = QPushButton("Tick all that can be anonymized")
        self.all_button.clicked.connect(self.tick_all)
        self.none_button = QPushButton("Untick all")
        self.none_button.clicked.connect(self.untick_all)
        self.model = RecordingsModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        em = self.fontMetrics().height()
        for col, width in enumerate((14, 6, 14)):
            header.resizeSection(col, int(em * width))
        self.count = _label(bold=True)
        self.empty = _label(
            "No recordings are open yet. Open or scan for recordings here or in Explore.",
            muted=True,
        )
        self.lost = _label()
        self.lost.setObjectName("Bad")
        self.lost.setVisible(False)
        row = QHBoxLayout()
        for b in (self.open_button, self.open_file_button, self.scan_button):
            row.addWidget(b)
        row.addStretch(1)
        row2 = QHBoxLayout()
        row2.addWidget(self.count, 1)
        row2.addWidget(self.all_button)
        row2.addWidget(self.none_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(intro)
        layout.addLayout(row)
        layout.addLayout(row2)
        layout.addWidget(self.lost)
        layout.addWidget(self.empty)
        layout.addWidget(self.table, 1)
        self._reasons: dict[str, tuple[tuple, str | None]] = {}

    @property
    def rows(self) -> list[RecordingEntry]:
        return self.model.rows

    def reason(self, entry: RecordingEntry) -> str | None:
        """Why this recording cannot be anonymized (plain words), or None.
        Cached per recording until what the Explorer read for it changes."""
        stamp = (id(entry.outcome), entry.busy)
        cached = self._reasons.get(entry.key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        reason = self._reason(entry)
        self._reasons[entry.key] = (stamp, reason)
        return reason

    def _reason(self, entry: RecordingEntry) -> str | None:
        details = entry.details
        if entry.busy:
            return "Still being read…"
        if details is None:
            if entry.needs_password:
                return "It needs its full-access password. Open it in Explore with it."
            return "It could not be read."
        if details.access == ACCESS_BASIC:
            return BLOCKER_PLAN_WORDS[Blocker.LOCKED]
        if details.anonymize_blockers:
            return " ".join(dict.fromkeys(blocker_words(c) for c, _ in details.anonymize_blockers))
        plan = plan_shifts(
            [recording_times(details, "x")], ShiftSettings(time_of_day=TimeOfDay.KEEP_UTC)
        )
        if plan.blocked:
            return " ".join(BLOCKER_PLAN_WORDS[b] for b in plan.blocked[0].reasons)
        return None

    def refresh(self) -> None:
        entries = self.page.explorer.entries()
        live = {e.key for e in entries}
        for key in [k for k in self._reasons if k not in live]:
            del self._reasons[key]
        # A ticked recording that can no longer be anonymized is unticked.
        self.ticked = {e.key for e in entries if e.key in self.ticked and self.reason(e) is None}
        self.model.set_rows(list(entries))
        self.empty.setVisible(not entries)
        self.table.setVisible(bool(entries))
        self._update_count()

    def tick(self, key: str, on: bool) -> None:
        if on:
            self.ticked.add(key)
        else:
            self.ticked.discard(key)
        self._update_count()
        self.changed.emit()

    def set_ticked(self, keys) -> None:
        self.page._refresh_recordings()  # a pending rebuild first
        usable = {e.key for e in self.rows if self.reason(e) is None}
        self.ticked = set(keys) & usable
        self.refresh()
        self.changed.emit()

    def tick_all(self) -> None:
        self.set_ticked(e.key for e in self.rows)

    def untick_all(self) -> None:
        self.set_ticked(())

    def chosen(self) -> list[RecordingEntry]:
        return [e for e in self.rows if e.key in self.ticked]

    def _update_count(self) -> None:
        n = len(self.ticked)
        self.count.setText(f"{format_count(n, 'recording')} chosen" if n else "None chosen yet")


class DatesStep(QWidget):
    changed = Signal()

    def __init__(self, page: AnonymizerPage):
        super().__init__(page)
        self.page = page
        self.plan: ShiftPlan | None = None
        intro = _label(
            "Every date and time in a copy is moved by the same amount, so the copy no "
            "longer says when the recording was made. The order and spacing of everything "
            "inside it stay exactly the same.",
            muted=True,
        )
        self.relative = QRadioButton("Keep the recordings' order and spacing (one shift for all)")
        self.aligned = QRadioButton("Start every copy at the same moment")
        self.relative.setChecked(True)
        mode = QButtonGroup(self)
        mode.addButton(self.relative)
        mode.addButton(self.aligned)
        self.keep_local = QRadioButton("Keep the local time of day")
        self.keep_utc = QRadioButton("Keep the UTC time of day")
        self.set_time = QRadioButton("Change the time of day too")
        self.keep_local.setChecked(True)
        tod = QButtonGroup(self)
        for b in (self.keep_local, self.keep_utc, self.set_time):
            tod.addButton(b)
        self.new_date = QDateEdit(QDate(2000, 1, 1))
        self.new_date.setCalendarPopup(True)
        self.new_date.setDisplayFormat("yyyy-MM-dd")
        self.new_time = QTimeEdit(QTime(0, 0))
        self.new_time.setDisplayFormat("HH:mm")
        self.zone = QComboBox()
        self.zone.setEditable(True)
        self.zone.addItems(zone_names())
        self.zone.setToolTip("Where the recordings were made: used to keep the local time of day")
        self.weekday = QCheckBox(
            "Keep the day of the week (the nearest date with the same weekday)"
        )
        self.reference = QComboBox()
        self.reference.setToolTip("The recording that gets the new date; the others follow it")
        form = QFormLayout()
        form.addRow("Copies:", self._stack(self.relative, self.aligned))
        form.addRow("Time of day:", self._stack(self.keep_local, self.keep_utc, self.set_time))
        form.addRow("Recording site's time zone:", self.zone)
        form.addRow("New date of the reference:", self.new_date)
        form.addRow("New time of day:", self.new_time)
        form.addRow("Reference recording:", self.reference)
        reference_help = _label(
            'The reference recording is the one that gets the new date above. In "one shift '
            'for all", every other recording moves by the same amount; in "the same '
            "moment\", every copy starts when the reference's copy starts.",
            muted=True,
        )
        form.addRow("", reference_help)
        form.addRow("", self.weekday)
        self.refusals = _label()
        self.refusals.setObjectName("Bad")
        self.preview = _table(("Recording", "Original start", "New start", "New end", "Notes"))
        self.preview.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.refusals)
        layout.addWidget(_label("Preview (new dates as the copies will show them):", bold=True))
        layout.addWidget(self.preview, 1)
        for widget in (self.relative, self.aligned, self.keep_local, self.keep_utc, self.set_time):
            widget.toggled.connect(self.recompute)
        self.weekday.toggled.connect(self.recompute)
        self.new_date.dateChanged.connect(self.recompute)
        self.new_time.timeChanged.connect(self.recompute)
        self.zone.currentTextChanged.connect(self.recompute)
        self.reference.currentIndexChanged.connect(self.recompute)

    @staticmethod
    def _stack(*widgets) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        for w in widgets:
            layout.addWidget(w)
        return box

    def set_zone_name(self, name: str) -> None:
        if name and name != "UTC":
            self.zone.setCurrentText(name)

    def set_recordings(self, choices: list[Choice]) -> None:
        current = self.reference.currentData()
        self.reference.blockSignals(True)
        self.reference.clear()
        self.reference.addItem("The earliest one", None)
        for c in choices:
            self.reference.addItem(c.entry.name, c.key)
        index = self.reference.findData(current)
        self.reference.setCurrentIndex(max(0, index))
        self.reference.blockSignals(False)
        self.recompute()

    def settings(self) -> ShiftSettings:
        if self.keep_local.isChecked():
            tod = TimeOfDay.KEEP_LOCAL
        elif self.keep_utc.isChecked():
            tod = TimeOfDay.KEEP_UTC
        else:
            tod = TimeOfDay.SET
        d = self.new_date.date()
        t = self.new_time.time()
        zone = self.zone.currentText().strip()
        return ShiftSettings(
            mode=Mode.ALIGNED if self.aligned.isChecked() else Mode.RELATIVE,
            time_of_day=tod,
            new_date=date(d.year(), d.month(), d.day()),
            new_time=time(t.hour(), t.minute()),
            site_zone=None if zone in ("", "UTC") else zone,
            keep_weekday=self.weekday.isChecked(),
            reference_id=self.reference.currentData(),
        )

    def recompute(self, *_args) -> None:
        self.new_time.setEnabled(self.set_time.isChecked())
        choices = self.page.choices
        times = [recording_times(c.entry.details, c.key) for c in choices]
        self.plan = plan_shifts(times, self.settings()) if times else None
        names = {c.key: c.entry.name for c in choices}
        plan = self.plan
        problems = refusal_words(plan, names) if plan is not None else []
        if plan is not None:
            for b in plan.blocked:
                words = " ".join(BLOCKER_PLAN_WORDS[r] for r in b.reasons)
                problems.append(f"{names.get(b.id, '')}: {words} Go back and untick it.")
        self.refusals.setText("\n".join(problems))
        self.refusals.setVisible(bool(problems))
        self.preview.setRowCount(0)
        site = self.page.zone
        for c in choices:
            row = self.preview.rowCount()
            self.preview.insertRow(row)
            planned = plan.by_id.get(c.key) if plan is not None else None
            self.preview.setItem(row, 0, _item(c.entry.name))
            self.preview.setItem(row, 1, _item(format_time(c.entry.details.start, site)))
            if planned is not None:
                self.preview.setItem(row, 2, _item(format_time(planned.new_start_us, UTC)))
                self.preview.setItem(row, 3, _item(format_time(planned.new_end_us, UTC)))
                notes = " ".join(warning_words(plan, c.key, names))
                self.preview.setItem(row, 4, _item(notes))
        self.preview.resizeRowsToContents()
        self.changed.emit()

    @property
    def ok(self) -> bool:
        return (
            self.plan is not None
            and self.plan.runnable
            and not self.plan.blocked
            and len(self.plan.recordings) == len(self.page.choices)
        )


class IdentityStep(QWidget):
    changed = Signal()

    def __init__(self, page: AnonymizerPage):
        super().__init__(page)
        self.page = page
        self._building = False
        intro = _label(
            "Choose what each copy is called. The name of the copy becomes its folder name "
            "and is stored in every file; the pseudonym is stored as the patient's code. "
            "Neither may contain the patient's name, ID or location, or the original "
            "recording's name.",
            muted=True,
        )
        self.table = _table(("Recording", "Name of the copy", "Pseudonym"))
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.AllEditTriggers
            & ~QAbstractItemView.EditTrigger.CurrentChanged
        )
        self.table.itemChanged.connect(self._on_item_changed)
        self.clear_patient = QCheckBox("Patient names and ID")
        self.clear_location = QCheckBox("Recording location")
        self.clear_descriptions = QCheckBox("Recording, channel and reference descriptions")
        for box in (self.clear_patient, self.clear_location, self.clear_descriptions):
            box.setChecked(True)
            box.toggled.connect(self._on_changed)
        self.text_drop = QRadioButton("Remove the text (the notes and their times stay)")
        self.text_keep = QRadioButton("Keep the text")
        self.text_drop.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.text_drop)
        group.addButton(self.text_keep)
        self.text_keep.toggled.connect(self._on_changed)
        self.keep_warning = _label(KEEP_TEXT_WARNING)
        self.keep_warning.setObjectName("Bad")
        self.clear_warning = _label(
            "Details you do not clear stay in the copies and can identify the patient."
        )
        self.clear_warning.setObjectName("Bad")
        self.problems = _label()
        self.problems.setObjectName("Bad")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(intro)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.problems)
        layout.addWidget(_label("Details to clear in the copies:", bold=True))
        for box in (self.clear_patient, self.clear_location, self.clear_descriptions):
            layout.addWidget(box)
        layout.addWidget(self.clear_warning)
        layout.addWidget(_label("Text of notes and events:", bold=True))
        layout.addWidget(self.text_drop)
        layout.addWidget(self.text_keep)
        layout.addWidget(self.keep_warning)
        self._on_changed()

    def set_recordings(self, choices: list[Choice]) -> None:
        self._building = True
        self.table.setRowCount(0)
        for i, c in enumerate(choices):
            if not c.name:
                c.name = f"SUBJ-{i + 1:03d}"
            if not c.pseudonym:
                c.pseudonym = c.name
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, _item(c.entry.name))
            self.table.setItem(row, 1, _item(c.name, editable=True))
            self.table.setItem(row, 2, _item(c.pseudonym, editable=True))
        self._building = False
        self.show_problems()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._building:
            return
        choice = self.page.choices[item.row()]
        text = item.text()
        if item.column() == 1:
            follow = choice.pseudonym == choice.name
            choice.name = text
            if follow:  # the pseudonym follows the name until it is edited
                choice.pseudonym = text
                self._building = True
                self.table.item(item.row(), 2).setText(text)
                self._building = False
        elif item.column() == 2:
            choice.pseudonym = text
        choice.names_message = None
        self.show_problems()
        self.changed.emit()

    def _on_changed(self, *_args) -> None:
        self.keep_warning.setVisible(self.text_keep.isChecked())
        self.clear_warning.setVisible(not all(self.clear_choices_bools()))
        for c in self.page.choices:
            c.names_message = None
        self.changed.emit()

    def clear_choices_bools(self) -> tuple[bool, bool, bool]:
        return (
            self.clear_patient.isChecked(),
            self.clear_location.isChecked(),
            self.clear_descriptions.isChecked(),
        )

    def clear(self) -> tuple[str, ...]:
        out = []
        if self.clear_patient.isChecked():
            out.append(CLEAR_PATIENT)
        if self.clear_location.isChecked():
            out.append(CLEAR_LOCATION)
        if self.clear_descriptions.isChecked():
            out.append(CLEAR_DESCRIPTIONS)
        return tuple(out)

    def text(self) -> str:
        return TEXT_KEEP if self.text_keep.isChecked() else TEXT_DROP

    def local_problems(self) -> list[str]:
        """Problems the app finds without asking mef3io: empty names,
        characters a folder name cannot hold, the same name twice."""
        out = []
        seen: dict[str, str] = {}
        for c in self.page.choices:
            for label, value in (("name of the copy", c.name), ("pseudonym", c.pseudonym)):
                problem = name_problem(value)
                if problem:
                    out.append(f"{c.entry.name}, {label}: {problem}")
            folded = c.name.strip().casefold()
            if folded in seen:
                out.append(
                    f"{c.entry.name}: the name of the copy is the same as for {seen[folded]}. "
                    "Each copy needs its own name."
                )
            seen[folded] = c.entry.name
        return out

    def show_problems(self) -> None:
        lines = self.local_problems()
        for c in self.page.choices:
            if c.names_message is not None:
                lines.append(f"{c.entry.name}: {c.names_message.title}. {c.names_message.text}")
        self.problems.setText("\n".join(lines))
        self.problems.setVisible(bool(lines))


class PasswordsStep(QWidget):
    changed = Signal()

    def __init__(self, page: AnonymizerPage):
        super().__init__(page)
        self.none = QRadioButton("No password (the usual choice for sharing)")
        self.both = QRadioButton("Protect the copies with two new passwords")
        self.none.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.none)
        group.addButton(self.both)
        self.basic = QLineEdit()
        self.basic_again = QLineEdit()
        self.full = QLineEdit()
        self.full_again = QLineEdit()
        for field_ in (self.basic, self.basic_again, self.full, self.full_again):
            field_.setEchoMode(QLineEdit.EchoMode.Password)
            field_.textChanged.connect(self._update)
        self.form = QWidget()
        form = QFormLayout(self.form)
        form.addRow("Basic password:", self.basic)
        form.addRow("Basic password again:", self.basic_again)
        form.addRow("Full-access password:", self.full)
        form.addRow("Full-access password again:", self.full_again)
        self.problem = _label()
        self.problem.setObjectName("Bad")
        self.interop = _label()
        self.interop.setObjectName("Caution")
        self.cleared_once = False
        self.cleared = _label(
            "For safety, the passwords you typed were cleared when you left this page. "
            "Type them again, or choose No password."
        )
        self.cleared.setObjectName("Caution")
        self.cleared.setVisible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_label(PASSWORD_EXPLANATION, muted=True))
        layout.addWidget(self.cleared)
        layout.addWidget(self.none)
        layout.addWidget(self.both)
        layout.addWidget(self.form)
        layout.addWidget(self.problem)
        layout.addWidget(self.interop)
        layout.addStretch(1)
        self.none.toggled.connect(self._update)
        self._update()

    def interop_text(self) -> str:
        """A plain warning when other programs may not open the copies."""
        if not self.both.isChecked():
            return ""
        if any(password_may_not_work_elsewhere(f.text()) for f in (self.basic, self.full)):
            return (
                "Other programs may not open the copies with these passwords: keep each "
                "password to at most 15 characters, using letters, digits and common "
                "punctuation (accented letters such as é count twice). This app will open them."
            )
        return ""

    def problem_text(self) -> str:
        if not self.both.isChecked():
            return ""
        if not self.basic.text() or not self.full.text():
            return "Type both passwords, or choose No password."
        if self.basic.text() != self.basic_again.text():
            return "The two basic passwords are not the same."
        if self.full.text() != self.full_again.text():
            return "The two full-access passwords are not the same."
        if self.basic.text() == self.full.text():
            return "The basic and the full-access password must be different."
        return ""

    def _update(self, *_args) -> None:
        self.form.setEnabled(self.both.isChecked())
        text = self.problem_text()
        self.problem.setText(text)
        self.problem.setVisible(bool(text))
        if hasattr(self, "interop"):
            warning = self.interop_text()
            self.interop.setText(warning)
            self.interop.setVisible(bool(warning))
        self.changed.emit()

    def passwords(self) -> tuple[str, str] | None:
        if not self.both.isChecked():
            return None
        return (self.basic.text(), self.full.text())

    def forget(self) -> None:
        if self.both.isChecked() or any(
            f.text() for f in (self.basic, self.basic_again, self.full, self.full_again)
        ):
            self.cleared_once = True
        for field_ in (self.basic, self.basic_again, self.full, self.full_again):
            field_.clear()
        self.none.setChecked(True)


class ReviewStep(QWidget):
    changed = Signal()

    def __init__(self, page: AnonymizerPage):
        super().__init__(page)
        self.page = page
        self.folder = ""
        self.reviews: dict[str, DestinationReview] = {}
        self.folder_label = _label("No folder chosen yet.", muted=True)
        self.folder_button = QPushButton("Choose where to save the copies…")
        self.folder_button.clicked.connect(self._choose_folder)
        self.cards = QVBoxLayout()
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(0, 0, 0, 0)
        box.addLayout(self.cards)
        box.addStretch(1)
        self.blocked = _label()
        self.blocked.setObjectName("Bad")
        row = QHBoxLayout()
        row.addWidget(self.folder_button)
        row.addWidget(self.folder_label, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(
            _label(
                "Check everything before the copies are made. Nothing has been written yet.",
                muted=True,
            )
        )
        layout.addLayout(row)
        layout.addWidget(self.blocked)
        layout.addWidget(_scroll(inner), 1)
        #: The text of each card, by recording (for tests).
        self.texts: dict[str, str] = {}

    def _choose_folder(self) -> None:
        folder = self.page.choose_output_folder()
        if folder:
            self.set_folder(folder)

    def set_folder(self, folder: str) -> None:
        self.folder = os.path.abspath(folder)
        self.folder_label.setText(self.folder)
        self.refresh()
        self.changed.emit()

    def refresh(self) -> None:
        _clear(self.cards)
        self.texts = {}
        self.reviews = {}
        page = self.page
        plan = page.dates.plan
        settings = page.dates.settings()
        if self.folder:
            try:
                free = shutil.disk_usage(self.folder).free
            except OSError:
                free = None
        else:
            free = None
        estimates = 0
        problems = []
        for c in page.choices:
            details = c.entry.details
            kept = page.selection.kept_in(c.key)
            excluded = page.selection.excluded_in(c.key)
            planned = plan.by_id.get(c.key) if plan is not None else None
            channel_bytes = {ch.name: ch.bytes for ch in details.channels}
            estimate = estimate_bytes(channel_bytes, kept)
            estimates += estimate
            lines = [f"Copy: {c.name}   (pseudonym: {c.pseudonym})"]
            lines.append(f"Channels kept: {len(kept)} of {len(details.channels)}")
            if excluded:
                lines.append("Left out: " + ", ".join(excluded))
            else:
                lines.append("Left out: none")
            if planned is not None:
                site = page.zone
                lines.append(
                    f"Start: {format_time(planned.start_us, site)}  →  "
                    f"{format_time(planned.new_start_us, UTC)}"
                )
                lines.append(
                    f"End: {format_time(planned.end_us, site)}  →  "
                    f"{format_time(planned.new_end_us, UTC)}"
                )
                lines.append(
                    shift_words(planned.shift_us, settings.time_of_day, settings.keep_weekday) + "."
                )
                lines += warning_words(plan, c.key, {x.key: x.entry.name for x in page.choices})
            lines.append(f"Estimated size: {format_size(estimate)}")
            warn = []
            if self.folder:
                destination = destination_for(self.folder, c.name)
                review = review_destination(c.entry.location, destination, estimate, free=free)
                sources = [x.entry.location for x in page.choices]
                if not review.inside_source and inside_any_recording(destination, sources):
                    review = replace(review, inside_source=True)
                self.reviews[c.key] = review
                lines.append(f"Saved as: {destination}")
                if review.exists:
                    warn.append("Something with this name is already in the folder.")
                if review.unfinished_copy_in_the_way:
                    warn.append(
                        "An unfinished copy (a folder ending in .partial) is in the way. "
                        "Delete it if no copy is running."
                    )
                if review.inside_source:
                    warn.append(
                        "The folder is inside a recording. Choose a folder that is not part "
                        "of any recording."
                    )
            text = "\n".join(lines + [f"⚠ {w}" for w in warn])
            self.texts[c.key] = text
            card = QFrame()
            card.setObjectName("Warning" if warn else "Notice")
            card_layout = QVBoxLayout(card)
            card_layout.addWidget(_label(c.entry.name, bold=True))
            body = _label(text)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            card_layout.addWidget(body)
            self.cards.addWidget(card)
            problems += [f"{c.entry.name}: {w}" for w in warn]
        if self.folder:
            free_text = format_size(free) if free is not None else "unknown"
            self.folder_label.setText(
                f"{self.folder}\nFree space: {free_text}; the copies need about "
                f"{format_size(estimates)}."
            )
            if free is not None and free < estimates:
                problems.append("There is not enough free space in this folder.")
        else:
            problems.append("Choose where to save the copies.")
        self.blocked.setText("\n".join(problems))
        self.blocked.setVisible(bool(problems))

    @property
    def ok(self) -> bool:
        if not self.folder or not self.reviews:
            return False
        total = sum(r.estimated_bytes or 0 for r in self.reviews.values())
        free = next(iter(self.reviews.values())).free_bytes
        if free is not None and free < total:
            return False
        return not any(
            r.exists or r.unfinished_copy_in_the_way or r.inside_source
            for r in self.reviews.values()
        )


class RunStep(QWidget):
    """Progress while the copies are made, then the result page."""

    def __init__(self, page: AnonymizerPage):
        super().__init__(page)
        self.page = page
        self.heading = _label(bold=True)
        self.heading.setFont(scaled_font(self, 1.15, bold=True))
        self.progress_slot = QVBoxLayout()
        self.table = _table(
            (
                "Recording",
                "Copy",
                "Original start",
                "New start",
                "Original end",
                "New end",
                "Moved by",
                "Result",
            )
        )
        self.table.setWordWrap(True)
        self.open_buttons = QGridLayout()
        self.messages = QVBoxLayout()
        self.key_box = QFrame()
        self.key_box.setObjectName("Warning")
        key_layout = QVBoxLayout(self.key_box)
        key_layout.addWidget(_label("The date key", bold=True))
        key_layout.addWidget(_label(DATE_KEY_WARNING))
        self.save_key_button = QPushButton("Save the date key…")
        self.save_key_button.clicked.connect(page.save_date_key)
        key_row = QHBoxLayout()
        key_row.addWidget(self.save_key_button)
        key_row.addStretch(1)
        key_layout.addLayout(key_row)
        self.key_box.setVisible(False)
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.heading)
        box.addLayout(self.progress_slot)
        box.addWidget(self.table)
        box.addLayout(self.open_buttons)
        box.addLayout(self.messages)
        box.addWidget(self.key_box)
        box.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_scroll(inner))
        #: Result text per recording (for tests).
        self.result_texts: dict[str, str] = {}

    def start(self, handle, requests: list[CopyRequest]) -> None:
        _clear(self.progress_slot)
        _clear(self.open_buttons)
        _clear(self.messages)
        self.result_texts = {}
        self.key_box.setVisible(False)
        self.heading.setText(
            f"Making {format_count(len(requests), 'anonymized copy', 'anonymized copies')}…"
        )
        self.progress = JobProgressWidget(handle, self)
        self.progress_slot.addWidget(self.progress)
        self.fill_table()

    def fill_table(self) -> None:
        page = self.page
        plan = page.dates.plan
        settings = page.dates.settings()
        site = page.zone
        self.table.setRowCount(0)
        for c in page.choices:
            row = self.table.rowCount()
            self.table.insertRow(row)
            planned = plan.by_id.get(c.key) if plan is not None else None
            result = page.run.results.get(c.key)
            if result is None:
                status = "Stopped" if page.run.finished else "Waiting…"
            elif result.ok:
                status = "Saved and checked"
            else:
                status = result.message.title if result.message else "Not saved"
            self.result_texts[c.key] = status
            cells = [c.entry.name, c.name]
            if planned is not None:
                cells += [
                    format_time(planned.start_us, site),
                    format_time(planned.new_start_us, UTC),
                    format_time(planned.end_us, site),
                    format_time(planned.new_end_us, UTC),
                    shift_words(planned.shift_us, settings.time_of_day, settings.keep_weekday),
                ]
            else:
                cells += [""] * 5
            cells.append(status)
            for col, text in enumerate(cells):
                self.table.setItem(row, col, _item(text))
        self.table.resizeRowsToContents()

    def finish(self) -> None:
        page = self.page
        results = page.run.results
        saved = [c for c in page.choices if results.get(c.key) and results[c.key].ok]
        failed = [c for c in page.choices if results.get(c.key) and not results[c.key].ok]
        left = page.run.leftovers
        if left:
            folders = ", ".join(sorted({os.path.basename(x.partial) for x in left}))
            self.heading.setText(
                f"{'Stopped' if page.run.stopped else 'Finished'}. "
                f"{format_count(len(saved), 'copy', 'copies')} saved and checked. An unfinished "
                f"copy could NOT be removed: delete the folder {folders} in the chosen folder "
                "yourself. The originals were not changed."
            )
        elif page.run.stopped:
            self.heading.setText(
                f"Stopped. {format_count(len(saved), 'copy', 'copies')} saved and checked; "
                "the unfinished copy was removed."
            )
        elif failed:
            self.heading.setText(
                f"{format_count(len(saved), 'copy', 'copies')} saved and checked, "
                f"{len(failed)} not saved."
            )
        else:
            self.heading.setText(
                f"Done: {format_count(len(saved), 'copy', 'copies')} saved and checked."
            )
        self.fill_table()
        for row, c in enumerate(saved):
            button = QPushButton(f"Open the copy {c.name}")
            button.setToolTip("Open it in Explore, on its Privacy check")
            button.clicked.connect(lambda _=False, choice=c: page.open_copy_of(choice))
            self.open_buttons.addWidget(button, row // 3, row % 3)
        for c in failed:
            message = results[c.key].message
            frame = QFrame()
            frame.setObjectName("Warning")
            inner = QVBoxLayout(frame)
            inner.addWidget(_label(f"{c.entry.name}: {message.title}", bold=True))
            inner.addWidget(_label(message.text))
            if message.detail:
                inner.addWidget(TechnicalDetails(message.detail))
            self.messages.addWidget(frame)
        notes = []
        for c in saved:
            for note in results[c.key].notes:
                notes.append(f"{c.name}: {note}")
        if notes:
            frame = QFrame()
            frame.setObjectName("Notice")
            inner = QVBoxLayout(frame)
            inner.addWidget(
                _label(
                    f"The check noticed {format_count(len(notes), 'thing')} worth a look before "
                    "you share the copies. None of them stopped a copy. The details are below."
                )
            )
            inner.addWidget(TechnicalDetails(technical("\n".join(notes))))
            self.messages.addWidget(frame)
        if any(c.entry.details and page.identity.text() == TEXT_KEEP for c in saved):
            self.messages.addWidget(_label(KEEP_TEXT_WARNING))
        self.key_box.setVisible(bool(saved))


# --------------------------------------------------------------------------
# The page


class AnonymizerPage(QWidget):
    """The Anonymize module."""

    #: Open a copy in Explore on its Privacy check: (path, password).
    open_copy = Signal(str, str)
    status = Signal(str)

    def __init__(self, jobs: JobManager, explorer, parent=None):
        super().__init__(parent)
        self.jobs = jobs
        self.explorer = explorer
        self.zone: tzinfo = UTC
        self.available = library_supports_anonymize()
        self.choices: list[Choice] = []
        self.selection = ChannelSelection({})
        self._selection_keys: tuple[str, ...] = ()
        self.run = RunState()
        self.handle = None
        self.names_handle = None
        self._open_password = ""
        self._passwords_dropped = False
        em = self.fontMetrics().height()

        title = _label("Anonymize recordings", wrap=False)
        title.setFont(scaled_font(self, 1.4, bold=True))
        self.step_labels: list[QLabel] = []
        steps_row = FlowLayout()
        for i, name in enumerate(STEPS):
            label = QLabel(f"{i + 1}. {name}")
            self.step_labels.append(label)
            steps_row.addWidget(label)
        self.unavailable = _label(NOT_AVAILABLE)
        self.unavailable.setObjectName("Bad")
        self.unavailable.setVisible(not self.available)

        self.recordings = RecordingsStep(self)
        self.picker = ChannelPicker(self)
        self.dates = DatesStep(self)
        self.identity = IdentityStep(self)
        self.passwords = PasswordsStep(self)
        self.review = ReviewStep(self)
        self.run_step = RunStep(self)
        self.stack = QStackedWidget(self)
        for widget in (
            self.recordings,
            self.picker,
            self.dates,
            self.identity,
            self.passwords,
            self.review,
            self.run_step,
        ):
            self.stack.addWidget(widget)
        for step in (self.recordings, self.dates, self.identity, self.passwords, self.review):
            step.changed.connect(self._update_buttons)
        self.picker.changed.connect(self._update_buttons)

        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self.back)
        self.next_button = QPushButton("Next")
        self.next_button.setProperty("primary", True)
        self.next_button.clicked.connect(self.next)
        self.why_not = _label(muted=True)
        nav = QHBoxLayout()
        nav.addWidget(self.why_not, 1)
        nav.addWidget(self.back_button)
        nav.addWidget(self.next_button)

        # Everything sits in a scroll area, as in Explore: a narrow window or
        # large text scrolls the page instead of widening the window.
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(int(em), int(em * 0.8), int(em), int(em * 0.6))
        layout.addWidget(title)
        layout.addLayout(steps_row)
        layout.addWidget(self.unavailable)
        layout.addWidget(self.stack, 1)
        layout.addLayout(nav)
        self.scroll = _scroll(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll)

        self._recordings_dirty = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(REFRESH_DELAY_MS)
        self._refresh_timer.timeout.connect(self._refresh_recordings)
        model = explorer.model
        for signal in (
            model.rowsInserted,
            model.rowsRemoved,
            model.modelReset,
            model.dataChanged,
            model.layoutChanged,
        ):
            signal.connect(self._explorer_changed)
        self.recordings.refresh()
        self.go(STEP_RECORDINGS)

    # -- shell ------------------------------------------------------------------------
    def set_theme(self, theme: Theme) -> None:  # noqa: ARG002 - colours come from the QSS
        return

    def set_zone_name(self, name: str) -> None:
        self.zone = resolve_zone(name)
        self.dates.set_zone_name(name)

    @property
    def step(self) -> int:
        return self.stack.currentIndex()

    def _explorer_changed(self, *_args) -> None:
        """The Explore list changed. A scan sends many signals: the list is
        marked out of date and rebuilt once, a moment later, and only while
        the step is on screen (else when it is shown again)."""
        self._recordings_dirty = True
        if self.isVisible() and self.step == STEP_RECORDINGS:
            self._refresh_timer.start()

    def _refresh_recordings(self) -> None:
        if not self._recordings_dirty:
            return
        self._recordings_dirty = False
        self.recordings.refresh()
        self._update_buttons()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._passwords_dropped:
            self._passwords_dropped = False
            self.go(STEP_PASSWORDS)
        if self.step == STEP_PASSWORDS:
            self.passwords.cleared.setVisible(self.passwords.cleared_once)
        if self.step == STEP_RECORDINGS and self._recordings_dirty:
            self._refresh_timer.start()

    def _choices_still_readable(self) -> bool:
        """Every chosen recording is still in the Explore list with its
        details (it may have been removed, or opened again, meanwhile). If
        not, go back to the first step and say why."""
        listed = {e.key: e for e in self.explorer.entries()}
        lost = []
        for c in self.choices:
            entry = listed.get(c.key)
            if entry is None or entry.details is None or entry.busy:
                lost.append(c.entry.name)
            else:
                c.entry = entry
        if not lost:
            return True
        self.choices = [c for c in self.choices if c.entry.name not in lost]
        self.recordings.ticked -= {k for k in self.recordings.ticked if k not in listed}
        self.go(STEP_RECORDINGS)
        self.recordings.lost.setText(
            "These recordings were removed from Explore, or are being read again, so they "
            f"were taken out of this run: {', '.join(lost)}. Check the list and continue."
        )
        self.recordings.lost.setVisible(True)
        return False

    def go(self, step: int) -> None:
        if step > STEP_RECORDINGS and step != STEP_RUN and not self._choices_still_readable():
            return
        if step != STEP_RECORDINGS:
            self.recordings.lost.setVisible(False)
        self.passwords.cleared.setVisible(step == STEP_PASSWORDS and self.passwords.cleared_once)
        self.stack.setCurrentIndex(step)
        for i, label in enumerate(self.step_labels):
            label.setObjectName(
                "StepCurrent" if i == step else ("StepDone" if i < step else "StepTodo")
            )
            label.setFont(scaled_font(label, 1.0, bold=i == step))
            label.style().unpolish(label)
            label.style().polish(label)
        if step == STEP_RECORDINGS:
            self._recordings_dirty = False
            self.recordings.refresh()
        elif step == STEP_CHANNELS:
            self._prepare_channels()
        elif step == STEP_DATES:
            self.dates.set_recordings(self.choices)
        elif step == STEP_IDENTITY:
            self.identity.set_recordings(self.choices)
        elif step == STEP_REVIEW:
            self.review.refresh()
        self._update_buttons()

    def _prepare_channels(self) -> None:
        keys = tuple(c.key for c in self.choices)
        if keys == self._selection_keys:
            return
        recordings = {c.key: [ch.name for ch in c.entry.details.channels] for c in self.choices}
        info: dict[str, list] = {}
        for c in self.choices:
            for ch in c.entry.details.channels:
                info.setdefault(ch.name, []).append(ch)
        rows = []
        for name, channels in info.items():
            rates = {format_rate(ch.sampling_frequency) for ch in channels}
            units = {ch.units for ch in channels}
            rows.append(
                ChannelRow(
                    name,
                    rate=rates.pop() if len(rates) == 1 else "varies",
                    unit=units.pop() if len(units) == 1 else "varies",
                    present_in=len(channels),
                )
            )
        previous = self.selection.excluded if self._selection_keys else ()
        selection = ChannelSelection(recordings, rows)
        notice = ""
        if self._selection_keys:
            # The batch changed: carry the choice over by name, and say so.
            carried = [n for n in previous if n in selection.rows]
            if carried:
                selection.set_kept(carried, False)
                selection.clear_history()
            notice = (
                "The chosen recordings changed. "
                + (
                    f"{format_count(len(carried), 'channel')} you had left out "
                    f"{'is' if len(carried) == 1 else 'are'} still left out, by name. "
                    if carried
                    else ""
                )
                + "Undo starts again from here."
            )
        self.selection = selection
        self._selection_keys = keys
        self.picker.set_selection(self.selection, len(self.choices), notice)

    def _blocker_text(self) -> str:
        """Why Next is not possible on this step ("" when it is)."""
        step = self.step
        if not self.available and step < STEP_RUN:
            return "This version of the app cannot make anonymized copies."
        if step == STEP_RECORDINGS:
            return "" if self.recordings.ticked else "Tick at least one recording."
        if step == STEP_CHANNELS:
            return "" if self.selection.can_continue else "Keep at least one channel."
        if step == STEP_DATES:
            return "" if self.dates.ok else "Fix the dates first (see above)."
        if step == STEP_IDENTITY:
            if self.identity.local_problems():
                return "Fix the names first (see above)."
            if self.names_handle is not None and not self.names_handle.is_finished:
                return "Checking the names…"
            return ""
        if step == STEP_PASSWORDS:
            return "Fix the passwords first." if self.passwords.problem_text() else ""
        if step == STEP_REVIEW:
            return "" if self.review.ok else "Fix what is listed above first."
        return ""

    def _update_buttons(self, *_args) -> None:
        step = self.step
        running = self.handle is not None and not self.handle.is_finished
        why = self._blocker_text()
        self.why_not.setText(why)
        self.back_button.setVisible(step != STEP_RUN)
        self.back_button.setEnabled(step > STEP_RECORDINGS)
        if step == STEP_REVIEW:
            self.next_button.setText("Make the copies")
        elif step == STEP_RUN:
            self.next_button.setText("Anonymize more recordings")
        else:
            self.next_button.setText("Next")
        if step == STEP_RUN:
            self.next_button.setEnabled(not running)
        else:
            self.next_button.setEnabled(not why)

    def back(self) -> None:
        if self.step > STEP_RECORDINGS and self.step != STEP_RUN:
            self.go(self.step - 1)

    def next(self) -> None:
        step = self.step
        if step == STEP_RUN:
            self.restart()
            return
        if self._blocker_text():
            return
        if step == STEP_RECORDINGS:
            self._refresh_recordings()
            chosen = self.recordings.chosen()
            old = {c.key: c for c in self.choices}
            self.choices = [old.get(e.key) or Choice(e) for e in chosen]
            for c, e in zip(self.choices, chosen, strict=True):
                c.entry = e
        if step == STEP_IDENTITY:
            self.check_names()
            return
        if step >= STEP_CHANNELS and not self._choices_still_readable():
            return
        if step == STEP_REVIEW:
            self.start()
            return
        self.go(step + 1)

    # -- the names, checked by mef3io before anything is written -----------------------
    def requests(self, folder: str | None = None) -> list[CopyRequest]:
        plan = self.dates.plan
        folder = folder or self.review.folder or ""
        out = []
        for c in self.choices:
            planned = plan.by_id[c.key]
            out.append(
                CopyRequest(
                    key=c.key,
                    source=c.entry.location,
                    destination=destination_for(folder, c.name) if folder else f"{c.name}.mefd",
                    shift_us=planned.shift_us,
                    pseudonym=c.pseudonym,
                    clear=self.identity.clear(),
                    text=self.identity.text(),
                    password=c.entry.password,
                    output_password=self.passwords.passwords(),
                    exclude_channels=self.selection.excluded_in(c.key),
                )
            )
        return out

    def check_names(self):
        """Ask mef3io whether each name and pseudonym is acceptable (in the
        background); move on to the next step only when all are."""
        for c in self.choices:
            c.names_message = None
        # The new passwords play no part in the names: never sent.
        requests = [replace(r, output_password=None) for r in self.requests()]
        checked = self._names_now()
        handle = self.jobs.submit("Checking the names of the copies", check_copy_names, requests)
        self.names_handle = handle
        # Locked while mef3io looks: a verdict always answers the names shown.
        self.identity.table.setEnabled(False)
        handle.finished.connect(lambda: self.identity.table.setEnabled(True))
        handle.succeeded.connect(lambda verdicts: self._names_checked(verdicts, checked))
        handle.failed.connect(self._names_failed)
        handle.finished.connect(self._update_buttons)
        self._update_buttons()
        return handle

    def _names_now(self) -> dict[str, tuple]:
        """What a name check answers for: each recording's name and
        pseudonym, and the choices that change what the names may contain."""
        shared = (self.identity.clear(), self.dates.plan.reference_id if self.dates.plan else None)
        return {c.key: (c.name, c.pseudonym, shared) for c in self.choices}

    def _names_checked(self, verdicts: dict, checked: dict | None = None) -> None:
        if checked is not None and checked != self._names_now():
            return  # the names changed since: this verdict is about others
        for c in self.choices:
            c.names_message = verdicts.get(c.key)
        self.identity.show_problems()
        if self.step == STEP_IDENTITY and all(c.names_message is None for c in self.choices):
            self.go(STEP_PASSWORDS)

    def _names_failed(self, message) -> None:
        self.identity.problems.setText(f"{message.title}. {message.text}")
        self.identity.problems.setVisible(True)

    # -- running -----------------------------------------------------------------------
    def choose_output_folder(self) -> str:
        return QFileDialog.getExistingDirectory(self, "Choose where to save the copies")

    def start(self):
        if not self.review.ok:
            return None
        requests = self.requests()
        self.run = RunState(requests=requests)
        title = (
            "Making an anonymized copy"
            if len(requests) == 1
            else f"Making {len(requests)} anonymized copies"
        )
        handle = self.jobs.submit(title, anonymize_recordings, requests)
        self.handle = handle
        handle.published.connect(self._on_results)
        handle.cancelled.connect(self._on_stopped)
        handle.failed.connect(self._on_failed)
        handle.finished.connect(self._on_finished)
        # The task has the requests; the page keeps only what "Open the copy"
        # needs (the copies' full-access password), not the basic one, and
        # the form is emptied.
        output = self.passwords.passwords()
        self._open_password = output[1] if output else ""
        self.run.requests = [replace(r, output_password=None, password="") for r in requests]
        self.passwords.forget()
        self.go(STEP_RUN)
        self.run_step.start(handle, requests)
        self._update_buttons()
        return handle

    def stop(self) -> None:
        if self.handle is not None:
            self.handle.cancel()

    def _on_results(self, items: list) -> None:
        for item in items:
            if isinstance(item, Leftover):
                self.run.leftovers.append(item)
            else:
                self.run.results[item.key] = item
        self.run_step.fill_table()

    def _on_stopped(self) -> None:
        self.run.stopped = True

    def _on_failed(self, message) -> None:
        self.run_step.messages.addWidget(_label(f"{message.title}. {message.text}"))

    def _on_finished(self) -> None:
        self.run.finished = True
        self.run_step.finish()
        self._update_buttons()
        saved = sum(1 for r in self.run.results.values() if r.ok)
        self.status.emit(f"{format_count(saved, 'anonymized copy', 'anonymized copies')} saved.")

    def hideEvent(self, event) -> None:  # noqa: N802
        # Passwords typed but not used yet are not kept while the page is away.
        had = self.passwords.both.isChecked()
        self.passwords.forget()
        if had and STEP_PASSWORDS < self.step < STEP_RUN:
            # Never go on without them: back to that step when shown again.
            self._passwords_dropped = True
        super().hideEvent(event)

    def restart(self) -> None:
        self.passwords.forget()
        self.passwords.cleared_once = False
        self._open_password = ""
        self.choices = []
        self.run = RunState()
        self.handle = None
        self._selection_keys = ()
        self.recordings.ticked.clear()
        self.go(STEP_RECORDINGS)

    def open_copy_of(self, choice: Choice) -> None:
        result = self.run.results.get(choice.key)
        if result is None or not result.ok:
            return
        self.open_copy.emit(result.destination, self._open_password)

    # -- the date key ----------------------------------------------------------------------
    def date_key_rows(self) -> list[DateKeyRow]:
        rows = []
        plan = self.dates.plan
        for c in self.choices:
            result = self.run.results.get(c.key)
            if result is None or not result.ok:
                continue
            planned = plan.by_id[c.key]
            rows.append(
                DateKeyRow(c.entry.name, c.name, planned.start_us, planned.end_us, planned.shift_us)
            )
        return rows

    def choose_date_key_path(self) -> str:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save the date key",
            os.path.join(os.path.expanduser("~"), "date-key.csv"),
            "Table (*.csv)",
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        return path

    def confirm_date_key(self) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Save the date key?")
        box.setText("The date key can undo the anonymization.")
        box.setInformativeText(DATE_KEY_WARNING)
        save = box.addButton("Save it", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Don't save", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is save

    def save_date_key(self) -> bool:
        rows = self.date_key_rows()
        if not rows or not self.confirm_date_key():
            return False
        path = self.choose_date_key_path()
        if not path:
            return False
        places = [self.review.folder] if self.review.folder else []
        places += [r.destination for r in self.run.results.values()]
        places += [c.entry.location for c in self.choices]
        if date_key_too_close(path, places):
            QMessageBox.warning(
                self,
                "Choose another place for the date key",
                "The date key would be stored next to the copies or inside a recording, where "
                "anyone who gets the copies would get it too. Nothing was saved. Choose a "
                "folder that is stored like the original recordings.",
            )
            return False
        try:
            write_date_key(path, rows)
        except OSError as exc:
            from ..model.errors import describe_error

            message = describe_error(exc)
            QMessageBox.warning(self, message.title, message.text)
            return False
        self.status.emit("Date key saved.")
        return True


__all__ = ["AnonymizerPage", "STEPS"]
