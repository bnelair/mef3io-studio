"""The Privacy check card: does this recording still identify the patient?

One line at the top (green: nothing found; red: how many details were
found), a checklist with one row per kind of detail, whether the anonymizer
made the recording, and its start and end. Built from what the Explorer
already read (``model.privacycheck``); it reads no file. Values stay
"Hidden" until Show, shared with the Patient details tab.
"""

from __future__ import annotations

from datetime import tzinfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..common.pages import scaled_font
from ..model.privacycheck import (
    MOVED_DATES,
    NOT_MADE_BY_ANONYMIZER,
    SYMBOLS,
    Mark,
    PrivacyCheck,
    build_check,
    headline,
)
from ..model.timefmt import format_time
from .panels import HIDDEN, _clear, _notice, _scroll
from .state import RecordingEntry


class PrivacyPanel(QWidget):
    reveal_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        inner = QWidget()
        self.headline = QLabel("", inner)
        self.headline.setObjectName("PrivacyHeadline")
        self.headline.setFont(scaled_font(self, 1.2, bold=True))
        self.headline.setWordWrap(True)
        self.found_list = QLabel("", inner)
        self.found_list.setWordWrap(True)
        self.found_list.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.button = QPushButton("Show", inner)
        self.button.setCheckable(True)
        self.button.setToolTip("Show the values found (this recording only)")
        self.button.toggled.connect(self._toggled)
        self.origin = QVBoxLayout()
        self.dates = QLabel("", inner)
        self.dates.setWordWrap(True)
        self.dates.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.dates_note = QLabel("", inner)
        self.dates_note.setObjectName("Muted")
        self.dates_note.setWordWrap(True)
        self.grid = QGridLayout()
        self.grid.setColumnStretch(2, 1)
        intro = QLabel(
            "Looks for everything in this recording that could identify the patient, "
            "using only its details (no signal is read). Values stay hidden until you "
            "choose Show.",
            inner,
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        top = QHBoxLayout()
        top.addWidget(self.headline, 1)
        top.addWidget(self.button, 0, Qt.AlignmentFlag.AlignTop)
        box = QVBoxLayout(inner)
        box.addLayout(top)
        box.addWidget(self.found_list)
        box.addLayout(self.origin)
        box.addWidget(self.dates)
        box.addWidget(self.dates_note)
        box.addLayout(self.grid)
        box.addWidget(intro)
        box.addStretch(1)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(_scroll(inner))
        self._updating = False
        self.check: PrivacyCheck | None = None
        #: What each row says, by label (for tests): (symbol, text).
        self.rows: dict[str, tuple[str, str]] = {}
        self.origin_text = ""

    def _toggled(self, shown: bool) -> None:
        self.button.setText("Hide" if shown else "Show")
        if not self._updating:
            self.reveal_changed.emit(shown)

    def _color(self, mark: Mark) -> str:
        return {Mark.FOUND: "Bad", Mark.KEPT: "Caution", Mark.UNKNOWN: "Muted"}.get(mark, "Good")

    def show_entry(self, entry: RecordingEntry, zone: tzinfo) -> None:
        self._updating = True
        self.button.setChecked(entry.revealed)
        self.button.setText("Hide" if entry.revealed else "Show")
        self._updating = False
        _clear(self.origin)
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.rows = {}
        details = entry.details
        if details is None:
            self.check = None
            self.headline.setText("")
            self.found_list.setText("")
            self.dates.setText("")
            self.dates_note.setText("")
            self.button.setEnabled(False)
            return
        self.button.setEnabled(True)
        check = self.check = build_check(details)
        self.headline.setText(headline(check))
        self.headline.setProperty("state", "bad" if check.found else "good")
        self.headline.setObjectName("PrivacyBad" if check.found else "PrivacyGood")
        self.headline.style().unpolish(self.headline)
        self.headline.style().polish(self.headline)
        lines = []
        for item in check.found:
            if entry.revealed:
                shown = "; ".join(item.values) or "(set)"
            else:
                shown = HIDDEN
            lines.append(f"• {item.label}: {shown}")
        if check.kept_text and not check.found:
            lines.append("• Note text was kept, as chosen: read it before sharing.")
        self.found_list.setText("\n".join(lines))
        self.found_list.setVisible(bool(lines))

        if check.made_by_anonymizer:
            self.origin_text = "Made by the anonymizer."
            self.origin.addWidget(_notice(self.origin_text))
        else:
            self.origin_text = NOT_MADE_BY_ANONYMIZER
            detail = (
                "Some of its parts were made by the anonymizer and some were not, or some "
                "could not be read."
                if check.partly_anonymizer
                else ""
            )
            self.origin.addWidget(_notice(self.origin_text, warning=True, detail=detail))

        start, end = details.start, details.end
        label = "Moved dates" if check.made_by_anonymizer else "Dates as stored"
        self.dates.setText(f"{label}: {format_time(start, zone)} to {format_time(end, zone)}")
        self.dates.setFont(scaled_font(self, 1.05, bold=True))
        self.dates_note.setText(MOVED_DATES if check.made_by_anonymizer else "")
        self.dates_note.setVisible(check.made_by_anonymizer)

        em = self.fontMetrics().height()
        for row, item in enumerate(check.items):
            symbol = QLabel(SYMBOLS[item.mark])
            symbol.setObjectName(self._color(item.mark))
            symbol.setFont(scaled_font(self, 1.1, bold=True))
            symbol.setMinimumWidth(int(em * 1.2))
            name = QLabel(item.label)
            name.setFont(scaled_font(self, 1.0, bold=True))
            text = item.summary
            if item.visible_value:
                text = f"{text} {item.visible_value}"
            elif item.values and item.mark in (Mark.FOUND, Mark.KEPT, Mark.EMPTY):
                if entry.revealed:
                    text = f"{text} {'; '.join(v for v in item.values if v)}".strip()
                elif item.mark is not Mark.EMPTY:
                    text = f"{text} ({HIDDEN})"
            value = QLabel(text)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.grid.addWidget(symbol, row, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(name, row, 1, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(value, row, 2)
            self.rows[item.label] = (SYMBOLS[item.mark], text)
        # Keep the grid compact: no frame lines between rows.
        for widget in (self.found_list,):
            widget.setFrameShape(QFrame.Shape.NoFrame)


__all__ = ["PrivacyPanel"]
