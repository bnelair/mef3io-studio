"""Plain-language dialogs with the technical wording behind a toggle."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..model.errors import UserMessage
from .pages import scaled_font


class TechnicalDetails(QWidget):
    """A "Technical details" toggle with a read-only text box under it.
    Technical terms (file names, error types) belong only in here."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.toggle = QToolButton(self)
        self.toggle.setObjectName("DetailsToggle")
        self.toggle.setText("Technical details")
        self.toggle.setCheckable(True)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.box = QPlainTextEdit(self)
        self.box.setReadOnly(True)
        self.box.setPlainText(text)
        self.box.setVisible(False)
        self.box.setMinimumHeight(self.fontMetrics().height() * 5)
        self.toggle.toggled.connect(self._on_toggled)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.box)

    def _on_toggled(self, shown: bool) -> None:
        self.box.setVisible(shown)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)


class MessageDialog(QDialog):
    """Title, a plain-language explanation, optional technical details."""

    def __init__(self, message: UserMessage, parent=None):
        super().__init__(parent)
        self.setWindowTitle(message.title)
        em = self.fontMetrics().height()
        self.setMinimumWidth(em * 26)
        heading = QLabel(message.title, self)
        heading.setFont(scaled_font(self, 1.25, bold=True))
        heading.setWordWrap(True)
        body = QLabel(message.text, self)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, self)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.setSpacing(int(em * 0.6))
        layout.addWidget(heading)
        layout.addWidget(body)
        self.details = None
        if message.detail:
            self.details = TechnicalDetails(message.detail, self)
            layout.addWidget(self.details)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(buttons)
        layout.addLayout(row)
