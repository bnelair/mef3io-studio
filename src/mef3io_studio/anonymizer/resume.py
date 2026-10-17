"""Asking for passwords again when an interrupted batch continues.

Passwords are never written into the batch journal, so continuing a batch
asks for them again: the full-access password of EACH original that needs
one (originals may have different passwords), and the two passwords of the
copies, each typed twice. Every one is then tried before the batch goes on
(the originals are opened with theirs, a finished copy with the new one).
Recordings are named by position ("Recording 3 of 7"), as everywhere in
the resume flow.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)


class ResumePasswordsDialog(QDialog):
    def __init__(self, sources: list[int], total: int, protected: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Passwords for the batch")
        intro = QLabel(
            "Passwords are never stored, so type them again to continue the batch. Each one "
            "is tried before anything is made.",
            self,
        )
        intro.setWordWrap(True)
        self.sources: dict[int, QLineEdit] = {}
        form = QFormLayout()
        for n in sources:
            field = QLineEdit(self)
            self.sources[n] = field
            form.addRow(f"Full-access password of recording {n + 1} of {total}:", field)
        self.protected = protected
        self.basic = QLineEdit(self)
        self.basic_again = QLineEdit(self)
        self.full = QLineEdit(self)
        self.full_again = QLineEdit(self)
        if protected:
            form.addRow("Basic password of the copies:", self.basic)
            form.addRow("Basic password of the copies, again:", self.basic_again)
            form.addRow("Full-access password of the copies:", self.full)
            form.addRow("Full-access password of the copies, again:", self.full_again)
        for field in (
            *self.sources.values(),
            self.basic,
            self.basic_again,
            self.full,
            self.full_again,
        ):
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.textChanged.connect(self._update)
        self.problem = QLabel("", self)
        self.problem.setObjectName("Bad")
        self.problem.setWordWrap(True)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Not now")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)
        self._update()

    def problem_text(self) -> str:
        if any(not f.text() for f in self.sources.values()):
            return "Type the full-access password of every original listed."
        if self.protected:
            if not self.basic.text() or not self.full.text():
                return "Type both passwords of the copies."
            if self.basic.text() != self.basic_again.text():
                return "The two basic passwords are not the same."
            if self.full.text() != self.full_again.text():
                return "The two full-access passwords are not the same."
        return ""

    def _update(self, *_args) -> None:
        text = self.problem_text()
        self.problem.setText(text)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not text)

    def values(self) -> tuple[dict[int, str], tuple[str, str] | None]:
        output = (self.basic.text(), self.full.text()) if self.protected else None
        return {n: f.text() for n, f in self.sources.items()}, output
