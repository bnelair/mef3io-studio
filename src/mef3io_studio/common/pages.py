"""Placeholder pages for modules that arrive in later roadmap steps."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout, QWidget

from . import icons
from .theme import Theme


def scaled_font(widget: QWidget, factor: float, bold: bool = False):
    """A font relative to the user's system font, so text follows the OS
    font-size setting instead of a fixed pixel size."""
    font = widget.font()
    font.setPointSizeF(font.pointSizeF() * factor)
    font.setBold(bold)
    return font


class PlaceholderPage(QWidget):
    def __init__(self, icon_name: str, title: str, text: str, parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        card = QFrame(self)
        card.setObjectName("Card")
        em = self.fontMetrics().height()
        card.setMaximumWidth(em * 36)
        card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        self.icon = QLabel(card)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading = QLabel(title, card)
        heading.setFont(scaled_font(self, 1.6, bold=True))
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body = QLabel(text, card)
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setObjectName("Muted")
        body.setMinimumWidth(em * 22)
        body.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        pill = QLabel("Coming in a later update", card)
        pill.setObjectName("Pill")
        pill.setAlignment(Qt.AlignmentFlag.AlignCenter)

        inner = QVBoxLayout(card)
        inner.setContentsMargins(32, 32, 32, 32)
        inner.setSpacing(12)
        inner.addWidget(self.icon)
        inner.addWidget(heading)
        inner.addWidget(body)
        inner.addWidget(pill, 0, Qt.AlignmentFlag.AlignHCenter)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)
        outer.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(2)

    def set_theme(self, theme: Theme) -> None:
        side = self.fontMetrics().height() * 4
        self.icon.setPixmap(icons.icon(self._icon_name, theme.accent).pixmap(side, side))
