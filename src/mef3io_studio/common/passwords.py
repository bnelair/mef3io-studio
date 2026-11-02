"""The passwords choice for a NEW copy: none, or a new pair (basic +
full-access). Shared by the anonymizer, the joiner and "Edit details".

The originals' passwords are never carried over, and the app never stores
passwords. Passwords here protect the details, settings and notes of the new
recording; the signal itself is never password-protected."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFormLayout,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..model.anonymize import password_may_not_work_elsewhere
from .pages import label as _label

PASSWORD_EXPLANATION = (
    "A password-protected copy needs two passwords. The basic password opens the "
    "recording settings and descriptions; the full-access password also opens the "
    "patient details and the text of notes and events. Passwords do not hide the "
    "signal, the recording's name or its timing: anyone with the files can still read "
    "those. There is no way to get a forgotten password back, and the app never "
    "stores passwords. The original's passwords are never carried over."
)


class PasswordsStep(QWidget):
    """No password, or two new ones. Also the joiner's (``joiner/page.py``),
    which names what is protected (``what``) and explains it its own way."""

    changed = Signal()

    def __init__(
        self,
        page,
        what: str = "the copies",
        explanation: str = PASSWORD_EXPLANATION,
        none_label: str = "No password (the usual choice for sharing)",
    ):
        super().__init__(page)
        self.what = what
        self.none = QRadioButton(none_label)
        self.both = QRadioButton(f"Protect {what} with two new passwords")
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
        layout.addWidget(_label(explanation, muted=True))
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
                f"Other programs may not open {self.what} with these passwords: keep each "
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
