"""The explorer's small dialogs: a recording's password, and the site time
zone."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from ..common.pages import scaled_font
from ..model.timefmt import UTC_NAME, zone_names

PASSWORD_EXPLANATION = (
    "MEF passwords come in two kinds. The full-access password opens everything, "
    "including the patient details. The basic password opens the rest; the patient "
    "details then stay locked. Passwords don't hide the signal, the recording's name "
    "or its timing. The password is kept only while the app is open, and never saved."
)


class PasswordDialog(QDialog):
    """Asks for one recording's password. The text is masked as it is typed."""

    def __init__(self, recording_name: str, wrong: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Password needed")
        em = self.fontMetrics().height()
        self.setMinimumWidth(em * 26)
        heading = QLabel(f"“{recording_name}” is password-protected", self)
        heading.setFont(scaled_font(self, 1.15, bold=True))
        heading.setWordWrap(True)
        self.problem = QLabel(
            "That password did not open it. Check it (it is case-sensitive) and try again.",
            self,
        )
        self.problem.setObjectName("Danger")
        self.problem.setWordWrap(True)
        self.problem.setVisible(wrong)
        body = QLabel(PASSWORD_EXPLANATION, self)
        body.setObjectName("Muted")
        body.setWordWrap(True)
        self.field = QLineEdit(self)
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.field.setPlaceholderText("Password")
        self.field.setAccessibleName("Password")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.open_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.open_button.setText("Open")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Not now")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.field.textChanged.connect(lambda t: self.open_button.setEnabled(bool(t)))
        self.open_button.setEnabled(False)
        layout = QVBoxLayout(self)
        layout.setSpacing(int(em * 0.6))
        layout.addWidget(heading)
        layout.addWidget(self.problem)
        layout.addWidget(body)
        layout.addWidget(self.field)
        layout.addWidget(buttons)
        self.field.setFocus()

    def password(self) -> str:
        return self.field.text()


class TimeZoneDialog(QDialog):
    """Choose the recording site's time zone (one per app profile)."""

    def __init__(self, current: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Time zone")
        em = self.fontMetrics().height()
        self.setMinimumWidth(em * 28)
        heading = QLabel("Show times in the recording site's time zone", self)
        heading.setFont(scaled_font(self, 1.15, bold=True))
        heading.setWordWrap(True)
        body = QLabel(
            "Recordings store every time in UTC. Choose where the recordings were made "
            "(for example Europe/Prague or America/Chicago), and times are shown in that "
            "place's local time, correct on both sides of a daylight-saving change. Choose "
            "UTC to show the stored times as they are. The time offset some recordings "
            "carry is not used: it is one fixed number and ignores daylight saving.",
            self,
        )
        body.setObjectName("Muted")
        body.setWordWrap(True)
        self.combo = QComboBox(self)
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.names = zone_names()
        self.combo.addItems(self.names)
        completer = QCompleter(self.names, self.combo)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.combo.setCompleter(completer)
        self.combo.setCurrentText(current or UTC_NAME)
        self.combo.setAccessibleName("Time zone")
        self.problem = QLabel("Choose a time zone from the list.", self)
        self.problem.setObjectName("Danger")
        self.problem.setVisible(False)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use this time zone")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Keep the current one")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setSpacing(int(em * 0.6))
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addWidget(self.combo)
        layout.addWidget(self.problem)
        layout.addWidget(buttons)

    def zone_name(self) -> str:
        return self.combo.currentText().strip()

    def _accept_if_valid(self) -> None:
        if self.zone_name() in self.names:
            self.accept()
        else:
            self.problem.setVisible(True)
