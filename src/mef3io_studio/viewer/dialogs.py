"""Small dialogs of the viewer: the jump box."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from .jump import HELP_TEXT


class JumpDialog(QDialog):
    """Type a time, a move or a recording letter. ``jump_requested(text,
    previous)`` is emitted; the window answers with ``show_error`` on a
    problem or closes the dialog."""

    jump_requested = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Jump")
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("14:30   day 2 08:00   +10m   B")
        self.hint = QLabel(HELP_TEXT, self)
        self.hint.setObjectName("Muted")
        self.hint.setWordWrap(True)
        self.error = QLabel("", self)
        self.error.setObjectName("Danger")
        self.error.setWordWrap(True)
        self.go = QPushButton("Go", self)
        self.go.setDefault(True)
        self.previous = QPushButton("Go to the previous one", self)
        self.previous.setToolTip("For a time of day: the last time it happened before this page")
        box = QVBoxLayout(self)
        box.addWidget(self.edit)
        box.addWidget(self.hint)
        box.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.previous)
        row.addWidget(self.go)
        box.addLayout(row)
        self.setMinimumWidth(self.fontMetrics().height() * 28)
        self.go.clicked.connect(lambda: self.jump_requested.emit(self.edit.text(), False))
        self.previous.clicked.connect(lambda: self.jump_requested.emit(self.edit.text(), True))
        self.edit.returnPressed.connect(
            lambda: self.jump_requested.emit(
                self.edit.text(), bool(Qt.KeyboardModifier.ShiftModifier & self._mods())
            )
        )

    @staticmethod
    def _mods():
        from PySide6.QtGui import QGuiApplication

        return QGuiApplication.keyboardModifiers()

    def show_error(self, text: str) -> None:
        self.error.setText(text)
