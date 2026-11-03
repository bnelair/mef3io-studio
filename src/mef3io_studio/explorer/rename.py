"""The Rename window: new names with a preview, a confirmation, and Undo.

Opened from Explore for the selected recordings. It looks first (in the
background: the start times, whether each recording's stored name is in
step with its folder, whether anything is writing to it), lists the
recordings in time order, lets the user type new names or build them from a
pattern, and checks every name before anything happens (the rules are in
:mod:`..model.rename`). Renaming is mef3io's journaled operation
(:mod:`..model.rename_ops`): it changes the folder, the notes of the whole
recording and the name stored in every file together, and a crash leaves a
recording either fully old or fully new.

Rules the window keeps:

* Nothing happens before the user confirms (the confirmation has no default
  "Yes"), and every name is checked first.
* Undo is a second, equally careful operation (also previewed and
  confirmed; password-protected recordings ask for their password again).
  It is available while the app stays open.
* The GUI thread never calls mef3io: every look and every rename is a task.
* Recordings whose start time is not confirmed stay out of the time order
  and cannot give a name from the start time.

Names and paths are never logged. Plain wording (docs/ui-language.md).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, tzinfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..common.dialogs import MessageDialog
from ..common.pages import scaled_font
from ..model import rename as rn
from ..model import rename_ops as ops
from ..model.errors import UserMessage
from ..model.recording import is_packed, location_key, recording_name
from ..model.timefmt import format_count, format_time, zone_label
from ..model.times import RecordingTime, order_by_time
from ..workers.jobs import JobManager
from ..workers.tasks import (
    check_rename_state,
    read_recording_times,
    rename_recordings,
    undo_renames,
)
from .dialogs import PasswordDialog
from .state import RecordingEntry

COL_NOW, COL_NEW, COL_START, COL_NOTE = range(4)
COLUMNS = ("Now", "New name", "Starts", "Notes")

DATE_WARNING = (
    "Names built from dates show when a recording was made, and passwords do not hide "
    "names or timing. If these recordings are meant to be shared, anonymize them first."
)
UNVERIFIED_START = "Not confirmed"


@dataclass
class _Row:
    entry: RecordingEntry
    time: RecordingTime | None = None
    state: rn.RowState = field(default_factory=rn.RowState)
    new_name: str = ""
    index: int | None = None
    #: What happened to it (after the run).
    result: ops.ItemResult | None = None
    #: A pattern that could not be applied to it.
    template_problem: rn.NameProblem | None = None

    @property
    def packed(self) -> bool:
        return is_packed(self.entry.location)

    @property
    def old_name(self) -> str:
        return recording_name(self.entry.location)


@dataclass(frozen=True)
class RenameBatch:
    """What one rename did, kept in memory so it can be undone."""

    units: tuple[ops.UndoUnit, ...] = field(repr=False)

    def __bool__(self) -> bool:
        return bool(self.units)


def _plural(n: int, one: str) -> str:
    return format_count(n, one)


class RenameDialog(QDialog):
    """Rename the given recordings (see the module notes)."""

    #: ``(old key, new location)`` for every recording whose place changed.
    relocated = Signal(list)
    #: A batch that can be undone.
    renamed = Signal(object)

    def __init__(
        self,
        jobs: JobManager,
        entries: list[RecordingEntry],
        zone: tzinfo = UTC,
        in_use: Callable[[], set[str]] | None = None,
        undo: RenameBatch | None = None,
        listed: Callable[[], set[str]] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.jobs = jobs
        self.zone = zone
        #: Which recordings are open in a signal viewer RIGHT NOW (asked again
        #: at every confirmation), and which are still in the list.
        self._in_use_now = in_use or (lambda: set())
        self._listed = listed
        self.in_use = set(self._in_use_now())
        self.setWindowTitle("Rename recordings")
        em = self.fontMetrics().height()
        self.setMinimumWidth(em * 44)
        self.rows = [_Row(e, new_name=recording_name(e.location)) for e in entries]
        self.folders: dict[str, set[str]] = {}
        self.plan = rn.RenamePlan((), ())
        self.batch: RenameBatch | None = undo
        self.results: list[ops.ItemResult] = []
        self.busy = ""
        self._loading = 2 if self.rows else 0
        self._ready = False
        self._done = False
        self._blocked_rows: set[int] = set()
        self._current = None
        self._skipped = 0
        self._handles: list = []
        #: Asks "are you sure"; replaced in tests.
        self.confirm = self._ask_confirmation
        #: Asks for a password (undo); replaced in tests: (name, wrong) -> str.
        self.ask_password = self._ask_password

        heading = QLabel("Rename recordings", self)
        heading.setFont(scaled_font(self, 1.25, bold=True))
        self.intro = QLabel(
            "A recording's name is kept in several places: its folder, the notes that belong "
            "to the whole recording, and inside every file. This changes all of them "
            "together, so other programs still find the notes. Signals, notes and times are "
            "not touched. You can undo it afterwards.",
            self,
        )
        self.intro.setWordWrap(True)
        self.warning = QLabel(DATE_WARNING, self)
        self.warning.setObjectName("Warning")
        self.warning.setWordWrap(True)
        self.status = QLabel("Looking at the recordings…", self)
        self.status.setWordWrap(True)

        self.pattern = QLineEdit(self)
        self.pattern.setPlaceholderText("{start:%Y-%m-%d_%H%M%S}")
        self.pattern.setAccessibleName("Pattern for the new names")
        self.pattern_button = QPushButton("Fill in the names", self)
        self.pattern_button.clicked.connect(self.apply_pattern)
        self.pattern_help = QLabel(
            f"{rn.TEMPLATE_HELP} Times are in {zone_label(zone)}. You can also type any name "
            "into the list.",
            self,
        )
        self.pattern_help.setObjectName("Muted")
        self.pattern_help.setWordWrap(True)
        pattern_row = QHBoxLayout()
        pattern_row.addWidget(QLabel("Pattern:", self))
        pattern_row.addWidget(self.pattern, 1)
        pattern_row.addWidget(self.pattern_button)

        self.table = QTableWidget(len(self.rows), len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().hide()
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setMinimumHeight(em * 12)
        self.table.itemChanged.connect(self._on_item_changed)

        self.drift_box = QCheckBox(
            "Put the names stored inside in step with the folder first", self
        )
        self.drift_box.setChecked(True)
        self.drift_box.setVisible(False)
        self.drift_box.toggled.connect(lambda _on: self._recheck())
        self.archive_note = QLabel(rn.ARCHIVE_NOTE, self)
        self.archive_note.setObjectName("Muted")
        self.archive_note.setWordWrap(True)
        self.archive_note.setVisible(any(r.packed for r in self.rows))
        self.summary = QLabel("", self)
        self.summary.setWordWrap(True)

        self.rename_button = QPushButton("Rename…", self)
        self.rename_button.clicked.connect(self.start)
        self.undo_button = QPushButton("Undo the last rename…", self)
        self.undo_button.clicked.connect(self.start_undo)
        self.stop_button = QPushButton("Stop", self)
        self.stop_button.clicked.connect(self._stop)
        self.stop_button.setVisible(False)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.rename_button)
        row.addWidget(self.undo_button)
        row.addWidget(self.stop_button)
        row.addStretch(1)
        row.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.setSpacing(int(em * 0.6))
        for w in (heading, self.intro, self.warning, self.status):
            layout.addWidget(w)
        layout.addLayout(pattern_row)
        layout.addWidget(self.pattern_help)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.drift_box)
        layout.addWidget(self.archive_note)
        layout.addWidget(self.summary)
        layout.addLayout(row)
        self._fill_table()
        if self.rows:
            self._load()
        else:
            self.status.setText("Select recordings in the list to rename them.")
        self._enable()

    # -- looking first -----------------------------------------------------------
    def _load(self) -> None:
        requests = [
            ops.StateRequest(r.entry.key, r.entry.location, r.packed, {}) for r in self.rows
        ]
        state = self.jobs.submit("Looking at the recordings", check_rename_state, requests)
        state.succeeded.connect(self._got_state)
        state.failed.connect(self._load_failed)
        times = [(r.entry.key, r.entry.location, r.entry.password) for r in self.rows]
        found = self.jobs.submit("Reading the start times", read_recording_times, times)
        found.succeeded.connect(self._got_times)
        found.failed.connect(self._load_failed)
        self._handles += [state, found]

    def _got_state(self, result) -> None:
        states, folders = result
        self.folders = folders
        for r in self.rows:
            r.state = states.get(r.entry.key, rn.RowState())
            r.state = rn.RowState(
                drift=r.state.drift,
                unfinished=r.state.unfinished,
                recently_written=r.state.recently_written,
                needs_password=self._needs_password(r.entry),
                in_use=r.entry.key in self.in_use,
                unreadable=r.state.unreadable,
                deepest_inside=r.state.deepest_inside,
            )
        self._loaded()

    def _needs_password(self, entry: RecordingEntry) -> bool:
        details = entry.details
        if entry.outcome is not None and entry.outcome.needs_password:
            return True
        return bool(details is not None and details.password_protected and not entry.password)

    def _got_times(self, found: dict) -> None:
        for r in self.rows:
            value = found.get(r.entry.key)
            r.time = value if isinstance(value, RecordingTime) else None
        self._loaded()

    def _load_failed(self, message: UserMessage) -> None:
        self._loading = 0
        self.status.setText(message.text)
        self._enable()

    def _loaded(self) -> None:
        self._loading -= 1
        if self._loading > 0:
            return
        ordered, unverified = order_by_time({r.entry.key: r.time for r in self.rows})
        by_key = {r.entry.key: r for r in self.rows}
        self.rows = [by_key[k] for k in ordered] + [by_key[k] for k in unverified]
        for n, key in enumerate(ordered, start=1):
            by_key[key].index = n
        self.drift_box.setVisible(any(r.state.drift for r in self.rows))
        self._fill_table()
        self.status.setText(
            "Type the new names, or fill them in from a pattern. Recordings are listed in "
            "time order."
        )
        self._recheck()

    # -- the table -----------------------------------------------------------------
    def _fill_table(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        for n, r in enumerate(self.rows):
            now = QTableWidgetItem(r.old_name)
            now.setFlags(Qt.ItemFlag.ItemIsEnabled)
            new = QTableWidgetItem(r.new_name)
            start = QTableWidgetItem(self._start_text(r))
            start.setFlags(Qt.ItemFlag.ItemIsEnabled)
            note = QTableWidgetItem("")
            note.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for col, item in ((COL_NOW, now), (COL_NEW, new), (COL_START, start), (COL_NOTE, note)):
                self.table.setItem(n, col, item)
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()

    def _start_text(self, r: _Row) -> str:
        if r.time is None:
            return "…" if self._loading > 0 else UNVERIFIED_START
        if not r.time.verified or r.time.start is None:
            return UNVERIFIED_START
        return format_time(r.time.start, self.zone)

    def _on_item_changed(self, item) -> None:
        if item.column() == COL_NEW and 0 <= item.row() < len(self.rows):
            self.rows[item.row()].new_name = rn.clean_name(
                item.text(), self.rows[item.row()].packed
            )
            self._recheck()

    def set_new_name(self, row: int, text: str) -> None:
        """Type a name into a row (as the user would)."""
        self.table.item(row, COL_NEW).setText(text)

    def new_names(self) -> list[str]:
        return [r.new_name for r in self.rows]

    def apply_pattern(self) -> None:
        template = self.pattern.text().strip() or self.pattern.placeholderText()
        self.apply_template(template)

    def apply_template(self, template: str) -> None:
        self.table.blockSignals(True)
        for n, r in enumerate(self.rows):
            start = r.time.start if r.time is not None and r.time.verified else None
            made = rn.name_from_template(
                template, index=r.index, old_name=r.old_name, start_us=start, zone=self.zone
            )
            if isinstance(made, rn.NameProblem):
                r.new_name = r.old_name
                self.table.item(n, COL_NEW).setText(r.old_name)
                r.template_problem = made
            else:
                made = rn.clean_name(made, r.packed)
                r.new_name = made
                self.table.item(n, COL_NEW).setText(made)
                r.template_problem = None
        self.table.blockSignals(False)
        self._recheck()

    # -- checking ------------------------------------------------------------------
    def _jobs_wanted(self) -> list[tuple[_Row, bool]]:
        """The rows that will be touched: ``(row, only_settle)``."""
        fix = self.drift_box.isChecked()
        out = []
        for r in self.rows:
            changed = r.new_name != r.old_name
            if changed:
                out.append((r, False))
            elif not r.packed and (r.state.unfinished or (r.state.drift and fix)):
                out.append((r, True))
        return out

    def _recheck(self) -> None:
        wanted = self._jobs_wanted()
        renames = [(r, s) for r, s in wanted if not s]
        items = [
            rn.RenameItem(
                r.entry.key,
                r.entry.location,
                r.old_name,
                r.new_name,
                r.packed,
                r.state.deepest_inside,
            )
            for r, _s in renames
        ]
        self.plan = rn.plan_renames(items, lister=lambda f: self.folders.get(f, set()))
        by_row = {id(renames[i][0]): self.plan.rows[i] for i in range(len(renames))}
        self._blocked_rows = set()
        for n, r in enumerate(self.rows):
            note = self._note_for(r, by_row.get(id(r)))
            self.table.item(n, COL_NOTE).setText(note)
        self._update_summary(wanted)
        self._enable()

    def _note_for(self, r: _Row, check: rn.RowCheck | None) -> str:
        parts: list[str] = []
        if r.template_problem is not None:
            parts.append(rn.problem_text(r.template_problem, r.packed))
        if check is not None and not check.ok:
            parts.append(rn.problem_text(check.problems[0], r.packed))
        blocked = rn.blocked_reason(r.state, r.packed)
        touched = r.new_name != r.old_name or r.state.unfinished
        if blocked and touched:
            parts.append(blocked)
            self._blocked_rows.add(id(r))
        if r.state.unfinished:
            parts.append(rn.UNFINISHED_NOTE)
        if r.state.drift and not r.packed:
            parts.append(
                rn.DRIFT_NOTE
                if self.drift_box.isChecked()
                else "Its stored name is out of step: tick the box below to fix that first."
            )
        return " ".join(parts)

    def _update_summary(self, wanted) -> None:
        bad = [r for r, s in wanted if id(r) in self._blocked_rows]
        problems = sum(1 for c in self.plan.rows if not c.ok)
        drift_blocked = [
            r for r, s in wanted if not s and r.state.drift and not self.drift_box.isChecked()
        ]
        if not wanted:
            self.summary.setText("Nothing to rename yet: type a new name for at least one.")
        elif problems or bad or drift_blocked:
            self.summary.setText("Fix the names marked in the list, then you can rename.")
        else:
            n = len(wanted)
            self.summary.setText(f"Ready to rename {_plural(n, 'recording')}.")
        self._ready = bool(wanted) and not (problems or bad or drift_blocked)

    def _enable(self) -> None:
        idle = not self.busy and self._loading <= 0
        self.rename_button.setEnabled(idle and self._ready and not self._done)
        self.undo_button.setEnabled(idle and bool(self.batch))
        self.undo_button.setVisible(bool(self.batch))
        self.table.setEnabled(not self.busy)
        self.pattern_button.setEnabled(idle and not self._done)
        self.stop_button.setVisible(bool(self.busy))

    # -- renaming --------------------------------------------------------------------
    def _ask_confirmation(self, text: str, informative: str) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Rename?")
        box.setText(text)
        box.setInformativeText(informative)
        yes = box.addButton("Rename", QMessageBox.ButtonRole.AcceptRole)
        no = box.addButton("Don't rename", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(no)  # never "Yes" by default
        box.exec()
        return box.clickedButton() is yes

    def _ask_password(self, name: str, wrong: bool) -> str:
        dialog = PasswordDialog(name, wrong=wrong, parent=self)
        return dialog.password() if dialog.exec() else ""

    def _refresh_in_use(self) -> bool:
        """Ask again which recordings a viewer shows; False (and the list
        marked) when one of the rows to rename is now in use."""
        now = set(self._in_use_now())
        for r in self.rows:
            r.state = replace(r.state, in_use=r.entry.key in now)
        self.in_use = now
        self._recheck()
        if not self._ready:
            self.status.setText("Not renamed: see the notes in the list.")
        return self._ready

    def start(self) -> None:
        """Rename (after the confirmation). The rows must be ready."""
        if self.busy or self._done or not self._refresh_in_use():
            return
        wanted = self._jobs_wanted()
        count = len([1 for _r, s in wanted if not s])
        settled = len(wanted) - count
        text = (
            f"Rename {_plural(count, 'recording')}?"
            if count
            else f"Put {_plural(settled, 'recording')} in order?"
        )
        more = (
            "Each name is changed in every place it is kept; signals, notes and times are "
            "not touched. If something goes wrong, the recording is put back as it was. "
            "You can undo this afterwards (while the app stays open). Make sure no "
            "recorder or other program is using these recordings."
        )
        if any(r.packed for r, _s in wanted):
            more += " " + rn.ARCHIVE_NOTE
        if not self.confirm(text, more):
            return
        if not self._refresh_in_use():  # a viewer may have been opened meanwhile
            return
        order = {self.rows[i_].entry.key: pos for pos, i_ in enumerate(self._order_rows(wanted))}
        jobs = []
        for r, settle in sorted(wanted, key=lambda w: order.get(w[0].entry.key, 10**9)):
            jobs.append(
                ops.RenameJob(
                    r.entry.key,
                    r.entry.location,
                    r.new_name,
                    r.entry.password,
                    r.packed,
                    fix_drift_first=bool(r.state.drift) and not settle and not r.packed,
                    settle_only=settle,
                )
            )
        self.busy = "renaming"
        self._enable()
        self.status.setText("Renaming…")
        handle = self.jobs.submit("Renaming recordings", rename_recordings, jobs)
        handle.succeeded.connect(self._renamed)
        handle.failed.connect(self._failed)
        self._handles.append(handle)
        self._current = handle

    def _order_rows(self, wanted) -> list[int]:
        """Row indexes in the order the plan wants (a chain goes first)."""
        renames = [r for r, s in wanted if not s]
        position = {id(r): n for n, r in enumerate(self.rows)}
        ordered = [position[id(renames[i])] for i in self.plan.order]
        settle = [position[id(r)] for r, s in wanted if s]
        return settle + ordered

    def _stop(self) -> None:
        if self._current is not None:
            self._current.cancel()

    def _failed(self, message: UserMessage) -> None:
        self.busy = ""
        self.status.setText("Nothing was renamed.")
        self._show(message)
        self._recheck()

    def _show(self, message: UserMessage) -> None:
        MessageDialog(message, self).open()

    def _renamed(self, outcome) -> None:
        self.busy = ""
        by_key = {r.entry.key: r for r in self.rows}
        moved, units, failed = [], [], []
        for item in outcome.results:
            row = by_key.get(item.key)
            if row is None:
                continue
            row.result = item
            if item.ok:
                if item.new_location:
                    moved.append((item.key, item.new_location))
                if item.steps:
                    protected = bool(row.entry.password) or bool(
                        row.entry.details is not None and row.entry.details.password_protected
                    )
                    # The password is NOT kept: undo asks for it again.
                    units.append(
                        ops.UndoUnit(
                            location_key(item.new_location), item.steps, "", protected=protected
                        )
                    )
            else:
                failed.append(row)
        for key, location in outcome.unreported:
            moved.append((key, location))
        done = len(moved)
        text = f"{_plural(done, 'recording')} renamed." if done else "Nothing was renamed."
        if failed:
            text += f" {_plural(len(failed), 'recording')} could not be renamed (see the list)."
        if outcome.stopped:
            text += " Stopped: what was already renamed stays renamed."
        if outcome.unfinished:
            text += (
                " One recording was interrupted by the stop. Nothing is lost: the next "
                "rename of it puts it back the way it was."
            )
        if outcome.unreported:
            text += " One recording was renamed just as you stopped; it cannot be undone from here."
        if any(item.put_back for item in outcome.results):
            text += " An earlier unfinished rename was put back as it was first."
        self.status.setText(text)
        self._done = True
        for n, r in enumerate(self.rows):
            if r.result is not None and not r.result.ok:
                self.table.item(n, COL_NOTE).setText(r.result.message.text)
        if units:
            self.batch = RenameBatch(tuple(units))
            self.renamed.emit(self.batch)
        if moved:
            self.relocated.emit(moved)
        for r in failed:
            self._show(r.result.message)
            break
        self._enable()

    # -- undo ------------------------------------------------------------------------
    def _usable_units(self) -> list[ops.UndoUnit] | None:
        """The units that can be undone now. Units whose recording is no
        longer in the list are dropped from the batch; None (with the reason
        shown) when one is open in a signal viewer."""
        units = list(self.batch.units)
        if self._listed is not None:
            listed = set(self._listed())
            kept = [u for u in units if u.key in listed]
            if len(kept) != len(units):
                units = kept
                self.batch = RenameBatch(tuple(kept)) if kept else None
                self.renamed.emit(self.batch)
        open_now = set(self._in_use_now())
        if any(u.key in open_now for u in units):
            self.status.setText(
                "Not undone: a recording is open in a signal viewer. Close that window first."
            )
            return None
        return units

    def start_undo(self) -> None:
        """Undo the last rename: look first, ask, then do it."""
        if self.busy or not self.batch:
            return
        units = self._usable_units()
        if not units:
            if units is not None:
                self.status.setText("Nothing left to undo: the recordings are no longer listed.")
            self._enable()
            return
        requests = []
        for unit in units:
            skip = {}
            last = unit.steps[-1]
            report = last.report
            if report is not None:
                skip = dict(getattr(report, "files", {}) or {})
            requests.append(ops.StateRequest(unit.key, last.new_path, last.kind == "archive", skip))
        self.busy = "looking"
        self._enable()
        self.status.setText("Looking at the recordings…")
        handle = self.jobs.submit("Looking at the recordings", check_rename_state, requests)
        handle.succeeded.connect(self._undo_looked)
        handle.failed.connect(self._failed)
        self._handles.append(handle)

    def _undo_looked(self, result) -> None:
        states, _folders = result
        self.busy = ""
        self._enable()
        units = self._usable_units()
        if not units:
            return
        blocked = [
            u for u in units if states.get(u.key) is not None and states[u.key].recently_written
        ]
        if blocked:
            self.status.setText(
                "Not undone: a recording was written to in the last few minutes. Wait until "
                "the writing is finished, then try again."
            )
            return
        count = len(units)
        if not self.confirm(
            f"Put {_plural(count, 'recording')} back to the old name?",
            "Only the names are changed back, in every place they are kept. Anything added "
            "to the recordings since is kept.",
        ):
            return
        if (units := self._usable_units()) is None or not units:  # a viewer opened meanwhile
            return
        todo, skipped = [], 0
        for u in units:
            password = ""
            if u.protected:
                password = self.ask_password(recording_name(u.steps[-1].new_path), False)
                if not password:
                    skipped += 1
                    continue
            todo.append(ops.UndoUnit(u.key, u.steps, password, u.protected))
        if not todo:
            self.status.setText("Not undone: the password is needed.")
            return
        self._skipped = skipped
        self.busy = "undoing"
        self._enable()
        self.status.setText("Undoing…")
        handle = self.jobs.submit("Undoing the rename", undo_renames, todo)
        handle.succeeded.connect(self._undone)
        handle.failed.connect(self._failed)
        self._handles.append(handle)
        self._current = handle

    def _undone(self, result) -> None:
        results, stopped = result
        self.busy = ""
        moved, failed = [], []
        by_key = {u.key: u for u in (self.batch.units if self.batch else ())}
        for item in results:
            unit = by_key.get(item.key)
            if item.ok:
                moved.append((item.key, item.new_location))
                by_key.pop(item.key, None)
                continue
            failed.append(item)
            if unit is not None and item.steps:
                # Part of it was undone: keep only the steps still to do,
                # under the recording's present place.
                by_key.pop(item.key, None)
                if item.new_location and item.new_location != unit.steps[-1].new_path:
                    moved.append((item.key, item.new_location))
                key = location_key(item.new_location) if item.new_location else item.key
                by_key[key] = ops.UndoUnit(key, item.steps, "", unit.protected)
        left = list(by_key.values())
        self.batch = RenameBatch(tuple(left)) if left else None
        text = f"{_plural(len(moved), 'recording')} put back to the old name."
        if failed:
            text += f" {_plural(len(failed), 'recording')} could not be put back."
        if stopped:
            text += " Stopped: the rest is still renamed."
        if self._skipped:
            text += f" {_plural(self._skipped, 'recording')} left because no password was given."
            self._skipped = 0
        self.status.setText(text)
        if moved:
            self.relocated.emit(moved)
        self.renamed.emit(self.batch)
        if failed:
            self._show(failed[0].message)
        self._enable()
