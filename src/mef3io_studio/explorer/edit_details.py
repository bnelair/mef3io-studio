"""Explore's "Edit details (makes a new copy)": change the patient details
and the descriptions of a recording by saving a NEW recording.

The original is never touched: the changes go into a new, checked copy next
to it (``model/changes.py``, mef3io's ``transform_session``), optionally with
new passwords. Times cannot be changed, so they are shown but not editable.
Patient details stay hidden until "Show" is chosen for the recording, exactly
as in the Patient details tab. The window never calls mef3io: the check and
the copy run as tasks in a helper process.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import tzinfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..common.activity import JobProgressWidget
from ..common.dialogs import TechnicalDetails
from ..common.pages import label as _label
from ..common.pages import scaled_font
from ..common.passwords import PasswordsStep
from ..model import changes
from ..model.anonymize import (
    destination_for,
    estimate_bytes,
    inside_any_recording,
    name_problem,
    review_destination,
)
from ..model.recording import ACCESS_FULL, ACCESS_OPEN
from ..model.refusals import DETAIL_LABELS
from ..model.timefmt import format_size, format_time
from ..workers.jobs import JobManager
from ..workers.tasks import check_changes, make_changes
from .panels import _scroll
from .state import RecordingEntry

HIDDEN_HINT = "Hidden until you choose Show"

INTRO = (
    "Nothing in the original recording is changed. The details you change are saved in a "
    "new recording next to it, which the app then checks against the original. Leave a box "
    "as it is to keep what is stored; empty a box to remove its text."
)
TIMES_NOTE = (
    "Dates and times cannot be changed. The new recording keeps every time exactly as it is stored."
)
NEEDS_FULL_ACCESS = (
    "Changing details needs the full-access password. Open this recording in Explore with it first."
)
ROOM_TIP = (
    "How many more characters fit. Letters with accents and other special characters take "
    "more room. A text that is too long is never cut: it must be shortened."
)
PROTECTED_NOTE = (
    "This recording is password-protected. The new recording will NOT be, unless you "
    "protect it with two new passwords here."
)

#: (everyday label, mef3io detail, one line or several)
SESSION_FIELDS = (
    ("First name", "subject_name_1", False),
    ("Last name", "subject_name_2", False),
    ("Patient ID", "subject_id", False),
    ("Place of the recording", "recording_location", False),
    ("Recording description", "session_description", True),
)
#: The patient attribute that holds each one's current text.
CURRENT_ATTRS = {
    "subject_name_1": "first_name",
    "subject_name_2": "last_name",
    "subject_id": "patient_id",
    "recording_location": "location",
    "session_description": "recording_description",
}
COL_CHANNEL, COL_DESCRIPTION, COL_REFERENCE = range(3)


class DetailBox(QWidget):
    """One detail: a box, and how much more text fits. ``initial`` is what
    is stored (None while it is hidden): the box tells what changed."""

    edited = Signal()

    def __init__(self, name: str, multiline: bool, parent=None):
        super().__init__(parent)
        self.name = name
        self.initial: str | None = None
        if multiline:
            self.box = QPlainTextEdit(self)
            self.box.setFixedHeight(self.fontMetrics().height() * 4)
            self.box.textChanged.connect(self._changed)
        else:
            self.box = QLineEdit(self)
            self.box.textChanged.connect(self._changed)
        self.room = QLabel("", self)
        self.room.setObjectName("Muted")
        self.room.setToolTip(ROOM_TIP)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.box)
        layout.addWidget(self.room)
        self._changed()

    def text(self) -> str:
        return self.box.toPlainText() if isinstance(self.box, QPlainTextEdit) else self.box.text()

    def set_text(self, text: str) -> None:
        if isinstance(self.box, QPlainTextEdit):
            self.box.setPlainText(text)
        else:
            self.box.setText(text)

    def set_stored(self, value: str | None) -> None:
        """Show what is stored (None = hidden: the box stays empty and an
        empty box means "keep")."""
        self.initial = value
        placeholder = HIDDEN_HINT if value is None else ""
        self.box.setPlaceholderText(placeholder)
        if value is not None and not self.text():
            self.set_text(value)

    def too_long(self) -> bool:
        return changes.remaining(self.text(), self.name) < 0

    def _changed(self) -> None:
        left = changes.remaining(self.text(), self.name)
        if left < 0:
            self.room.setText(f"Too long by {-left}")
            self.room.setObjectName("Bad")
        else:
            self.room.setText(f"{left} left")
            self.room.setObjectName("Muted")
        self.room.style().unpolish(self.room)
        self.room.style().polish(self.room)
        self.edited.emit()


class EditDetailsDialog(QDialog):
    """Change details, save a new recording."""

    #: The new recording was saved and checked: (its folder, the full-access
    #: password it was protected with, or "").
    open_requested = Signal(str, str)
    #: "Show" was chosen here: the Explore list must follow.
    reveal_requested = Signal()

    def __init__(
        self,
        entry: RecordingEntry,
        jobs: JobManager,
        zone: tzinfo,
        parent=None,
    ):
        super().__init__(parent)
        self.entry = entry
        self.jobs = jobs
        self.zone = zone
        self.handle = None
        self.request: changes.ChangeRequest | None = None
        self.result: changes.ChangeResult | None = None
        self.folder = os.path.dirname(entry.location)
        self._generation = 0
        self._busy = False
        self._stopped = False
        self._failure = None
        self._open_password = ""
        self._pending: changes.ChangeRequest | None = None
        self._closed = False
        self.setWindowTitle("Edit details (makes a new copy)")
        em = self.fontMetrics().height()
        self.setMinimumWidth(em * 40)
        self.resize(em * 46, em * 40)
        details = entry.details

        heading = QLabel("Edit details (makes a new copy)", self)
        heading.setFont(scaled_font(self, 1.25, bold=True))
        self.intro = _label(INTRO, muted=True)

        # -- hidden or shown --------------------------------------------------
        self.hidden_note = _label(
            "The patient details of this recording are hidden. Choose Show to see and edit "
            "what is stored. Without it you can still type a new text for a box, and boxes "
            "you leave empty are kept."
        )
        self.hidden_note.setObjectName("Caution")
        self.show_button = QPushButton("Show the current details", self)
        self.show_button.clicked.connect(self._show_current)
        self.access_note = _label(NEEDS_FULL_ACCESS)
        self.access_note.setObjectName("Bad")

        # -- the details -------------------------------------------------------
        self.boxes: dict[str, DetailBox] = {}
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        for label, name, multiline in SESSION_FIELDS:
            box = DetailBox(name, multiline, self)
            box.edited.connect(self._update)
            self.boxes[name] = box
            form.addRow(f"{label}:", box)
        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(
            ["Channel", "Description", "Reference (a new text replaces it)"]
        )
        self.table.verticalHeader().hide()
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_CHANNEL, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_DESCRIPTION, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_REFERENCE, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(em * 8)
        self._stored_descriptions: dict[str, str | None] = {}
        for channel in details.channels if details is not None else ():
            row = self.table.rowCount()
            self.table.insertRow(row)
            name_item = QTableWidgetItem(channel.name)
            name_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.setItem(row, COL_CHANNEL, name_item)
            self.table.setItem(row, COL_DESCRIPTION, QTableWidgetItem(""))
            self.table.setItem(row, COL_REFERENCE, QTableWidgetItem(""))
        self.table.itemChanged.connect(lambda _item: self._update())

        # -- times: shown, not editable ------------------------------------------
        self.times = _label(self._times_text())
        self.times_note = _label(TIMES_NOTE, muted=True)

        # -- passwords ----------------------------------------------------------------
        self.protected_note = _label(PROTECTED_NOTE)
        self.protected_note.setObjectName("Caution")
        self.passwords = PasswordsStep(self, what="the new recording", none_label="No password")
        self.passwords.changed.connect(self._update)

        # -- where it goes -----------------------------------------------------------
        self.name = QLineEdit(f"{entry.name}-edited", self)
        self.name.textChanged.connect(self._update)
        self.folder_label = _label(muted=True)
        self.folder_button = QPushButton("Choose the folder…", self)
        self.folder_button.clicked.connect(self._choose_folder)
        where = QFormLayout()
        where.addRow("Name of the new recording:", self.name)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(self.folder_button)
        where.addRow("Saved in:", folder_row)
        self.name_hint = _label(
            "If the old name contains the patient's name, choose another one.", muted=True
        )

        # -- messages, progress, buttons -------------------------------------------
        self.problem = _label()
        self.problem.setObjectName("Bad")
        self.busy_note = _label("Working… you can close this window when it is done.", muted=True)
        self.busy_note.setVisible(False)
        self.findings = QVBoxLayout()
        self.progress_slot = QVBoxLayout()
        self.result_label = _label()
        self.result_details = QVBoxLayout()
        self.save_button = QPushButton("Check and save the new recording", self)
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self.check_and_save)
        self.open_button = QPushButton("Open it in Explore", self)
        self.open_button.clicked.connect(self._open_result)
        self.open_button.setVisible(False)
        self.close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self.close_buttons.rejected.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_buttons)

        inner = QWidget(self)
        body = QVBoxLayout(inner)
        body.addWidget(self.hidden_note)
        body.addWidget(self.show_button, 0, Qt.AlignmentFlag.AlignLeft)
        body.addWidget(self.access_note)
        body.addLayout(form)
        body.addWidget(_label("Channels", bold=True))
        body.addWidget(self.table)
        body.addWidget(_label("Times", bold=True))
        body.addWidget(self.times)
        body.addWidget(self.times_note)
        body.addWidget(_label("Password protection", bold=True))
        body.addWidget(self.protected_note)
        body.addWidget(self.passwords)
        body.addWidget(_label("The new recording", bold=True))
        body.addLayout(where)
        body.addWidget(self.name_hint)
        body.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(self.intro)
        layout.addWidget(_scroll(inner), 1)
        layout.addWidget(self.problem)
        layout.addWidget(self.busy_note)
        layout.addLayout(self.findings)
        layout.addLayout(self.progress_slot)
        layout.addWidget(self.result_label)
        layout.addLayout(self.result_details)
        layout.addLayout(buttons)

        self.inputs = inner
        self._fill()
        self._update()

    # -- what is shown ----------------------------------------------------------
    def _access_ok(self) -> bool:
        details = self.entry.details
        return details is not None and details.access in (ACCESS_FULL, ACCESS_OPEN)

    def _times_text(self) -> str:
        details = self.entry.details
        if details is None or not details.channels:
            return ""
        start = min(c.start for c in details.channels)
        end = max(c.end for c in details.channels)
        return f"Starts {format_time(start, self.zone)}. Ends {format_time(end, self.zone)}."

    def _fill(self) -> None:
        """Show what is stored, when it may be shown."""
        details = self.entry.details
        shown = self.entry.revealed and details is not None
        patient = details.patient if details is not None else None
        for name, box in self.boxes.items():
            stored = None
            if shown and patient is not None:
                stored = getattr(patient, CURRENT_ATTRS[name]) or ""
            box.set_stored(stored)
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            channel = self.table.item(row, COL_CHANNEL).text()
            info = next((c for c in details.channels if c.name == channel), None)
            stored = (info.description or "") if shown and info is not None else None
            self._stored_descriptions[channel] = stored
            item = self.table.item(row, COL_DESCRIPTION)
            if stored is not None and not item.text():
                item.setText(stored)
            item.setToolTip("" if stored is not None else HIDDEN_HINT)
        self.table.blockSignals(False)
        self.hidden_note.setVisible(not shown)
        self.show_button.setVisible(not shown)
        self.access_note.setVisible(not self._access_ok())
        self._show_protected_note()

    def _show_protected_note(self) -> None:
        details = self.entry.details
        self.protected_note.setVisible(
            details is not None
            and details.access != ACCESS_OPEN
            and not self.passwords.both.isChecked()
        )

    def _show_current(self) -> None:
        self.entry.revealed = True
        self.reveal_requested.emit()
        self._fill()
        self._update()

    # -- the form as a request -----------------------------------------------------
    def new_details(self) -> dict:
        """What the person changed, as mef3io takes it (only changed fields)."""
        stored = {n: b.initial for n, b in self.boxes.items()}
        typed = {n: b.text() for n, b in self.boxes.items()}
        channels, references = {}, {}
        for row in range(self.table.rowCount()):
            channel = self.table.item(row, COL_CHANNEL).text()
            channels[channel] = self.table.item(row, COL_DESCRIPTION).text()
            references[channel] = self.table.item(row, COL_REFERENCE).text()
        return changes.changed_details(
            stored, typed, self._stored_descriptions, channels, references
        )

    def destination(self) -> str:
        return destination_for(self.folder, self.name.text().strip())

    def make_request(self) -> changes.ChangeRequest:
        return changes.ChangeRequest(
            self.entry.location,
            self.destination(),
            password=self.entry.password,
            output_password=self.passwords.passwords(),
            metadata=self.new_details(),
        )

    def problem_text(self) -> str:
        if not self._access_ok():
            return ""
        long = changes.too_long(self.new_details())
        if long:
            labels = "; ".join(DETAIL_LABELS[n] for n in long)
            return f"Too long, and the app never cuts a text short: {labels}. Make it shorter."
        found = name_problem(self.name.text())
        if found:
            return found
        if not self.folder or not os.path.isdir(self.folder):
            return "Choose a folder that exists."
        dst = self.destination()
        review = review_destination(self.entry.location, dst, self._estimate())
        if review.exists:
            return "A file or folder with that name is already in the chosen folder."
        if review.unfinished_copy_in_the_way:
            return (
                "A folder ending in .partial, left by an earlier run that did not finish, is "
                "in the way. If nothing else is running, delete it, or choose another name."
            )
        if review.inside_source or inside_any_recording(dst, [self.entry.location]):
            return "Choose a folder that is not inside a recording."
        if not review.enough_space:
            return (
                f"There is not enough free space in that folder: about "
                f"{format_size(review.estimated_bytes)} are needed, "
                f"{format_size(review.free_bytes)} are free."
            )
        password = self.passwords.problem_text()
        if password:
            return password
        if not self.new_details() and self.passwords.passwords() is None:
            return "Change at least one detail, or add password protection."
        return ""

    def _estimate(self) -> int | None:
        details = self.entry.details
        if details is None:
            return None
        return estimate_bytes(
            {c.name: c.bytes for c in details.channels}, tuple(c.name for c in details.channels)
        )

    def _update(self, *_args) -> None:
        self.folder_label.setText(self.folder or "No folder chosen")
        self._show_protected_note()
        text = self.problem_text()
        self.problem.setText(text)
        self.problem.setVisible(bool(text))
        self.save_button.setEnabled(self._access_ok() and not text and not self._busy)

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose where to save the new recording", self.folder
        )
        if folder:
            self.folder = folder
            self._update()

    # -- checking, then saving ---------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.inputs.setEnabled(not busy)
        self.close_buttons.setEnabled(not busy)
        self.busy_note.setVisible(busy)
        self._update()

    def _clear(self, layout) -> None:
        while layout.count():
            widget = layout.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def check_and_save(self):
        """mef3io's checks (nothing written), then, if nothing stops it, the
        copy itself."""
        if self._busy or self.problem_text():
            return None
        self._closed = False
        self._pending = self.make_request()
        self._clear(self.findings)
        self._clear(self.progress_slot)
        self._clear(self.result_details)
        self.result_label.setText("")
        self._set_busy(True)
        handle = self.jobs.submit("Checking before saving", check_changes, self._pending)
        generation = self._generation

        def checked(found) -> None:
            request, self._pending = self._pending, None
            if generation != self._generation or request is None:
                return
            if found:
                self._show_findings(found)
                self._set_busy(False)
                return
            self._start(request)

        def failed(message) -> None:
            self._pending = None
            self._show_findings([changes.Finding("", message)])
            self._set_busy(False)

        handle.succeeded.connect(checked)
        handle.failed.connect(failed)
        handle.cancelled.connect(self._check_stopped)
        return handle

    def _check_stopped(self) -> None:
        self._pending = None
        self._set_busy(False)

    def _show_findings(self, found: list) -> None:
        frame = QFrame()
        frame.setObjectName("Warning")
        inner = QVBoxLayout(frame)
        inner.addWidget(_label("This cannot be saved yet", bold=True))
        for f in found:
            inner.addWidget(_label(f"{f.message.title}. {f.message.text}"))
            if f.message.detail:
                inner.addWidget(TechnicalDetails(f.message.detail))
        self.findings.addWidget(frame)

    def _start(self, request: changes.ChangeRequest):
        self.request = request
        self.result = None
        self._stopped = False
        self._failure = None
        handle = self.jobs.submit("Saving a new recording with changes", make_changes, request)
        self.handle = handle
        handle.succeeded.connect(lambda result: setattr(self, "result", result))
        handle.cancelled.connect(lambda: setattr(self, "_stopped", True))
        handle.failed.connect(lambda message: setattr(self, "_failure", message))
        handle.finished.connect(self._finished)
        progress = JobProgressWidget(handle, self)
        self.progress_slot.addWidget(progress)
        return handle

    def stop(self) -> None:
        if self.handle is not None:
            self.handle.cancel()

    def _finished(self) -> None:
        self._clear(self.progress_slot)
        self._set_busy(False)
        result = self.result
        # Closed while it ran: the passwords are not kept, not even to open it.
        output = None if self._closed or not self.request else self.request.output_password
        if self.request is not None:
            self.request = replace(self.request, output_password=None, password="")
        if self._stopped:
            self.result_label.setText(
                "Stopped. Nothing half-made was left behind, and the original was not changed."
            )
        elif result is not None and result.ok:
            self.result_label.setText(
                "The new recording was saved and checked against the original, which was "
                "not changed."
            )
            self._open_password = output[1] if output else ""
            self.open_button.setVisible(True)
            self.save_button.setEnabled(False)
        else:
            message = result.message if result is not None else self._failure
            title = message.title if message else "The new recording could not be made"
            self.result_label.setText(f"{title}. {message.text if message else ''}")
            if message is not None and message.detail:
                self.result_details.addWidget(TechnicalDetails(message.detail))

    def _open_result(self) -> None:
        if self.request is None or self.result is None or not self.result.ok:
            return
        password, self._open_password = self._open_password, ""
        self.open_requested.emit(self.request.destination, password)

    def _wipe(self) -> None:
        """Forget every password and the finished run (memory only)."""
        self.passwords.forget()
        self._open_password = ""
        self._closed = True
        if not self._busy:
            self._pending = None
            self.request = None
            self.result = None
            self.handle = None

    def done(self, result) -> None:
        self._wipe()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._wipe()
        super().closeEvent(event)

    def reject(self) -> None:
        if self._busy:
            return
        super().reject()
