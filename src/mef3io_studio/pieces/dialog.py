"""Explore's "Cut into pieces…": ONE recording cut into several new
recordings, each saved as a checked copy (mef3io-studio#31, section 2).

Steps: which recording, where to cut (every N hours or days, optionally from
midnight in the site time zone, or at times you give; the pieces are listed
as you choose, pieces with no data are left out and said so), passwords,
names (a pattern with a live preview) and folder, then the run. The run is
the SAME as "Make new recordings" (``makenew.run.BatchRunMixin``): each piece
is one ``changes.ChangeRequest`` in the ONE batch runner, so the check before
starting, Stop, free space, name checks, resume after a crash and the check
of every piece against its original are shared. A crashed or stopped batch of
pieces is continued from "Make new recordings" (``app.continue_batch``).

Rules the window keeps:

* The original is only read; every piece is a new recording that mef3io
  checks against the original before it gets its name.
* Passwords are typed here and handed to the batch task; the window keeps
  none of them.
* The window never calls mef3io; where the data is comes from a task over
  what the Explorer already holds, and the copies run as tasks.
* Names and times are never logged.

Plain wording (docs/ui-language.md).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, tzinfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..common.pages import label as _label
from ..common.pages import scaled_font
from ..common.passwords import PasswordsStep
from ..explorer.panels import _scroll
from ..explorer.state import RecordingEntry
from ..joiner.page import TimeInput
from ..makenew.dialog import PROTECTED_NOTE, source_of
from ..makenew.run import BatchRunMixin
from ..model import makenew as makenew_model
from ..model import pieces as P
from ..model import pipeline
from ..model.coverage import RecordingInput
from ..model.recording import ACCESS_OPEN
from ..model.timefmt import format_size, format_span, format_time, zone_label
from ..workers.jobs import JobManager
from ..workers.tasks import BatchState, data_stretches

STEP_RECORDING, STEP_CUT, STEP_PASSWORDS, STEP_WHERE, STEP_RUN = range(5)
STEP_TITLES = ("Recording", "Where to cut", "Passwords", "Names and folder", "Making the pieces")

INTRO = (
    "Cut one recording into pieces. Each piece is saved as a new recording and checked against "
    "the original, which is never changed."
)
READING = "Finding out where the recording has data…"
CHOOSE_ONE = "Choose the recording to cut."
FOLDER_FIRST = "Choose a folder that exists."
PATTERN_HELP = (
    "<name> is the recording's name, <number> is 01, 02, … and <date> is the day the piece starts."
)


class PiecesDialog(BatchRunMixin, QDialog):
    """Cut one recording into pieces."""

    STEP_WHERE = STEP_WHERE
    STEP_RUN = STEP_RUN
    NOUN = "pieces"
    UNIT = "Piece"
    ONE = "a piece"

    #: Some pieces were saved: (their folders, the full-access password
    #: they were protected with or "").
    open_requested = Signal(list, str)

    def __init__(
        self,
        entries: list[RecordingEntry],
        jobs: JobManager,
        zone: tzinfo = UTC,
        keep_awake: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.jobs = jobs
        self.zone = zone
        self.keep_awake = keep_awake
        self.entries = [e for e in entries if e.details is not None]
        self.handle = None
        self.step = STEP_RECORDING
        self.batch: pipeline.Batch | None = None
        self.results = {}
        self.leftovers = []
        self.state: BatchState | None = None
        self.stopped = False
        self.finished_run = False
        self._busy = False
        self._open_password = ""
        self._journal = None
        self._preflight_problems: list[str] = []
        #: Where the chosen recording has data: None while it is being found out.
        self.runs: tuple[tuple[int, int], ...] | None = None
        self.verified = True
        self._runs_key = ""
        self._runs_handle = None
        self.at_times: list[int] = []
        self.setWindowTitle("Cut into pieces")
        em = self.fontMetrics().height()
        self.setMinimumWidth(em * 44)
        self.resize(em * 58, em * 44)

        heading = QLabel("Cut into pieces", self)
        heading.setFont(scaled_font(self, 1.25, bold=True))
        self.intro = _label(INTRO, muted=True)
        self.step_label = _label(bold=True)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(_scroll(self._build_recording()))
        self.stack.addWidget(_scroll(self._build_cut()))
        self.stack.addWidget(_scroll(self._build_passwords()))
        self.stack.addWidget(_scroll(self._build_where()))
        self.stack.addWidget(_scroll(self._build_run()))

        self.problem = _label()
        self.problem.setObjectName("Bad")
        self.back_button = QPushButton("Back", self)
        self.back_button.clicked.connect(self.back)
        self.next_button = QPushButton("Next", self)
        self.next_button.clicked.connect(self.next)
        self.start_button = QPushButton("Check and make the pieces", self)
        self.start_button.setProperty("primary", True)
        self.start_button.clicked.connect(self.start)
        self.open_button = QPushButton("Show them in Explore", self)
        self.open_button.clicked.connect(self._open_results)
        self.open_button.setVisible(False)
        self.close_button = QPushButton("Close", self)
        self.close_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(self.intro)
        layout.addWidget(self.step_label)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.problem)
        layout.addLayout(buttons)
        self.go(STEP_RECORDING)

    # -- step 1: the recording ----------------------------------------------------
    def _build_recording(self) -> QWidget:
        page = QWidget(self)
        self.recording_list = QListWidget(page)
        self.recording_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for entry in self.entries:
            item = QListWidgetItem(entry.name)
            item.setData(Qt.ItemDataRole.UserRole, entry.key)
            self.recording_list.addItem(item)
        if self.recording_list.count():
            self.recording_list.setCurrentRow(0)
        self.recording_list.currentRowChanged.connect(lambda _row: self._changed())
        layout = QVBoxLayout(page)
        layout.addWidget(_label("Which recording?", bold=True))
        layout.addWidget(_label("Choose the recording to cut into pieces.", muted=True))
        layout.addWidget(self.recording_list, 1)
        return page

    def chosen(self) -> RecordingEntry | None:
        item = self.recording_list.currentItem()
        if item is None:
            return None
        key = item.data(Qt.ItemDataRole.UserRole)
        return next((e for e in self.entries if e.key == key), None)

    def source(self):
        entry = self.chosen()
        return source_of(entry) if entry is not None else None

    # -- step 2: where to cut ------------------------------------------------------
    def _build_cut(self) -> QWidget:
        page = QWidget(self)
        self.mode_every = QRadioButton("Every", page)
        self.mode_every.setChecked(True)
        self.mode_at = QRadioButton("At the times I give", page)
        self.every = QSpinBox(page)
        self.every.setRange(1, 100_000)
        self.every.setValue(1)
        self.unit = QComboBox(page)
        self.unit.addItem("hours", P.HOURS)
        self.unit.addItem("days", P.DAYS)
        self.unit.setCurrentIndex(1)
        self.aligned = QCheckBox("Start each piece at midnight in the site time zone", page)
        self.aligned.setChecked(True)
        row = QHBoxLayout()
        row.addWidget(self.mode_every)
        row.addWidget(self.every)
        row.addWidget(self.unit)
        row.addStretch(1)
        self.at_input = TimeInput(page)
        self.at_add = QPushButton("Add this time", page)
        self.at_remove = QPushButton("Remove the chosen time", page)
        self.at_list = QListWidget(page)
        self.at_list.setMaximumHeight(self.fontMetrics().height() * 6)
        at_row = QHBoxLayout()
        at_row.addWidget(self.at_input)
        at_row.addWidget(self.at_add)
        at_row.addWidget(self.at_remove)
        at_row.addStretch(1)
        self.zone_note = _label(muted=True)
        self.table = QTableWidget(0, 5, page)
        self.table.setHorizontalHeaderLabels(["Piece", "Start", "End", "Length", "About how big"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(self.fontMetrics().height() * 8)
        self.left_out_note = _label(muted=True)
        self.mode_every.toggled.connect(self._cut_changed)
        self.every.valueChanged.connect(self._cut_changed)
        self.unit.currentIndexChanged.connect(self._cut_changed)
        self.aligned.toggled.connect(self._cut_changed)
        self.at_add.clicked.connect(self.add_time)
        self.at_remove.clicked.connect(self.remove_time)
        layout = QVBoxLayout(page)
        layout.addWidget(_label("Where to cut", bold=True))
        layout.addLayout(row)
        layout.addWidget(self.aligned)
        layout.addWidget(self.mode_at)
        layout.addLayout(at_row)
        layout.addWidget(self.at_list)
        layout.addWidget(self.zone_note)
        layout.addWidget(_label("The pieces", bold=True))
        layout.addWidget(self.table, 1)
        layout.addWidget(self.left_out_note)
        return page

    def add_time(self) -> None:
        t = self.at_input.us(self.zone)
        if t not in self.at_times:
            self.at_times.append(t)
            self.at_times.sort()
        self.mode_at.setChecked(True)
        self._cut_changed()

    def remove_time(self) -> None:
        row = self.at_list.currentRow()
        if 0 <= row < len(self.at_times):
            del self.at_times[row]
        self._cut_changed()

    def choice(self) -> P.Choice:
        return P.Choice(
            P.EVERY if self.mode_every.isChecked() else P.AT,
            self.every.value(),
            self.unit.currentData(),
            self.aligned.isChecked(),
            tuple(self.at_times),
        )

    def plan(self) -> P.PiecePlan:
        entry = self.chosen()
        if entry is None:
            return P.PiecePlan(problem=CHOOSE_ONE)
        if self.runs is None:
            return P.PiecePlan(problem=READING)
        total = sum(c.bytes for c in entry.details.channels)
        return P.plan_pieces(self.runs, total, self.choice(), self.zone, verified=self.verified)

    def _cut_changed(self, *_args) -> None:
        self._changed()

    def _fill_cut(self) -> None:
        every = self.mode_every.isChecked()
        for widget in (self.every, self.unit, self.aligned):
            widget.setEnabled(every)
        for widget in (self.at_input, self.at_add, self.at_remove, self.at_list):
            widget.setEnabled(not every)
        self.zone_note.setText(
            f"Times are shown and typed in {'UTC' if self.zone is UTC else 'the site time zone'} "
            f"({zone_label(self.zone)})."
        )
        self.at_list.clear()
        self.at_list.addItems([format_time(t, self.zone) for t in self.at_times])
        plan = self.plan()
        self.table.setRowCount(len(plan.pieces))
        for n, piece in enumerate(plan.pieces):
            cells = (
                str(piece.number),
                format_time(piece.start, self.zone),
                format_time(piece.end, self.zone),
                format_span(piece.seconds),
                format_size(piece.bytes),
            )
            for col, text in enumerate(cells):
                self.table.setItem(n, col, QTableWidgetItem(text))
        self.left_out_note.setText(P.describe_left_out(plan.left_out))

    # -- step 3: passwords ----------------------------------------------------------
    def _build_passwords(self) -> QWidget:
        page = QWidget(self)
        self.protected_note = _label(PROTECTED_NOTE.replace("recordings", "pieces"))
        self.protected_note.setObjectName("Caution")
        self.passwords = PasswordsStep(page, what="the pieces", none_label="No password")
        self.passwords.changed.connect(self._changed)
        layout = QVBoxLayout(page)
        layout.addWidget(_label("Password protection (optional)", bold=True))
        layout.addWidget(self.protected_note)
        layout.addWidget(self.passwords)
        layout.addStretch(1)
        return page

    # -- step 4: names and folder -----------------------------------------------------
    def _build_where(self) -> QWidget:
        page = QWidget(self)
        self.pattern = QLineEdit(P.DAY_PATTERN, page)
        self.pattern.textChanged.connect(self._changed)
        self.folder = ""
        self.folder_label = _label(muted=True)
        self.folder_button = QPushButton("Choose the folder…", page)
        self.folder_button.clicked.connect(self._choose_folder)
        self.names_preview = QListWidget(page)
        self.names_preview.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.names_preview.setMinimumHeight(self.fontMetrics().height() * 6)
        form = QFormLayout()
        form.addRow("Name:", self.pattern)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(self.folder_button)
        form.addRow("Saved in:", folder_row)
        layout = QVBoxLayout(page)
        layout.addWidget(_label("Names and folder", bold=True))
        layout.addLayout(form)
        layout.addWidget(_label(PATTERN_HELP, muted=True))
        layout.addWidget(
            _label(
                "If the recording's name contains the patient's name, leave out <name>.", muted=True
            )
        )
        layout.addWidget(_label("The pieces will be called", bold=True))
        layout.addWidget(self.names_preview, 1)
        return page

    def new_names(self) -> list[str]:
        entry = self.chosen()
        if entry is None:
            return []
        return P.piece_names(self.plan(), entry.name, self.pattern.text(), self.zone)

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose where to save the pieces", self.folder
        )
        if folder:
            self.folder = folder
            self._changed()

    # -- moving between steps -------------------------------------------------------------
    def go(self, step: int) -> None:
        self.step = step
        self.stack.setCurrentIndex(step)
        self.step_label.setText(f"Step {step + 1} of {len(STEP_TITLES)}: {STEP_TITLES[step]}")
        if step == STEP_CUT:
            self._find_data()
        if step == STEP_PASSWORDS:
            self.protected_note.setVisible(self._protected() and self.passwords.passwords() is None)
        self._changed()

    def _protected(self) -> bool:
        entry = self.chosen()
        return entry is not None and entry.details.access != ACCESS_OPEN

    def _find_data(self) -> None:
        """Find out where the chosen recording has data (a task over what
        the Explorer holds; nothing is read from the files)."""
        entry = self.chosen()
        if entry is None or self._runs_key == entry.key:
            return
        self._runs_key = entry.key
        self.runs = None
        item = RecordingInput(entry.key, entry.name, entry.details)
        handle = self.jobs.submit("Finding where the recording has data", data_stretches, item)
        self._runs_handle = handle
        key = entry.key

        def done(found) -> None:
            if self._runs_key == key:
                self.runs, self.verified = found
                self._changed()

        handle.succeeded.connect(done)

    def back(self) -> None:
        if self.step in (STEP_CUT, STEP_PASSWORDS, STEP_WHERE) and not self._busy:
            self.go(self.step - 1)

    def next(self) -> None:
        if self.step < STEP_WHERE and not self.blocker():
            self.go(self.step + 1)

    def blocker(self) -> str:
        """Why the step cannot be left ("" when it can)."""
        step = self.step
        if step == STEP_RECORDING:
            return "" if self.chosen() is not None else CHOOSE_ONE
        if step == STEP_CUT:
            return self.plan().problem
        if step == STEP_PASSWORDS:
            return self.passwords.problem_text()
        if step == STEP_WHERE:
            found = P.pattern_problem(self.pattern.text())
            if found:
                return found
            plan, source = self.plan(), self.source()
            if plan.problem or source is None:
                return plan.problem or CHOOSE_ONE
            problems = makenew_model.name_problems(
                P.piece_sources(source, plan), self.new_names(), self.folder
            )
            return problems[0] if problems else ""
        return ""

    def _changed(self, *_args) -> None:
        if not hasattr(self, "problem"):
            return
        if self.step == STEP_CUT:
            self._fill_cut()
        if self.step == STEP_PASSWORDS:
            self.protected_note.setVisible(self._protected() and self.passwords.passwords() is None)
        if self.step == STEP_WHERE:
            self.folder_label.setText(self.folder or "No folder chosen")
            self.names_preview.clear()
            self.names_preview.addItems(
                [f"{n + 1}. {name}" for n, name in enumerate(self.new_names())]
            )
        running = self.step == STEP_RUN
        text = "" if running else self.blocker()
        shown = text or ("\n".join(self._preflight_problems) if not running else "")
        self.problem.setText(shown)
        self.problem.setVisible(bool(shown))
        self.back_button.setVisible(not running)
        self.next_button.setVisible(self.step < STEP_WHERE)
        self.start_button.setVisible(self.step == STEP_WHERE)
        self.back_button.setEnabled(self.step > STEP_RECORDING)
        self.next_button.setEnabled(not text)
        self.start_button.setEnabled(not text and not self._busy)
        self.close_button.setEnabled(not self._busy)

    # -- the batch -----------------------------------------------------------------------
    def make_batch(self) -> pipeline.Batch:
        source, plan = self.source(), self.plan()
        requests = P.requests_for(
            source, plan, self.new_names(), self.folder, self.passwords.passwords()
        )
        originals = {
            r.key: (source.name, p.start, p.end) for r, p in zip(requests, plan.pieces, strict=True)
        }
        batch = pipeline.plan_batch(
            pipeline.FOLDERS,
            requests,
            self.folder,
            "",
            P.estimates_for(source, plan),
            originals,
        )
        return replace(batch, keep_awake=self.keep_awake)


__all__ = ["PiecesDialog"]
