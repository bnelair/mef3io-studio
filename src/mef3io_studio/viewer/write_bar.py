"""The red bar at the very top of the viewer: the changes to the notes are NOT in the recording.

Every change to the notes (added, changed, removed) stays in memory, in this window, until
"Write to file" is pressed. While there is at least one such change the bar is shown, strong
red with white text (readable in both themes; not colour alone: a warning sign and bold
words), says how many there are, and offers "Write to file...", "Show changes" and
"Discard changes...". With none it is hidden. It takes a row of its own above the controls.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy

from ..model.write_changes import counts_text

#: Backgrounds with white text: 6.5:1 (light) and 5.6:1 (dark), both above 4.5:1.
RED_LIGHT = "#b3261e"
RED_DARK = "#c62828"
WARNING_SIGN = "⚠"


def bar_text(added: int, changed: int, removed: int) -> str:
    """What the bar says (plain words), with the counts."""
    counts = counts_text(added, changed, removed)
    return (
        "NOT SAVED: your changes to the notes are not in the recording yet "
        f"({counts}). They exist only in this window. "
        "“Write to file” will update the recording."
    )


class WriteBar(QFrame):
    write_requested = Signal()
    show_requested = Signal()
    discard_requested = Signal()

    def __init__(self, parent=None, dark: bool = False):
        super().__init__(parent)
        self.setObjectName("WriteBar")
        self.setVisible(False)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 10, 6)
        row.setSpacing(8)
        self.sign = QLabel(WARNING_SIGN, self)
        self.sign.setObjectName("WriteBarSign")
        self.text = QLabel("", self)
        self.text.setObjectName("WriteBarText")
        self.text.setWordWrap(True)
        self.text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.write_button = QPushButton("Write to file…", self)
        self.write_button.setObjectName("WriteBarPrimary")
        self.write_button.setToolTip("See every change, then update the recording (Ctrl+S)")
        self.show_button = QPushButton("Show changes", self)
        self.show_button.setToolTip("The list of every change that is not written yet")
        self.discard_button = QPushButton("Discard changes…", self)
        self.discard_button.setToolTip("Throw all these changes away; the recording stays as it is")
        for b in (self.write_button, self.show_button, self.discard_button):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.sign)
        row.addWidget(self.text, 1)
        for b in (self.write_button, self.show_button, self.discard_button):
            row.addWidget(b)
        self.write_button.clicked.connect(self.write_requested)
        self.show_button.clicked.connect(self.show_requested)
        self.discard_button.clicked.connect(self.discard_requested)
        self.counts = (0, 0, 0)
        self.set_dark(dark)

    def set_dark(self, dark: bool) -> None:
        red = RED_DARK if dark else RED_LIGHT
        self.setStyleSheet(
            f"#WriteBar {{ background: {red}; border-bottom: 2px solid #ffffff; }}"
            "#WriteBar QLabel { color: #ffffff; }"
            "#WriteBarSign { font-size: 20px; font-weight: bold; }"
            "#WriteBarText { font-weight: bold; }"
            "#WriteBar QPushButton { color: #ffffff; background: transparent; "
            "border: 2px solid #ffffff; border-radius: 4px; padding: 4px 10px; }"
            "#WriteBar QPushButton:hover, #WriteBar QPushButton:focus "
            "{ background: rgba(255,255,255,60); }"
            f"#WriteBar QPushButton#WriteBarPrimary {{ background: #ffffff; color: {red}; "
            "font-weight: bold; }"
            "#WriteBar QPushButton#WriteBarPrimary:hover, "
            "#WriteBar QPushButton#WriteBarPrimary:focus { background: #ffe9e7; }"
        )

    def show_counts(self, added: int, changed: int, removed: int) -> None:
        """Show the bar with these counts, or hide it when there is nothing to write."""
        self.counts = (added, changed, removed)
        total = added + changed + removed
        if total:
            self.text.setText(bar_text(added, changed, removed))
            self.text.setAccessibleName("Changes not written to the recording")
        self.setVisible(bool(total))
