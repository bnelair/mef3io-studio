"""Explore's "Make new recordings…": the same changes in many recordings at
once, each saved as a NEW, checked recording (mef3io-studio#16).

One window, five steps: which recordings, which channels (a saved choice or
the picker), other details, passwords, names and folder. It then runs the
anonymizer's unattended batch (``model.pipeline`` + ``workers.tasks.run_batch``:
a journal that survives a crash, Stop, progress, free-space and name checks
before anything is written), with one copy with changes per recording.

Rules the window keeps:

* The originals are only read; every result is a new recording that mef3io
  checks against its original before it gets its name.
* Passwords are typed here and handed to the batch task; the window keeps
  none of them (the form is emptied when the batch starts and when the window
  closes). Saved choices hold channel names only, in the app's private data
  folder (``model.presets``), never next to a recording.
* The window never calls mef3io: checks and copies run as tasks.
* Names, channels and details are never logged.

Plain wording (docs/ui-language.md).
"""

from __future__ import annotations

import os
from dataclasses import replace

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
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..anonymizer.page import channel_selection_for
from ..anonymizer.picker import ChannelPicker
from ..common.pages import label as _label
from ..common.pages import scaled_font
from ..common.passwords import PasswordsStep
from ..explorer.edit_details import SESSION_FIELDS, DetailBox
from ..explorer.panels import _scroll
from ..explorer.state import RecordingEntry
from ..model import anonymize, makenew, pipeline, presets
from ..model import journal as journal_
from ..model.channels import ChannelSelection
from ..model.recording import ACCESS_OPEN
from ..model.timefmt import format_count
from ..workers.jobs import JobManager
from ..workers.tasks import BatchState
from .run import BatchRunMixin

STEP_RECORDINGS, STEP_CHANNELS, STEP_DETAILS, STEP_PASSWORDS, STEP_WHERE, STEP_RUN = range(6)
STEP_TITLES = (
    "Recordings",
    "Channels",
    "Details",
    "Passwords",
    "Names and folder",
    "Making them",
)

INTRO = (
    "Make the same changes in many recordings at once. Each one is saved as a new recording "
    "next to your choice of folder and checked against its original. The originals are never "
    "changed."
)
NEEDS_FULL_ACCESS = "needs its full-access password: open it in Explore with that password first"
STORE_PROBLEM = (
    "The saved choices could not be read or written. Nothing was changed. Check that the "
    "app's data folder is available and has room, then try again."
)
UNREADABLE_STORE = "The saved choices cannot be read, so nothing was exported. Nothing was changed."
SHARE_NOTE = (
    "A saved-choices file holds channel names. Channel names can identify a patient, so share "
    "it only where you would share the recordings."
)
DETAILS_NOTE = (
    "The text you type here goes into every new recording and replaces what each one has. A "
    "box you leave empty keeps what each recording has. To remove a detail, use Edit details "
    "on one recording."
)
PROTECTED_NOTE = (
    "Some of these recordings are password-protected. The new recordings will NOT be, unless "
    "you protect them with two new passwords here. The originals' passwords are never carried "
    "over."
)
PLAIN_COPY = (
    "Leave out a channel, change a detail or add passwords. Otherwise the new recordings would "
    "just be copies."
)
SHOWN_CHANNELS = 6


def _list(names) -> str:
    names = list(names)
    text = ", ".join(names[:SHOWN_CHANNELS])
    if len(names) > SHOWN_CHANNELS:
        text += f" and {len(names) - SHOWN_CHANNELS} more"
    return text


def source_of(entry: RecordingEntry) -> makenew.Source:
    """One listed recording as the batch's plain-data source (also used by
    "Cut into pieces")."""
    return makenew.Source(
        entry.key,
        entry.location,
        entry.name,
        tuple(c.name for c in entry.details.channels),
        {c.name: c.bytes for c in entry.details.channels},
        entry.password,
    )


