"""Asking for passwords again when an interrupted batch continues.

Passwords are never written into the batch journal, so continuing a batch
asks for them again: the full-access password of the originals (when they
need one) and the two passwords of the copies (when they were protected).
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout


class ResumePasswordsDialog(QDialog):
    def __init__(self, needs_source: bool, protected: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Passwords for the batch")
        intro = QLabel(
            "Passwords are never stored, so type them again to continue the batch.", self
        )
        intro.setWordWrap(True)
        self.source = QLineEdit(self)
        self.basic = QLineEdit(self)
        self.full = QLineEdit(self)
        form = QFormLayout()
        if needs_source:
            form.addRow("Full-access password of the originals:", self.source)
        if protected:
            form.addRow("Basic password of the copies:", self.basic)
            form.addRow("Full-access password of the copies:", self.full)
        for field in (self.source, self.basic, self.full):
            field.setEchoMode(QLineEdit.EchoMode.Password)
        self.needs_source, self.protected = needs_source, protected
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Not now")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, tuple[str, str] | None]:
        output = (self.basic.text(), self.full.text()) if self.protected else None
        return self.source.text(), output
