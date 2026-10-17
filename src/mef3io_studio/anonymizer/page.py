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
    QSizePolicy,
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
from ..model import pipeline
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
from ..workers.tasks import (
    EARLIER as EARLIER_NOTE,
)
from ..workers.tasks import (
    UNCONFIRMED as UNCONFIRMED_NOTE,
)
from ..workers.tasks import (
    BatchState,
    BundleOutcome,
    check_copy_names,
    finish_bundle_now,
    preflight_batch,
    run_batch,
)
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
    "Output",
    "Review",
    "Make the copies",
)
(
    STEP_RECORDINGS,
    STEP_CHANNELS,
    STEP_DATES,
    STEP_IDENTITY,
    STEP_PASSWORDS,
    STEP_OUTPUT,
    STEP_REVIEW,
    STEP_RUN,
) = range(len(STEPS))

FOREIGN_RESULT = (
    "{which}: something with the copy's name is already at the destination, and it was not "
    "made by this batch, so it was left exactly as it is. Nothing was removed or written. "
    "Move or rename it, then continue the batch, or choose Keep them."
)

#: The batch archive's name until the user changes it (says nothing about anyone).
DEFAULT_BUNDLE = "anonymized-recordings.tar"

BUNDLE_EXPLANATION = (
    "One file for the whole batch, for sharing. Inside it, each anonymized recording is "
    "its own folder. This app cannot open this file directly: "
    "extract it first with any archive tool (7-Zip, the one built into Windows or macOS, "
    "or tar), which gives back the recordings, ready to use. This does NOT join the "
    "recordings into one recording: they stay separate."
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


class OutputStep(QWidget):
    """How the copies are delivered: folders, one archive per recording, or
    one archive for the whole batch."""

    changed = Signal()

    def __init__(self, page: AnonymizerPage):
        super().__init__(page)
        self.page = page
        self.folders = QRadioButton("A folder per recording (.mefd), as the originals")
        self.archives = QRadioButton(
            "An archive per recording (.mefd.tar): one file each, which this app opens directly"
        )
        self.bundle = QRadioButton("One archive for the whole batch (.tar), for sharing")
        self.folders.setChecked(True)
        group = QButtonGroup(self)
        for b in (self.folders, self.archives, self.bundle):
            group.addButton(b)
            b.toggled.connect(self._update)
        self.bundle_name = QLineEdit(DEFAULT_BUNDLE)
        self.bundle_name.textChanged.connect(self._update)
        self.bundle_help = _label(BUNDLE_EXPLANATION, muted=True)
        self.work_label = _label(muted=True)
        self.work_button = QPushButton("Change the work folder in Settings…")
        self.work_button.clicked.connect(page.settings_requested)
        self.problem = _label()
        self.problem.setObjectName("Bad")
        bundle_row = QFormLayout()
        bundle_row.addRow("Name of the batch archive:", self.bundle_name)
        work_row = QHBoxLayout()
        work_row.addWidget(self.work_label, 1)
        work_row.addWidget(self.work_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(
            _label(
                "Choose how the copies are saved. Once you start, the whole batch runs by "
                "itself: you do not need to come back after each recording. If it is "
                "interrupted, a small sealed note about it (no names or dates in clear) stays "
                "in the app's own data folder until you continue or discard the batch.",
                muted=True,
            )
        )
        layout.addWidget(self.folders)
        layout.addWidget(self.archives)
        layout.addWidget(self.bundle)
        layout.addLayout(bundle_row)
        layout.addWidget(self.bundle_help)
        layout.addLayout(work_row)
        layout.addWidget(self.problem)
        layout.addStretch(1)
        self._update()

    def output(self) -> str:
        if self.archives.isChecked():
            return pipeline.ARCHIVES
        if self.bundle.isChecked():
            return pipeline.BUNDLE
        return pipeline.FOLDERS

    def bundle_file(self) -> str:
        name = self.bundle_name.text().strip()
        return name if name.lower().endswith(".tar") else f"{name}.tar"

    def problem_text(self) -> str:
        if self.output() != pipeline.BUNDLE:
            return ""
        name = self.bundle_name.text().strip()
        problem = name_problem(name[:-4] if name.lower().endswith(".tar") else name)
        if problem:
            return f"Name of the batch archive: {problem}"
        if self.bundle_file().lower().endswith(".mefd.tar"):
            return (
                "The batch archive's name must not end in .mefd.tar: that is the name of a "
                "single recording."
            )
        if self.names_identity(name):
            return (
                "The batch archive's name contains a patient detail or the name of one of "
                "the original recordings. Choose a name that says nothing about the patients."
            )
        return ""

    def names_identity(self, name: str) -> bool:
        """The batch archive's name holds an identifying value of the batch
        (patient details, stored names, the originals' names)."""
        import unicodedata

        def fold(text: str) -> str:
            text = unicodedata.normalize("NFKD", text)
            return "".join(c for c in text if not unicodedata.combining(c)).casefold()

        folded = fold(name)
        for c in self.page.choices:
            # The originals' names and the stored names as a whole; the
            # patient's names, ID and location also word by word. Free-text
            # descriptions are NOT used: their ordinary words ("recordings")
            # would refuse harmless names such as the default one.
            whole = [c.entry.name]
            words: list[str] = []
            details = c.entry.details
            if details is not None:
                patient = details.patient
                for value in (
                    patient.first_name,
                    patient.last_name,
                    patient.patient_id,
                    patient.location,
                ):
                    if value:
                        whole.append(value)
                        words += str(value).replace(",", " ").split()
                for field_name in ("session_name", "anonymized_name"):
                    fact = details.identity_fact(field_name)
                    if fact is not None:
                        whole += list(fact.values)
            for part in [*whole, *words]:
                part = str(part).strip()
                if len(part) >= 3 and fold(part) in folded:
                    return True
        return False

    def _update(self, *_args) -> None:
        bundle = self.output() == pipeline.BUNDLE
        self.bundle_name.setEnabled(bundle)
        self.bundle_help.setVisible(bundle)
        uses_work = self.output() != pipeline.FOLDERS
        self.work_label.setVisible(uses_work)
        self.work_button.setVisible(uses_work)
        if uses_work:
            self.work_label.setText(
                "Each copy is made and checked in the work folder first, then archived; "
                "the work folder only ever holds the recording being worked on. Work "
                f"folder: {self.page.work_folder()}"
            )
        text = self.problem_text()
        self.problem.setText(text)
        self.problem.setVisible(bool(text))
        self.changed.emit()


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
        self.mode_label = _label(muted=True)
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
        layout.addWidget(self.mode_label)
        layout.addWidget(self.blocked)
        layout.addWidget(_scroll(inner), 1)
        #: The text of each card, by recording (for tests).
        self.texts: dict[str, str] = {}
        self.estimates: dict[str, int] = {}
        self.mode_text = ""
        self.work_ok = True

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
        self.estimates: dict[str, int] = {}
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
            self.estimates[c.key] = estimate
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
                destination = page.final_path(c.name, self.folder)
                review = review_destination(c.entry.location, destination, estimate, free=free)
                sources = [x.entry.location for x in page.choices]
                if not review.inside_source and inside_any_recording(destination, sources):
                    review = replace(review, inside_source=True)
                self.reviews[c.key] = review
                if page.output.output() == pipeline.BUNDLE:
                    lines.append(f"Goes into the batch archive: {destination}")
                else:
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
            if inside_any_recording(self.folder, [x.entry.location for x in page.choices]):
                problems.append("The folder is inside a recording.")
        else:
            problems.append("Choose where to save the copies.")
        output = page.output.output()
        self.work_ok = True
        if output != pipeline.FOLDERS:
            work = page.work_folder()
            largest = max((v for v in self.estimates.values()), default=0)
            work_free = pipeline.free_bytes(work)
            self.mode_text = (
                "Each copy is made and checked in the work folder, archived, the archive "
                "is checked, and the work copy is deleted. "
                f"Work folder: {work} (free: "
                f"{format_size(work_free) if work_free is not None else 'unknown'}; the "
                f"largest recording needs about {format_size(largest)})."
            )
            if work_free is not None and work_free < largest:
                problems.append(
                    "The work folder does not have room for the largest recording. Free "
                    "some space or choose another work folder in Settings."
                )
                self.work_ok = False
            if output == pipeline.BUNDLE:
                self.mode_text += " " + BUNDLE_EXPLANATION
        else:
            self.mode_text = "Each copy is written straight into the chosen folder and checked."
        self.mode_label.setText(self.mode_text)
        if page.preflight_problems:
            problems += page.preflight_problems
        self.blocked.setText("\n".join(problems))
        self.blocked.setVisible(bool(problems))

    @property
    def ok(self) -> bool:
        if not self.folder or not self.reviews or not self.work_ok:
            return False
        if inside_any_recording(self.folder, [c.entry.location for c in self.page.choices]):
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
        self.retry_button = None
        self.finish_bundle_button = None

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

    def _items(self):
        batch = self.page.batch
        return list(batch.items) if batch is not None else []

    def fill_table(self) -> None:
        page = self.page
        settings = page.dates.settings() if page.choices else None
        site = page.zone
        self.table.setRowCount(0)
        for item in self._items():
            key = item.request.key
            row = self.table.rowCount()
            self.table.insertRow(row)
            result = page.run.results.get(key)
            if result is None:
                status = "Stopped" if page.run.finished else "Waiting…"
            elif result.ok:
                status = page.status_of(result)
            else:
                status = result.message.title if result.message else "Not saved"
            self.result_texts[key] = status
            shift = item.request.shift_us
            words = (
                shift_words(shift, settings.time_of_day, settings.keep_weekday)
                if settings is not None
                else shift_words(shift)
            )
            start, end = page.original_times(item)
            cells = [
                item.original_name,
                item.request.name,
                format_time(start, site),
                format_time(start + shift, UTC),
                format_time(end, site),
                format_time(end + shift, UTC),
                words,
                status,
            ]
            for col, text in enumerate(cells):
                self.table.setItem(row, col, _item(text))
        self.table.resizeRowsToContents()

    def finish(self) -> None:
        page = self.page
        _clear(self.open_buttons)
        _clear(self.messages)
        results = page.run.results
        items = self._items()
        saved = [i for i in items if results.get(i.request.key) and results[i.request.key].ok]
        failed = [i for i in items if results.get(i.request.key) and not results[i.request.key].ok]
        left = page.run.leftovers
        if left:
            folders = ", ".join(sorted({os.path.basename(x.partial) for x in left}))
            self.heading.setText(
                f"{'Stopped' if page.run.stopped else 'Finished'}. "
                f"{format_count(len(saved), 'copy', 'copies')} saved and checked. An unfinished "
                f"copy could NOT be removed: delete {folders} yourself (in the chosen folder or "
                "the work folder). The originals were not changed."
            )
        elif page.run.stopped:
            self.heading.setText(
                f"Stopped. {format_count(len(saved), 'copy', 'copies')} saved and checked; "
                "nothing half-made was left behind."
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
        batch = page.batch
        bundle_mode = batch is not None and batch.output == pipeline.BUNDLE
        outcome = page.bundle_outcome
        state = page.batch_state
        if state is not None and state.journal is not None and (state.paused or state.retry):
            frame = QFrame()
            frame.setObjectName("Warning")
            inner = QVBoxLayout(frame)
            n = len(state.retry)
            inner.addWidget(
                _label(
                    (
                        "The batch paused: the recordings or the destination could not be "
                        "reached several times in a row. The network drive may be unavailable. "
                        if state.paused
                        else ""
                    )
                    + f"{format_count(n, 'recording')} could not be reached and can be tried "
                    "again. Nothing of them was written. Check the drive, then retry; or "
                    "close the app and choose Continue the batch later.",
                    bold=True,
                )
            )
            retry = QPushButton("Retry the failed ones")
            retry.setProperty("primary", True)
            retry.clicked.connect(page.retry_failed)
            retry.setEnabled(page._retry_batch is not None)
            self.retry_button = retry
            inner.addWidget(retry, 0, Qt.AlignmentFlag.AlignLeft)
            self.messages.addWidget(frame)
        if bundle_mode and state is not None and state.bundle_pending and outcome is None:
            frame = QFrame()
            frame.setObjectName("Warning")
            inner = QVBoxLayout(frame)
            done = sum(1 for i in items if results.get(i.request.key) and results[i.request.key].ok)
            inner.addWidget(
                _label(
                    f"The batch archive is not finished: {done} of {len(items)} recordings are "
                    "in it so far. It is kept, unfinished, so the batch can be continued (the "
                    "app offers that at its next start). Or finish it now with what is done: "
                    "it then holds only those recordings.",
                    bold=True,
                )
            )
            finish = QPushButton("Finish the archive with what is done")
            finish.clicked.connect(page.finish_bundle_now)
            self.finish_bundle_button = finish
            inner.addWidget(finish, 0, Qt.AlignmentFlag.AlignLeft)
            self.messages.addWidget(frame)
        if bundle_mode and outcome is not None:
            frame = QFrame()
            frame.setObjectName("Notice" if outcome.ok and not outcome.partial else "Warning")
            inner = QVBoxLayout(frame)
            if outcome.ok:
                missing = outcome.total - outcome.recordings
                partial = (
                    f" It is PARTIAL: {outcome.recordings} of the {outcome.total} recordings; "
                    f"{missing} {'is' if missing == 1 else 'are'} not in it."
                    if outcome.partial and missing
                    else ""
                )
                inner.addWidget(
                    _label(
                        f"The batch archive holds {format_count(outcome.recordings, 'recording')}"
                        f": {outcome.path}. Extract it with any archive tool to use them."
                        + partial,
                        bold=True,
                    )
                )
                show = QPushButton("Show in folder")
                show.clicked.connect(lambda _=False, p=outcome.path: page.show_in_folder(p))
                inner.addWidget(show, 0, Qt.AlignmentFlag.AlignLeft)
            else:
                text = "The batch archive was not made."
                if outcome.message is not None:
                    text += f" {outcome.message.title}. {outcome.message.text}"
                inner.addWidget(_label(text, bold=True))
            self.messages.addWidget(frame)
        if not bundle_mode:
            for row, item in enumerate(saved):
                name = item.request.name
                cell = QWidget()
                cell_layout = QHBoxLayout(cell)
                cell_layout.setContentsMargins(0, 0, 0, 0)
                button = QPushButton(f"Open the copy {name}")
                button.setToolTip("Open it in Explore, on its Privacy check")
                button.clicked.connect(lambda _=False, it=item: page.open_item(it))
                show = QPushButton("Show in folder")
                show.clicked.connect(lambda _=False, p=item.final: page.show_in_folder(p))
                cell_layout.addWidget(button)
                cell_layout.addWidget(show)
                self.open_buttons.addWidget(cell, row // 2, row % 2)
        for item in failed:
            message = results[item.request.key].message
            frame = QFrame()
            frame.setObjectName("Warning")
            inner = QVBoxLayout(frame)
            inner.addWidget(_label(f"{item.original_name}: {message.title}", bold=True))
            inner.addWidget(_label(message.text))
            if message.detail:
                inner.addWidget(TechnicalDetails(message.detail))
            self.messages.addWidget(frame)
        notes = []
        for item in saved:
            for note in results[item.request.key].notes:
                if note != "made earlier":
                    notes.append(f"{item.request.name}: {note}")
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
        if saved and any(i.request.text == TEXT_KEEP for i in saved):
            self.messages.addWidget(_label(KEEP_TEXT_WARNING))
        self.key_box.setVisible(bool(saved))


# --------------------------------------------------------------------------
# The page


class AnonymizerPage(QWidget):
    """The Anonymize module."""

    #: Open a copy in Explore on its Privacy check: (path, password).
    open_copy = Signal(str, str)
    status = Signal(str)
    #: The user wants the Settings (to change the work folder).
    settings_requested = Signal()
    #: A batch ended (for a system notification): plain text.
    batch_finished = Signal(str)
    #: Continuing a batch was refused by the up-front checks: plain text.
    resume_problems = Signal(str)

    def __init__(self, jobs: JobManager, explorer, settings=None, parent=None):
        super().__init__(parent)
        self.jobs = jobs
        self.explorer = explorer
        self.settings = settings
        self.batch: pipeline.Batch | None = None
        self.preflight_handle = None
        self.preflight_problems: list[str] = []
        self.bundle_outcome = None
        self._journal = None
        self.zone: tzinfo = UTC
        self.choices: list[Choice] = []
        self.selection = ChannelSelection({})
        self._selection_keys: tuple[str, ...] = ()
        self.run = RunState()
        self.handle = None
        self.names_handle = None
        self._open_password = ""
        self._passwords_dropped = False
        self._retry_batch = None
        self.batch_state = None
        self._original_times: dict[str, tuple] = {}
        self._zone_name = ""
        self._recordings_dirty = True
        self._built = False
        # The page fills whatever room it gets (it scrolls), so its size hint
        # is never needed.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        model = explorer.model
        for signal in (
            model.rowsInserted,
            model.rowsRemoved,
            model.modelReset,
            model.dataChanged,
            model.layoutChanged,
        ):
            signal.connect(self._explorer_changed)

    # The wizard's widgets are made the first time the page is shown (or
    # used): a few hundred widgets with wrapped texts, built while the user
    # is on another page, measurably slowed that page's tasks (the window
    # stalled longer when a task started; tests/test_combined_timeline_ui.py
    # pins 0.25 s). Any wizard attribute that is not there yet builds them.
    def __getattr__(self, name: str):
        if name.startswith("_") or self.__dict__.get("_built", True):
            raise AttributeError(name)
        self._build()
        return object.__getattribute__(self, name)

    @property
    def built(self) -> bool:
        return self._built

    def _build(self) -> None:
        if self._built:
            return
        self._built = True
        em = self.fontMetrics().height()

        title = _label("Anonymize recordings", wrap=False)
        title.setFont(scaled_font(self, 1.4, bold=True))
        self.step_labels: list[QLabel] = []
        steps_row = FlowLayout()
        for i, name in enumerate(STEPS):
            label = QLabel(f"{i + 1}. {name}")
            self.step_labels.append(label)
            steps_row.addWidget(label)

        self.recordings = RecordingsStep(self)
        self.picker = ChannelPicker(self)
        self.dates = DatesStep(self)
        self.identity = IdentityStep(self)
        self.passwords = PasswordsStep(self)
        self.output = OutputStep(self)
        self.review = ReviewStep(self)
        self.run_step = RunStep(self)
        self.stack = QStackedWidget(self)
        for widget in (
            self.recordings,
            self.picker,
            self.dates,
            self.identity,
            self.passwords,
            self.output,
            self.review,
            self.run_step,
        ):
            self.stack.addWidget(widget)
        for step in (
            self.recordings,
            self.dates,
            self.identity,
            self.passwords,
            self.output,
            self.review,
        ):
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
        layout.addWidget(self.stack, 1)
        layout.addLayout(nav)
        self.scroll = _scroll(inner)
        self._outer.addWidget(self.scroll)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(REFRESH_DELAY_MS)
        self._refresh_timer.timeout.connect(self._refresh_recordings)
        if self._zone_name:
            self.dates.set_zone_name(self._zone_name)
        self._recordings_dirty = False
        self.recordings.refresh()
        self.go(STEP_RECORDINGS)

    # -- shell ------------------------------------------------------------------------
    def set_theme(self, theme: Theme) -> None:  # noqa: ARG002 - colours come from the QSS
        return

    def set_zone_name(self, name: str) -> None:
        self.zone = resolve_zone(name)
        self._zone_name = name
        if self._built:
            self.dates.set_zone_name(name)

    @property
    def step(self) -> int:
        return self.stack.currentIndex()

    def _explorer_changed(self, *_args) -> None:
        """The Explore list changed. A scan sends many signals: the list is
        marked out of date and rebuilt once, a moment later, and only while
        the step is on screen (else when it is shown again)."""
        self._recordings_dirty = True
        if self._built and self.isVisible() and self.step == STEP_RECORDINGS:
            self._refresh_timer.start()

    def _refresh_recordings(self) -> None:
        if not self._recordings_dirty:
            return
        self._recordings_dirty = False
        self.recordings.refresh()
        self._update_buttons()

    def showEvent(self, event) -> None:  # noqa: N802
        self._build()
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
        elif step == STEP_OUTPUT:
            self.output._update()
        elif step == STEP_REVIEW:
            self.preflight_problems = []
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
        if step == STEP_OUTPUT:
            return "Fix the name of the batch archive first." if self.output.problem_text() else ""
        if step == STEP_REVIEW:
            if self.preflight_handle is not None and not self.preflight_handle.is_finished:
                return "Checking everything before starting…"
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
            return self.start()
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

    # -- the work folder (a setting) -------------------------------------------------
    def work_folder(self) -> str:
        """Where copies are made and checked before they are archived: the
        user's choice in Settings, else the default in the app's data folder."""
        if self.settings is None:
            return pipeline.default_work_folder()
        from ..common.settings_dialog import work_folder

        return work_folder(self.settings)

    def keep_awake(self) -> bool:
        return True if self.settings is None else bool(self.settings.get("anonymizer/keep_awake"))

    def final_path(self, name: str, folder: str | None = None) -> str:
        folder = folder if folder is not None else self.review.folder
        output = self.output.output()
        if output == pipeline.ARCHIVES:
            return os.path.join(folder, f"{name}.mefd.tar")
        if output == pipeline.BUNDLE:
            return os.path.join(folder, self.output.bundle_file())
        return destination_for(folder, name)

    def make_batch(self) -> pipeline.Batch:
        requests = self.requests()
        plan = self.dates.plan
        originals = {
            c.key: (c.entry.name, plan.by_id[c.key].start_us, plan.by_id[c.key].end_us)
            for c in self.choices
        }
        batch = pipeline.plan_batch(
            self.output.output(),
            requests,
            self.review.folder,
            self.work_folder(),
            dict(self.review.estimates),
            originals,
            bundle_name=self.output.bundle_file(),
        )
        return replace(batch, keep_awake=self.keep_awake())

    # -- running -----------------------------------------------------------------------
    def start(self):
        """Check everything that can be checked up front (in the background),
        then run the whole batch unattended."""
        if not self.review.ok:
            return None
        batch = self.make_batch()
        self.preflight_problems = []
        handle = self.jobs.submit("Checking everything before starting", preflight_batch, batch)
        self.preflight_handle = handle
        handle.succeeded.connect(lambda verdict: self._preflight_done(batch, verdict))
        handle.failed.connect(self._preflight_failed)
        handle.finished.connect(self._update_buttons)
        self._update_buttons()
        return handle

    def resume(self, journal, password_for, output_password=None, ask_existing=None):
        """Continue an interrupted batch from its journal. The up-front checks
        run again for the recordings still to do: every original opened with
        ITS password, a finished copy opened with the copies' new password,
        names, space; the app's own unfinished files are removed. Results
        already at the destination for unfinished recordings are kept or
        made again, as the user chooses (``ask_existing``). Passwords were
        never journaled; the caller asked for them again."""
        from ..model import journal as journal_

        batch = pipeline.from_payload(journal.payload, password_for, output_password)
        batch = replace(batch, keep_awake=self.keep_awake())
        self.restart()
        self.batch = batch
        self._journal = journal
        handle = self.jobs.submit(
            "Checking everything before continuing",
            preflight_batch,
            batch,
            list(journal.states),
            output_password[1] if output_password else "",
            journal,
        )
        self.preflight_handle = handle

        def done(verdict: dict) -> None:
            problems = list(verdict.get("problems", []))
            if problems:
                self.preflight_problems = problems
                self.resume_problems.emit("\n".join(problems))
                return
            run = pipeline.with_originals(batch, verdict.get("originals", {}))
            # Finished ones: their dates are read again for the date key too.
            run = pipeline.with_originals(run, verdict.get("finished_originals", {}))
            existing = verdict.get("existing", [])
            if existing and ask_existing is not None:
                if ask_existing(existing, len(batch.items)) == "keep":
                    for n in existing:
                        journal.payload["items"][n]["unconfirmed"] = True
                        journal.mark(n, journal_.DONE)
                else:
                    ours = verdict.get("ours", {})
                    foreign = [n for n in existing if not ours.get(n)]
                    if foreign:
                        # A name alone proves nothing: never remove what this
                        # batch did not make.
                        which = ", ".join(
                            f"Recording {n + 1} of {len(batch.items)}" for n in foreign
                        )
                        self.preflight_problems = [FOREIGN_RESULT.format(which=which)]
                        self.resume_problems.emit(self.preflight_problems[0])
                        return
                    for n in existing:
                        pipeline.remove_own_result(run.items[n].final)
            self.batch = run
            self.run_batch(run, journal)

        handle.succeeded.connect(done)
        handle.failed.connect(lambda m: self.resume_problems.emit(f"{m.title}. {m.text}"))
        return handle

    def _preflight_failed(self, message) -> None:
        self.preflight_problems = [f"{message.title}. {message.text}"]
        self.review.refresh()

    def _preflight_done(self, batch: pipeline.Batch, verdict: dict) -> None:
        problems = list(verdict.get("problems", []))
        names = verdict.get("names", {})
        for c in self.choices:
            message = names.get(c.key)
            if message is not None:
                problems.append(f"{c.entry.name}: {message.title}. {message.text}")
        self.preflight_problems = problems
        if problems:
            self.review.refresh()
            self._update_buttons()
            return
        self.run_batch(batch)

    def run_batch(self, batch: pipeline.Batch, journal=None):
        # The task gets the batch with its passwords; the page keeps a copy
        # without any (for the summary and the date key), plus the full one
        # only while some recordings may need to be retried.
        self._retry_batch = batch
        self.batch = pipeline.without_passwords(batch)
        self.batch_state = None
        self.bundle_outcome = None
        self.run = RunState(requests=[i.request for i in batch.items])
        n = len(batch.items)
        title = "Making an anonymized copy" if n == 1 else f"Making {n} anonymized copies"
        handle = self.jobs.submit(title, run_batch, batch, journal)
        self.handle = handle
        handle.published.connect(self._on_results)
        handle.cancelled.connect(self._on_stopped)
        handle.failed.connect(self._on_failed)
        handle.finished.connect(self._on_finished)
        # The task has the requests; the page keeps only what "Open the copy"
        # needs (the copies' full-access password), not the basic one, and
        # the form is emptied.
        output = next(
            (i.request.output_password for i in batch.items if i.request.output_password), None
        )
        self._open_password = output[1] if output else ""
        self.run.requests = [
            replace(i.request, output_password=None, password="") for i in batch.items
        ]
        self.passwords.forget()
        self.passwords.cleared_once = False
        self.go(STEP_RUN)
        self.run_step.start(handle, self.run.requests)
        self._update_buttons()
        return handle

    def stop(self) -> None:
        if self.handle is not None:
            self.handle.cancel()

    def _on_results(self, items: list) -> None:
        for item in items:
            if isinstance(item, Leftover):
                self.run.leftovers.append(item)
            elif isinstance(item, BundleOutcome):
                self.bundle_outcome = item
            elif isinstance(item, BatchState):
                self.batch_state = item
                if item.journal is None or not (item.retry or item.bundle_pending):
                    self._retry_batch = None  # nothing to retry: drop the passwords
            else:
                self.run.results[item.key] = item
                if item.source_start_us is not None:
                    # mef3io's own record of the original's dates (in
                    # memory only): the summary and the date key use it.
                    self._original_times[item.key] = (item.source_start_us, item.source_end_us)
        self.run_step.fill_table()

    def _on_stopped(self) -> None:
        self.run.stopped = True

    def _on_failed(self, message) -> None:
        self.run_step.messages.addWidget(_label(f"{message.title}. {message.text}"))

    def _on_finished(self) -> None:
        self.run.finished = True
        if self.batch_state is None or self.batch_state.journal is None:
            self._retry_batch = None
        self.run_step.finish()
        self._update_buttons()
        delivered = [r for r in self.run.results.values() if self.delivered(r)]
        total = len(self.batch.items) if self.batch is not None else len(delivered)
        state = self.batch_state
        text = (
            f"Anonymizing finished: {len(delivered)} of {total} "
            f"{'copy' if total == 1 else 'copies'} saved and checked."
        )
        if state is not None and state.paused:
            text = (
                f"Anonymizing paused: {len(delivered)} of {total} done. The files could not be "
                "reached; check the drive, then retry."
            )
        elif self.run.stopped:
            text = f"Anonymizing stopped: {len(delivered)} of {total} saved and checked."
        if state is not None and state.bundle_pending:
            text += " The batch archive is not finished yet."
        self.status.emit(text)
        self.batch_finished.emit(text)

    # -- what a result means, with one archive for the batch ---------------------------------
    def delivered(self, result) -> bool:
        """The result is where the user can use it: saved and checked, and,
        with one archive for the batch, the archive was finished."""
        if not result.ok:
            return False
        if self.batch is not None and self.batch.output == pipeline.BUNDLE:
            outcome = self.bundle_outcome
            return outcome is not None and outcome.ok
        return True

    def status_of(self, result) -> str:
        notes = result.notes or ()
        if self.batch is not None and self.batch.output == pipeline.BUNDLE:
            outcome = self.bundle_outcome
            if outcome is None:
                return "Added to the batch archive"
            if not outcome.ok:
                return "Not delivered"
            return "Saved and checked (in the batch archive)"
        if UNCONFIRMED_NOTE in notes:
            return "Made before the interruption (not confirmed)"
        if EARLIER_NOTE in notes:
            return "Saved and checked (earlier)"
        return "Saved and checked"

    def retry_failed(self):
        """Run the recordings that could not be reached again (the batch's
        journal knows which; the passwords are still in memory)."""
        state = self.batch_state
        if self._retry_batch is None or state is None or state.journal is None:
            return None
        return self.run_batch(self._retry_batch, state.journal)

    def finish_bundle_now(self):
        """Give the unfinished batch archive its final name with the
        recordings already in it, and end the batch."""
        state = self.batch_state
        journal = state.journal if state is not None else self._journal
        if journal is None:
            return None
        handle = self.jobs.submit("Finishing the batch archive", finish_bundle_now, journal)

        def done(outcome) -> None:
            self.bundle_outcome = outcome
            self.batch_state = None
            self._retry_batch = None
            self.run_step.finish()
            text = (
                f"The batch archive was finished with {outcome.recordings} of the "
                f"{outcome.total} recordings; {outcome.total - outcome.recordings} are not in it."
                if outcome.ok
                else "The batch archive could not be finished."
            )
            self.status.emit(text)
            self.batch_finished.emit(text)

        handle.succeeded.connect(done)
        return handle

    def hideEvent(self, event) -> None:  # noqa: N802
        if not self._built:
            super().hideEvent(event)
            return
        # Passwords typed but not used yet are not kept while the page is away.
        had = self.passwords.both.isChecked()
        self.passwords.forget()
        if had and STEP_PASSWORDS < self.step < STEP_RUN:
            # Never go on without them: back to that step when shown again.
            self._passwords_dropped = True
        super().hideEvent(event)

    def original_times(self, item) -> tuple[int, int]:
        """The original's start and end: as mef3io reported them when it made
        the copy, else as read before the batch."""
        start, end = self._original_times.get(item.request.key, (None, None))
        return (
            item.original_start if start is None else start,
            item.original_end if end is None else end,
        )

    def restart(self) -> None:
        self._original_times = {}
        self._retry_batch = None
        self.batch_state = None
        self.batch = None
        self.bundle_outcome = None
        self.preflight_problems = []
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
        item = next(
            (i for i in (self.batch.items if self.batch else ()) if i.request.key == choice.key),
            None,
        )
        if item is not None:
            self.open_item(item)

    def open_item(self, item) -> None:
        result = self.run.results.get(item.request.key)
        if result is None or not result.ok:
            return
        self.open_copy.emit(item.final, self._open_password)

    def show_in_folder(self, path: str) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path) or path))

    # -- the date key ----------------------------------------------------------------------
    def date_key_rows(self) -> list[DateKeyRow]:
        rows = []
        for item in self.batch.items if self.batch is not None else ():
            result = self.run.results.get(item.request.key)
            if result is None or not self.delivered(result):
                continue
            start, end = self.original_times(item)
            rows.append(
                DateKeyRow(item.original_name, item.request.name, start, end, item.request.shift_us)
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
        if self.batch is not None:
            places += [self.batch.destination, self.batch.work_folder]
            places += [i.final for i in self.batch.items]
            places += [i.request.source for i in self.batch.items]
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