class MakeNewDialog(BatchRunMixin, QDialog):
    """Make new recordings from the chosen ones."""

    STEP_WHERE = STEP_WHERE
    STEP_RUN = STEP_RUN

    #: Some new recordings were saved: (their folders, the full-access
    #: password they were protected with or "").
    open_requested = Signal(list, str)

    def __init__(
        self,
        entries: list[RecordingEntry],
        jobs: JobManager,
        store: presets.PresetStore | None = None,
        keep_awake: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.jobs = jobs
        self.store = store or presets.PresetStore()
        self.keep_awake = keep_awake
        self.entries = [e for e in entries if e.details is not None]
        self.handle = None
        self.step = STEP_RECORDINGS
        self.selection = ChannelSelection({})
        self._selection_keys: tuple[str, ...] = ()
        self.applied: presets.ChannelPreset | None = None
        self._applying = False
        self.key_problem = False
        self.last_problem = ""
        self.batch: pipeline.Batch | None = None
        self.results: dict[str, anonymize.CopyResult] = {}
        self.leftovers: list[anonymize.Leftover] = []
        self.state: BatchState | None = None
        self.stopped = False
        self.finished_run = False
        self._busy = False
        self._open_password = ""
        self._journal = None
        self._preflight_problems: list[str] = []
        self.setWindowTitle("Make new recordings")
        em = self.fontMetrics().height()
        self.setMinimumWidth(em * 44)
        self.resize(em * 56, em * 44)

        heading = QLabel("Make new recordings", self)
        heading.setFont(scaled_font(self, 1.25, bold=True))
        self.intro = _label(INTRO, muted=True)
        self.step_label = _label(bold=True)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(_scroll(self._build_recordings()))
        self.stack.addWidget(_scroll(self._build_channels()))
        self.stack.addWidget(_scroll(self._build_details()))
        self.stack.addWidget(_scroll(self._build_passwords()))
        self.stack.addWidget(_scroll(self._build_where()))
        self.stack.addWidget(_scroll(self._build_run()))

        self.problem = _label()
        self.problem.setObjectName("Bad")
        self.back_button = QPushButton("Back", self)
        self.back_button.clicked.connect(self.back)
        self.next_button = QPushButton("Next", self)
        self.next_button.clicked.connect(self.next)
        self.start_button = QPushButton("Check and make the new recordings", self)
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
        self.go(STEP_RECORDINGS)

    # -- step 1: recordings ----------------------------------------------------
    def _build_recordings(self) -> QWidget:
        page = QWidget(self)
        self.recording_list = QListWidget(page)
        self.recording_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for entry in self.entries:
            usable = entry.copyable
            text = entry.name if usable else f"{entry.name} ({NEEDS_FULL_ACCESS})"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, entry.key)
            if usable:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.recording_list.addItem(item)
        self.recording_list.itemChanged.connect(lambda _item: self._changed())
        layout = QVBoxLayout(page)
        layout.addWidget(_label("Which recordings?", bold=True))
        layout.addWidget(_label("Tick the recordings to make new ones from.", muted=True))
        layout.addWidget(self.recording_list, 1)
        return page

    def ticked(self) -> list[RecordingEntry]:
        keys = {
            self.recording_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.recording_list.count())
            if self.recording_list.item(i).checkState() == Qt.CheckState.Checked
        }
        return [e for e in self.entries if e.key in keys]

    def sources(self) -> list[makenew.Source]:
        return [source_of(e) for e in self.ticked()]

    # -- step 2: channels ---------------------------------------------------------
    def _build_channels(self) -> QWidget:
        page = QWidget(self)
        self.preset_combo = QComboBox(page)
        self.preset_combo.setMinimumContentsLength(18)
        self.apply_button = QPushButton("Use this saved choice", page)
        self.apply_button.clicked.connect(
            lambda: self.apply_preset(self.preset_combo.currentText())
        )
        self.save_button = QPushButton("Save this choice…", page)
        self.save_button.clicked.connect(self._ask_save_preset)
        self.delete_button = QPushButton("Delete it", page)
        self.delete_button.clicked.connect(self._ask_delete_preset)
        self.share_button = QPushButton("Share…", page)
        self.share_button.clicked.connect(self._ask_export_presets)
        self.import_button = QPushButton("Add shared choices…", page)
        self.import_button.clicked.connect(self._ask_import_presets)
        self.ignore_case = QCheckBox("Ignore capital letters when matching names", page)
        self.ignore_case.toggled.connect(lambda _on: self._ignore_case_toggled())
        self.save_left_out = QCheckBox("Save the channels left out instead of the ones kept", page)
        self.preset_note = _label()
        self.preset_note.setObjectName("Caution")
        self.preset_note.setVisible(False)
        bar = QHBoxLayout()
        bar.addWidget(self.preset_combo, 1)
        bar.addWidget(self.apply_button)
        bar.addWidget(self.delete_button)
        more = QHBoxLayout()
        more.addWidget(self.save_button)
        more.addWidget(self.share_button)
        more.addWidget(self.import_button)
        more.addStretch(1)
        self.picker = ChannelPicker(page)
        self.picker.changed.connect(self._picker_edited)
        self.preview = QTableWidget(0, 4, page)
        self.preview.setHorizontalHeaderLabels(
            ["Recording", "Keeps", "Leaves out", "Channels it does not have"]
        )
        self.preview.verticalHeader().hide()
        self.preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preview.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.preview.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.preview.setMinimumHeight(self.fontMetrics().height() * 7)
        self.channel_problem = _label()
        self.channel_problem.setObjectName("Bad")
        layout = QVBoxLayout(page)
        layout.addWidget(_label("Which channels?", bold=True))
        layout.addWidget(_label("Saved choices", bold=True))
        layout.addLayout(bar)
        layout.addLayout(more)
        layout.addWidget(self.ignore_case)
        layout.addWidget(self.save_left_out)
        layout.addWidget(self.preset_note)
        layout.addWidget(_label(SHARE_NOTE, muted=True))
        layout.addWidget(self.picker, 3)
        layout.addWidget(_label("What each recording gets", bold=True))
        layout.addWidget(self.preview, 2)
        layout.addWidget(self.channel_problem)
        self._fill_presets()
        return page

    def _prepare_channels(self) -> None:
        keys = tuple(e.key for e in self.ticked())
        if keys == self._selection_keys:
            return
        previous = self.selection if self._selection_keys else None
        self.selection, notice = channel_selection_for(self.ticked(), previous)
        self._selection_keys = keys
        self.applied = None
        self.picker.set_selection(self.selection, len(keys), notice)

    def _guarded(self, call, default):
        """Every access to the saved choices goes through here: when the
        app's sealing key cannot be read, say so plainly (nothing is lost or
        replaced) and give ``default``."""
        self.last_problem = ""
        try:
            return call()
        except journal_.KeyUnavailable:
            self.last_problem = journal_.KEY_PROBLEM
            self.preset_note.setText(journal_.KEY_PROBLEM)
            self.preset_note.setVisible(True)
            self.key_problem = True
            return default
        except OSError:
            self.last_problem = STORE_PROBLEM
            self.preset_note.setText(STORE_PROBLEM)
            self.preset_note.setVisible(True)
            return default

    def _aside_note(self) -> str:
        """ "" or the plain notice that an unreadable store was set aside."""
        if not self.store.moved_aside:
            return ""
        return (
            "The file of saved choices could not be read, so it was set aside (not deleted) "
            f"as {self.store.moved_aside} in the app's own data folder, and a new one was "
            "started."
        )

    def _fill_presets(self, select: str = "") -> None:
        names = self._guarded(self.store.names, [])
        self.preset_combo.clear()
        self.preset_combo.addItems(names)
        if select in names:
            self.preset_combo.setCurrentText(select)
        have = bool(names)
        for widget in (self.preset_combo, self.apply_button, self.delete_button, self.share_button):
            widget.setEnabled(have)
        self.save_button.setEnabled(not self.key_problem)
        self.import_button.setEnabled(not self.key_problem)

    def apply_preset(self, name: str) -> tuple[str, ...]:
        """Set the picker to a saved choice. Returns the channels of it that
        none of the chosen recordings has (also shown, never an error)."""
        preset = self._guarded(lambda: self.store.get(name), None)
        if preset is None or not self._selection_keys:
            return ()
        self._applying = True
        try:
            none_has = presets.apply_to_selection(
                preset, self.selection, self.ignore_case.isChecked()
            )
            self.picker.set_selection(self.selection, len(self._selection_keys))
        finally:
            self._applying = False
        self.applied = preset
        self.preset_note.setText(
            f"{format_count(len(none_has), 'channel')} of this saved choice "
            f"{'is' if len(none_has) == 1 else 'are'} in none of the chosen recordings: "
            f"{_list(none_has)}."
            if none_has
            else ""
        )
        self.preset_note.setVisible(bool(none_has))
        self._changed()
        return none_has

    def _picker_edited(self) -> None:
        """A hand edit of the picker ends "this saved choice is applied"
        (the missing-channels column would describe a choice no longer
        shown)."""
        if not self._applying:
            self.applied = None
        self._changed()

    def _ignore_case_toggled(self) -> None:
        if self.applied is not None:
            self.apply_preset(self.applied.name)
        else:
            self._changed()

    def save_preset(self, name: str) -> str:
        """Save the picker's choice under ``name``. Returns "" or a plain
        message (why not, or a notice)."""
        try:
            preset = presets.preset_from_selection(
                name, self.selection, self.save_left_out.isChecked()
            )
        except presets.PresetError as exc:
            return str(exc)

        def save() -> str:
            self.store.add(preset)
            self._fill_presets(preset.name)
            return self._aside_note()

        try:
            result = self._guarded(save, None)
            return self.last_problem if result is None else result
        except presets.PresetError as exc:
            return str(exc)

    def delete_preset(self, name: str) -> bool:
        removed = self._guarded(lambda: self.store.remove(name), False)
        self._fill_presets()
        return removed

    def export_presets(self, path: str) -> str:
        """Write all saved choices to a file the person chose. "" or why not."""
        found = self._guarded(lambda: None if self.store.unreadable else self.store.load(), None)
        if found is None:
            return self.last_problem or UNREADABLE_STORE
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(presets.export_text(found))
        except OSError:
            return "The file could not be written. Choose another place."
        return ""

    def import_presets(self, path: str) -> str:
        """Add the saved choices of a shared file. "" or why not."""
        try:
            if os.path.getsize(path) > presets.MAX_FILE_BYTES:
                return "This file is too large to be a file of saved choices."
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return "The file could not be read."
        except ValueError:
            return "This file does not hold saved channel choices."
        try:
            added = self._guarded(lambda: self.store.import_text(text), None)
        except presets.PresetError as exc:
            return str(exc)
        if added is None:
            return self.last_problem
        self._fill_presets(added[0] if added else "")
        return self._aside_note()

    def _ask_save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Save this choice", "Name of the saved choice:")
        if ok:
            self._tell(self.save_preset(name))

    def _ask_delete_preset(self) -> None:
        name = self.preset_combo.currentText()
        if name and (
            QMessageBox.question(self, "Delete the saved choice", "Delete this saved choice?")
            == QMessageBox.StandardButton.Yes
        ):
            self.delete_preset(name)

    def _ask_export_presets(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save the saved choices as a file",
            "channel-choices.json",
            "Saved choices (*.json)",
        )
        if path:
            self._tell(self.export_presets(path))

    def _ask_import_presets(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Add shared choices", "", "Saved choices (*.json)"
        )
        if path:
            self._tell(self.import_presets(path))

    def _tell(self, text: str) -> None:
        if text:
            QMessageBox.information(self, "Saved choices", text)

    def rows(self) -> list[makenew.Row]:
        return makenew.rows(
            self.sources(), self.selection, self.applied, self.ignore_case.isChecked()
        )

    def _fill_preview(self) -> None:
        rows = self.rows()
        total = len(rows)
        self.preview.setRowCount(total)
        for n, (row, entry) in enumerate(zip(rows, self.ticked(), strict=True)):
            cells = (
                f"{n + 1} of {total}: {entry.name}",
                format_count(len(row.kept), "channel"),
                format_count(len(row.left_out), "channel"),
                _list(row.missing),
            )
            for col, text in enumerate(cells):
                self.preview.setItem(n, col, QTableWidgetItem(text))
        empty = [n + 1 for n, r in enumerate(rows) if r.empty]
        self.channel_problem.setText(
            "These recordings would keep no channel, so they cannot be made: "
            f"{', '.join(str(n) for n in empty)} (numbers as in the first column). "
            "Choose other channels or leave those recordings out."
            if empty
            else ""
        )
        self.channel_problem.setVisible(bool(empty))

    # -- step 3: details --------------------------------------------------------------
    def _build_details(self) -> QWidget:
        page = QWidget(self)
        self.change_details = QCheckBox("Change patient details or the recording description", page)
        self.change_details.toggled.connect(self._details_toggled)
        self.boxes: dict[str, DetailBox] = {}
        self.form = QWidget(page)
        form = QFormLayout(self.form)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        for label, name, multiline in SESSION_FIELDS:
            box = DetailBox(name, multiline, self.form)
            box.box.setPlaceholderText("Empty: keep what each recording has")
            box.edited.connect(self._changed)
            self.boxes[name] = box
            form.addRow(f"{label}:", box)
        self.form.setVisible(False)
        layout = QVBoxLayout(page)
        layout.addWidget(_label("Other details (optional)", bold=True))
        layout.addWidget(
            _label("Nothing here is needed. Skip this step to keep every detail.", muted=True)
        )
        layout.addWidget(self.change_details)
        layout.addWidget(_label(DETAILS_NOTE, muted=True))
        layout.addWidget(self.form)
        layout.addStretch(1)
        return page

    def _details_toggled(self, shown: bool) -> None:
        self.form.setVisible(shown)
        self._changed()

    def details(self) -> dict:
        """mef3io's details for what was typed (nothing unless the box is ticked)."""
        if not self.change_details.isChecked():
            return {}
        return makenew.details_from({n: b.text() for n, b in self.boxes.items()})

    # -- step 4: passwords --------------------------------------------------------------
    def _build_passwords(self) -> QWidget:
        page = QWidget(self)
        self.protected_note = _label(PROTECTED_NOTE)
        self.protected_note.setObjectName("Caution")
        self.passwords = PasswordsStep(page, what="the new recordings", none_label="No password")
        self.passwords.changed.connect(self._changed)
        layout = QVBoxLayout(page)
        layout.addWidget(_label("Password protection (optional)", bold=True))
        layout.addWidget(self.protected_note)
        layout.addWidget(self.passwords)
        layout.addStretch(1)
        return page

    # -- step 5: names and folder -----------------------------------------------------
    def _build_where(self) -> QWidget:
        page = QWidget(self)
        self.naming = QComboBox(page)
        self.naming.addItem("Keep each name and add a text after it", makenew.NAME_ADDED)
        self.naming.addItem("A new name and a number", makenew.NAME_NUMBERED)
        self.naming_text = QLineEdit("-new", page)
        self.naming.currentIndexChanged.connect(self._naming_changed)
        self.naming_text.textChanged.connect(self._changed)
        self.folder = ""
        self.folder_label = _label(muted=True)
        self.folder_button = QPushButton("Choose the folder…", page)
        self.folder_button.clicked.connect(self._choose_folder)
        self.names_preview = QListWidget(page)
        self.names_preview.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.names_preview.setMinimumHeight(self.fontMetrics().height() * 6)
        self.name_hint = _label(
            "If an old name contains the patient's name, choose a new name and a number.",
            muted=True,
        )
        where = QFormLayout()
        where.addRow("Names:", self.naming)
        where.addRow("Text:", self.naming_text)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(self.folder_button)
        where.addRow("Saved in:", folder_row)
        layout = QVBoxLayout(page)
        layout.addWidget(_label("Names and folder", bold=True))
        layout.addLayout(where)
        layout.addWidget(self.name_hint)
        layout.addWidget(_label("The new recordings will be called", bold=True))
        layout.addWidget(self.names_preview, 1)
        return page

    def _naming_changed(self) -> None:
        added = self.naming.currentData() == makenew.NAME_ADDED
        self.naming_text.blockSignals(True)
        self.naming_text.setText("-new" if added else "Recording-")
        self.naming_text.blockSignals(False)
        self._changed()

    def new_names(self) -> list[str]:
        return makenew.new_names(self.sources(), self.naming.currentData(), self.naming_text.text())

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose where to save the new recordings", self.folder
        )
        if folder:
            self.folder = folder
            self._changed()

    # -- moving between steps ------------------------------------------------------------
    def go(self, step: int) -> None:
        self.step = step
        self.stack.setCurrentIndex(step)
        self.step_label.setText(f"Step {step + 1} of {len(STEP_TITLES)}: {STEP_TITLES[step]}")
        if step == STEP_CHANNELS:
            self._prepare_channels()
        if step == STEP_PASSWORDS:
            self.protected_note.setVisible(
                self._any_protected() and self.passwords.passwords() is None
            )
        self._changed()

    def _any_protected(self) -> bool:
        return any(e.details.access != ACCESS_OPEN for e in self.ticked())

    def back(self) -> None:
        if (
            self.step in (STEP_CHANNELS, STEP_DETAILS, STEP_PASSWORDS, STEP_WHERE)
            and not self._busy
        ):
            self.go(self.step - 1)

    def next(self) -> None:
        if self.step < STEP_WHERE and not self.blocker():
            self.go(self.step + 1)

    def blocker(self) -> str:
        """Why the step cannot be left ("" when it can)."""
        step = self.step
        if step == STEP_RECORDINGS:
            return "" if self.ticked() else "Tick at least one recording."
        if step == STEP_CHANNELS:
            if not self.selection.total:
                return "Choose at least one recording first."
            empty = self.selection.recordings_left_empty()
            if not self.selection.kept:
                return "Keep at least one channel."
            if empty:
                return "Some recordings would keep no channel. Choose other channels."
            return ""
        if step == STEP_DETAILS:
            return makenew.details_problem(self.details())
        if step == STEP_PASSWORDS:
            return self.passwords.problem_text()
        if step == STEP_WHERE:
            sources = self.sources()
            found = makenew.name_problems(sources, self.new_names(), self.folder)
            if found:
                return found[0]
            if makenew.nothing_to_do(self.selection, self.details(), self.passwords.passwords()):
                return PLAIN_COPY
        return ""

    def _changed(self, *_args) -> None:
        if not hasattr(self, "problem"):
            return
        if self.step == STEP_CHANNELS and self._selection_keys:
            self._fill_preview()
        if self.step == STEP_PASSWORDS:
            self.protected_note.setVisible(
                self._any_protected() and self.passwords.passwords() is None
            )
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
        self.back_button.setEnabled(self.step > STEP_RECORDINGS)
        self.next_button.setEnabled(not text)
        self.start_button.setEnabled(not text and not self._busy)
        self.close_button.setEnabled(not self._busy)

    # -- the batch -----------------------------------------------------------------------
    def make_batch(self) -> pipeline.Batch:
        sources = self.sources()
        names = self.new_names()
        requests = makenew.requests_for(
            sources,
            self.selection,
            names,
            self.folder,
            self.details() or None,
            self.passwords.passwords(),
        )
        estimates = makenew.estimates_for(sources, self.selection)
        originals = {s.key: (s.name, 0, 0) for s in sources}
        batch = pipeline.plan_batch(
            pipeline.FOLDERS, requests, self.folder, "", estimates, originals
        )
        return replace(batch, keep_awake=self.keep_awake)


__all__ = ["MakeNewDialog"]
