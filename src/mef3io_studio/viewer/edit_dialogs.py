"""The dialogs of the viewer's edit mode: one note or event, the preview of a
save, the preview of an import, and the question when leaving with unsaved
changes (docs/design/viewer.md, "Edit mode").

Widgets only. What is saved, and where, is decided by ``model/annotation_draft``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import tzinfo

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..model import annotation_draft as ad
from ..model import timefmt

WHOLE = "Whole recording"
SAVE_WARNING = (
    "Adding notes to a recording cannot be undone in place. Once added, a note can only be "
    "left out later, in a new copy of the recording."
)


class NoteEditor(QDialog):
    """One note or event: when (and until when), its type, its text, and
    whether it belongs to the whole recording or to a channel."""

    def __init__(
        self,
        zone: tzinfo,
        quick_types: Sequence[str],
        channels: Sequence[str],
        check: Callable[[dict], str] | None = None,
        title: str = "Add a note or event",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.zone = zone
        self._check = check
        self._start_us: int | None = None
        self._start_text = ""
        self._end_us: int | None = None
        self._end_text = ""
        self.start_edit = QLineEdit(self)
        self.start_edit.setToolTip(
            f"The time in the site time zone ({timefmt.zone_label(zone)}), down to milliseconds"
        )
        self.lasts = QCheckBox("It lasts until", self)
        self.end_edit = QLineEdit(self)
        self.end_edit.setEnabled(False)
        self.type_box = QComboBox(self)
        self.type_box.setEditable(True)
        self.type_box.addItems(list(quick_types))
        self.type_box.setToolTip("The type of note. You can also write your own.")
        self.text_edit = QPlainTextEdit(self)
        self.text_edit.setPlaceholderText("What happened (optional)")
        self.text_edit.setFixedHeight(self.fontMetrics().height() * 4)
        self.where_box = QComboBox(self)
        self.where_box.addItem(WHOLE, None)
        for ch in channels:
            self.where_box.addItem(f"Channel {ch}", ch)
        self.error = QLabel("", self)
        self.error.setObjectName("Danger")
        self.error.setWordWrap(True)
        self.ok = QPushButton(
            "Add to the draft" if title.startswith("Add") else "Keep the change", self
        )
        self.ok.setDefault(True)
        self.stop = QPushButton("Stop", self)
        form = QFormLayout()
        form.addRow("Time", self.start_edit)
        form.addRow(self.lasts, self.end_edit)
        form.addRow("Type", self.type_box)
        form.addRow("Text", self.text_edit)
        form.addRow("Belongs to", self.where_box)
        box = QVBoxLayout(self)
        zone_note = QLabel(f"Times are in the site time zone: {timefmt.zone_label(zone)}.", self)
        zone_note.setObjectName("Muted")
        box.addWidget(zone_note)
        box.addLayout(form)
        box.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.stop)
        row.addWidget(self.ok)
        box.addLayout(row)
        self.setMinimumWidth(self.fontMetrics().height() * 30)
        self.lasts.toggled.connect(self.end_edit.setEnabled)
        self.ok.clicked.connect(self._accept)
        self.stop.clicked.connect(self.reject)

    def set_values(
        self,
        time: int,
        end: int | None = None,
        label: str = ad.PLAIN,
        text: str = "",
        channel: str | None = None,
    ) -> None:
        self._start_us = time
        self._start_text = ad.format_local(time, self.zone)
        self.start_edit.setText(self._start_text)
        self._end_us = end
        self._end_text = ad.format_local(end, self.zone) if end else ""
        self.lasts.setChecked(bool(end))
        self.end_edit.setText(self._end_text)
        i = self.type_box.findText(label, Qt.MatchFlag.MatchFixedString)
        if i >= 0:
            self.type_box.setCurrentIndex(i)
        else:
            self.type_box.setEditText(label)
        self.text_edit.setPlainText(text)
        j = self.where_box.findData(channel)
        self.where_box.setCurrentIndex(max(0, j))

    def values(self) -> dict:
        """What was entered (valid only after :meth:`_accept` succeeded)."""
        return {
            "time": self._time(self.start_edit.text(), self._start_text, self._start_us),
            "end": self._time(self.end_edit.text(), self._end_text, self._end_us)
            if self.lasts.isChecked()
            else None,
            "label": " ".join(self.type_box.currentText().split()) or ad.PLAIN,
            "text": self.text_edit.toPlainText().strip(),
            "channel": self.where_box.currentData(),
        }

    def _time(self, typed: str, original_text: str, original_us: int | None) -> int:
        if original_us is not None and typed.strip() == original_text:
            return original_us  # untouched: the exact stored microsecond is kept
        return ad.parse_local(typed, self.zone)

    def _accept(self) -> None:
        try:
            values = self.values()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        problem = ad.control_problem(values["label"], values["text"])
        if problem:
            self.error.setText(problem)
            return
        if values["end"] is not None and values["end"] <= values["time"]:
            self.error.setText("The end must be after the start.")
            return
        if self._check is not None:
            problem = self._check(values)
            if problem:
                self.error.setText(problem)
                return
        self.error.setText("")
        self.accept()


class SavePreview(QDialog):
    """What Save will write, per recording, before anything is written."""

    def __init__(
        self,
        plan_for: Callable[[dict], ad.SavePlan],
        label_of: Callable[[int], str],
        letter_of: Callable[[int], str],
        zone: tzinfo,
        parent=None,
        other_password_of: Callable[[int], str | None] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Save the new notes and events?")
        self._other_password_of = other_password_of or (lambda _i: None)
        self._plan_for = plan_for
        self._label_of = label_of
        self._letter_of = letter_of
        self.zone = zone
        self._choices: dict[tuple[int, ...], int] = {}
        self.plan = plan_for({})
        self.lines = QLabel("", self)
        self.lines.setWordWrap(True)
        self.lines.setTextFormat(Qt.TextFormat.PlainText)
        self.choice_box = QWidget(self)
        self.choice_layout = QVBoxLayout(self.choice_box)
        self.choice_layout.setContentsMargins(0, 0, 0, 0)
        self.choice_combos: dict[tuple[int, ...], QComboBox] = {}
        #: Recording index -> the field for the password the app does not hold.
        self.password_edits: dict[int, QLineEdit] = {}
        self.password_box = QWidget(self)
        self.password_layout = QVBoxLayout(self.password_box)
        self.password_layout.setContentsMargins(0, 0, 0, 0)
        self.warning = QLabel(SAVE_WARNING, self)
        self.warning.setWordWrap(True)
        self.warning.setObjectName("Caption")
        self.copy_note = QLabel("", self)
        self.copy_note.setWordWrap(True)
        self.copy_note.setObjectName("Muted")
        self.problems = QLabel("", self)
        self.problems.setWordWrap(True)
        self.problems.setObjectName("Danger")
        self.problems.setTextFormat(Qt.TextFormat.PlainText)
        self.ok = QPushButton("Add them to the recordings", self)
        self.ok.setDefault(True)
        self.stop = QPushButton("Stop", self)
        box = QVBoxLayout(self)
        for w in (
            self.lines,
            self.choice_box,
            self.password_box,
            self.problems,
            self.warning,
            self.copy_note,
        ):
            box.addWidget(w)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.stop)
        row.addWidget(self.ok)
        box.addLayout(row)
        self.setMinimumWidth(self.fontMetrics().height() * 34)
        self.ok.clicked.connect(self.accept)
        self.stop.clicked.connect(self.reject)
        self._build_choices()
        self._build_passwords()
        self._render()

    def passwords(self) -> dict[int, str]:
        """The extra password typed for each recording that needs one."""
        return {i: e.text() for i, e in self.password_edits.items() if e.text()}

    def _build_passwords(self) -> None:
        involved = set(self.plan.by_recording)
        for _item, group in self.plan.ambiguous:
            involved.update(group)
        asked = {i: self._other_password_of(i) for i in sorted(involved)}
        asked = {i: what for i, what in asked.items() if what}
        if not asked:
            self.password_box.setVisible(False)
            return
        intro = QLabel(
            "To add notes to a password-protected recording, both its passwords are needed. "
            "The app holds one of them; enter the other.",
            self.password_box,
        )
        intro.setWordWrap(True)
        self.password_layout.addWidget(intro)
        for i, what in asked.items():
            label = QLabel(f"{self._label_of(i)}: the {what}", self.password_box)
            edit = QLineEdit(self.password_box)
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.textChanged.connect(lambda _t: self._render())
            self.password_layout.addWidget(label)
            self.password_layout.addWidget(edit)
            self.password_edits[i] = edit

    def _passwords_ready(self) -> bool:
        return all(
            self.password_edits[i].text()
            for i in self.plan.by_recording
            if i in self.password_edits
        )

    def choices(self) -> dict[tuple[int, ...], int]:
        return dict(self._choices)

    def _build_choices(self) -> None:
        for group in self.plan.choice_groups:
            members = [it for it, c in self.plan.ambiguous if c == group]
            n = len(members)
            who = ad_join([self._letter_of(i) for i in group])
            names = sorted({m.channel for m in members if m.channel})
            if names and all(m.channel for m in members):
                kind = f"for channel {names[0]}" if len(names) == 1 else "for channels"
            else:
                kind = "for the whole recording"
            text = (
                f"{n} {'note' if n == 1 else 'notes'} {kind} fall where "
                f"recordings {who} both hold data. Choose where they go:"
            )
            label = QLabel(text, self.choice_box)
            label.setWordWrap(True)
            combo = QComboBox(self.choice_box)
            combo.addItem("Choose a recording", None)
            for i in group:
                combo.addItem(self._label_of(i), i)
            combo.currentIndexChanged.connect(lambda _x, g=group, c=combo: self._chose(g, c))
            self.choice_layout.addWidget(label)
            self.choice_layout.addWidget(combo)
            self.choice_combos[group] = combo

    def _chose(self, group: tuple[int, ...], combo: QComboBox) -> None:
        index = combo.currentData()
        if index is None:
            self._choices.pop(group, None)
        else:
            self._choices[group] = int(index)
        self.plan = self._plan_for(self._choices)
        self._render()

    def _render(self) -> None:
        plan = self.plan
        lines = []
        for index in sorted(plan.by_recording):
            share = plan.by_recording[index]
            a, b = share.span
            first, last = (timefmt.format_time(x, self.zone, 3) for x in (a, b))
            when = first if a == b else f"{first} to {last}"
            lines.append(
                f"{self._label_of(index)}: "
                f"{timefmt.format_count(share.count, 'note or event', 'notes and events')}, {when}"
            )
        if not lines:
            lines.append("There is nothing to add to a recording yet.")
        self.lines.setText("\n".join(lines))
        problems = []
        if plan.refused:
            n = len(plan.refused)
            problems.append(
                f"{n} {'note falls' if n == 1 else 'notes fall'} outside every recording "
                "and cannot be added. Take them out of the draft or move them first."
            )
        if plan.ambiguous:
            problems.append("Choose where the notes above go before saving.")
        self.problems.setText("\n".join(problems))
        self.problems.setVisible(bool(problems))
        n = plan.needs_copy
        self.copy_note.setText(
            f"{n} {'change to a stored note needs' if n == 1 else 'changes to stored notes need'} "
            "a new copy. That is available in a later version, so "
            f"{'it is' if n == 1 else 'they are'} not written now."
            if n
            else ""
        )
        self.copy_note.setVisible(bool(n))
        self.ok.setEnabled(plan.can_save and self._passwords_ready())


def ad_join(letters: Sequence[str]) -> str:
    if len(letters) <= 1:
        return "".join(letters)
    return ", ".join(letters[:-1]) + " and " + letters[-1]


class ImportPreview(QDialog):
    """What an import file holds, before anything enters the draft."""

    MAX_LISTED = 12

    def __init__(self, preview: ad.ImportPreview, zone: tzinfo, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add notes and events from a file?")
        self.preview = preview
        self.text = QLabel(self.summary(preview, zone), self)
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.PlainText)
        self.ok = QPushButton("Add them to the draft", self)
        self.ok.setDefault(True)
        self.ok.setEnabled(preview.can_add)
        self.stop = QPushButton("Stop", self)
        box = QVBoxLayout(self)
        box.addWidget(self.text)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.stop)
        row.addWidget(self.ok)
        box.addLayout(row)
        self.setMinimumWidth(self.fontMetrics().height() * 32)
        self.ok.clicked.connect(self.accept)
        self.stop.clicked.connect(self.reject)

    @staticmethod
    def summary(p: ad.ImportPreview, zone: tzinfo) -> str:
        if p.fatal:
            return f"{p.fatal} Nothing was added."
        lines = [f"The file has {timefmt.format_count(p.rows, 'row')}."]
        lines.append(
            f"{timefmt.format_count(len(p.usable), 'note or event', 'notes and events')} can be "
            "added to the draft."
        )
        if p.first is not None:
            lines.append(
                f"They run from {timefmt.format_time(p.first, zone, 3)} "
                f"to {timefmt.format_time(p.last, zone, 3)}."
            )
        if p.ambiguous:
            lines.append(
                f"{p.ambiguous} of them fall where several recordings hold data. You choose "
                "where they go when you save."
            )
        if p.outside:
            lines.append(
                f"{timefmt.format_count(len(p.outside), 'row')} fall outside every recording "
                "and are left out:"
            )
            lines += [f"  Row {n}: {why}" for n, why in p.outside[: ImportPreview.MAX_LISTED]]
            if len(p.outside) > ImportPreview.MAX_LISTED:
                lines.append(f"  and {len(p.outside) - ImportPreview.MAX_LISTED} more")
        if p.invalid:
            lines.append(
                f"{timefmt.format_count(len(p.invalid), 'row')} cannot be used and are left out:"
            )
            lines += [f"  Row {n}: {why}" for n, why in p.invalid[: ImportPreview.MAX_LISTED]]
            if len(p.invalid) > ImportPreview.MAX_LISTED:
                lines.append(f"  and {len(p.invalid) - ImportPreview.MAX_LISTED} more")
        lines.append("Nothing is written to a recording until you save.")
        return "\n".join(lines)


def ask_unsaved(parent, what: str = "leave") -> str:
    """Save / Discard / Cancel with unsaved changes. Returns "save",
    "discard" or "stay". (Qt's own standard buttons.)"""
    box = QMessageBox(
        QMessageBox.Icon.Question,
        "Save the changes?",
        "You have changes that are not saved yet. If you "
        + ("leave edit mode" if what == "leave" else "close the window")
        + " without saving, they are lost.",
        QMessageBox.StandardButton.Save
        | QMessageBox.StandardButton.Discard
        | QMessageBox.StandardButton.Cancel,
        parent,
    )
    box.setDefaultButton(QMessageBox.StandardButton.Save)
    box.exec()
    clicked = box.standardButton(box.clickedButton())
    if clicked == QMessageBox.StandardButton.Save:
        return "save"
    if clicked == QMessageBox.StandardButton.Discard:
        return "discard"
    return "stay"
