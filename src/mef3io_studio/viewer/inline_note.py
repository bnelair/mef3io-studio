"""The small text field that opens on the traces to write one note
(docs/design/viewer.md, "Adding notes quickly").

It sits where the note will be, takes the keyboard at once, and ends with
Enter (keep the note) or Esc (drop it). A short list of earlier texts and
types is offered for one key (Alt+1 to Alt+6, or a click). Widgets only: what
the note is, and where it goes, is decided by ``edit_mode``. What is typed
here can identify a patient: nothing is logged or stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QFrame, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

#: How many offered texts and types there can be (one key each).
MAX_HINTS = 6
#: A text longer than this is not kept as a note (the field stops there).
MAX_CHARS = 500


@dataclass(frozen=True)
class Hint:
    """One offered text or type. ``kind`` is "text" (replaces the field) or
    "type" (the label before a colon, kept in front of the rest)."""

    kind: str
    value: str


class _Field(QLineEdit):
    """A line that reports Enter, Esc, Ctrl+S, Alt+number and Up/Down."""

    keep = Signal(bool)  # True = and save now
    drop = Signal()
    hint_key = Signal(int)
    cycle = Signal(int)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key, mods = event.key(), event.modifiers()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.keep.emit(False)
        elif key == Qt.Key.Key_Escape:
            self.drop.emit()
        elif key == Qt.Key.Key_S and mods & Qt.KeyboardModifier.ControlModifier:
            self.keep.emit(True)
        elif Qt.Key.Key_1 <= key <= Qt.Key.Key_9 and mods & (
            Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ControlModifier
        ):
            self.hint_key.emit(int(key) - int(Qt.Key.Key_1))
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.cycle.emit(-1 if key == Qt.Key.Key_Up else 1)
        else:
            super().keyPressEvent(event)
            return
        event.accept()


class InlineNote(QFrame):
    """The field with a line saying what it adds, and the offered texts."""

    #: ``(text, save now)``: the note was kept.
    kept = Signal(str, bool)
    #: The note was dropped (Esc, or left empty by a click elsewhere).
    dropped = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("InlineNote")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.head = QLabel("", self)
        self.head.setObjectName("Caption")
        self.field = _Field(self)
        self.field.setMaxLength(MAX_CHARS)
        self.field.setPlaceholderText("What happened (optional)")
        self.help = QLabel("Enter keeps it. Esc drops it. Ctrl+S keeps it and saves.", self)
        self.help.setObjectName("Muted")
        self.hint_box = QWidget(self)
        self.hint_layout = QVBoxLayout(self.hint_box)
        self.hint_layout.setContentsMargins(0, 0, 0, 0)
        self.hint_layout.setSpacing(0)
        box = QVBoxLayout(self)
        box.setContentsMargins(8, 6, 8, 6)
        box.setSpacing(3)
        for w in (self.head, self.field, self.help, self.hint_box):
            box.addWidget(w)
        self.hints: list[Hint] = []
        self.hint_buttons: list[QPushButton] = []
        self._at = -1  # the offered text the arrows are on
        self._done = True
        self.field.keep.connect(self._keep)
        self.field.drop.connect(self._drop)
        self.field.hint_key.connect(self.use_hint)
        self.field.cycle.connect(self._cycle)
        self.field.installEventFilter(self)
        self.hide()

    # -- opening and closing ----------

    @property
    def is_open(self) -> bool:
        return self.isVisible() and not self._done

    def open_at(
        self,
        x: float,
        y: float,
        head: str,
        text: str = "",
        hints: Sequence[Hint] = (),
    ) -> None:
        """Show the field with its caret, at ``(x, y)`` of the parent's own
        coordinates (kept inside the parent)."""
        self._done = False
        self.head.setText(head)
        self.field.setText(text)
        self._set_hints(list(hints)[:MAX_HINTS])
        self._at = -1
        width = max(self.fontMetrics().height() * 26, 260)
        self.setFixedWidth(width)
        self.adjustSize()
        parent = self.parentWidget()
        px = parent.width() if parent is not None else width
        py = parent.height() if parent is not None else 0
        self.move(int(max(0, min(x, px - self.width()))), int(max(0, min(y, py - self.height()))))
        self.show()
        self.raise_()
        self.field.setFocus(Qt.FocusReason.OtherFocusReason)
        self.field.selectAll()

    def _set_hints(self, hints: list[Hint]) -> None:
        for b in self.hint_buttons:
            b.deleteLater()
        self.hint_buttons = []
        self.hints = hints
        for n, hint in enumerate(hints):
            shown = hint.value if hint.kind == "text" else f"{hint.value}:"
            label = f"{n + 1}  {shown}"
            button = QPushButton(label, self.hint_box)
            button.setFlat(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setStyleSheet("text-align: left; padding: 1px 2px;")
            button.setToolTip(f"Alt+{n + 1}")
            fm = self.fontMetrics()
            button.setText(fm.elidedText(label, Qt.TextElideMode.ElideRight, 240))
            button.clicked.connect(lambda _c=False, i=n: self.use_hint(i))
            self.hint_layout.addWidget(button)
            self.hint_buttons.append(button)
        self.hint_box.setVisible(bool(hints))

    def keep_now(self, save: bool = False) -> None:
        """Keep what is typed as if Enter was pressed (Ctrl+S: and save)."""
        self._keep(save)

    def close_field(self) -> None:
        self._done = True
        self.hide()

    def _keep(self, save: bool) -> None:
        if self._done:
            return
        text = self.field.text()
        self._done = True
        self.hide()
        self.kept.emit(text, save)

    def _drop(self) -> None:
        if self._done:
            return
        self._done = True
        self.hide()
        self.dropped.emit()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Leaving the field by a click elsewhere keeps what was typed (never
        loses it) and drops an empty field."""
        if obj is self.field and event.type() == QEvent.Type.FocusOut and not self._done:
            reason = event.reason()
            if reason in (Qt.FocusReason.PopupFocusReason, Qt.FocusReason.ActiveWindowFocusReason):
                return False  # a menu or another window: the field stays
            if self.field.text().strip():
                self._keep(False)
            else:
                self._drop()
        return super().eventFilter(obj, event)

    # -- the offered texts ----------

    def use_hint(self, index: int) -> None:
        """Put offered text ``index`` (0-based) into the field."""
        if not 0 <= index < len(self.hints):
            return
        hint = self.hints[index]
        if hint.kind == "text":
            self.field.setText(hint.value)
        else:  # a type: its label in front, the rest of the text kept
            rest = self.field.text()
            head, sep, tail = rest.partition(":")
            body = tail.strip() if sep and head.strip() and len(head) <= 40 else rest.strip()
            self.field.setText(f"{hint.value}: {body}" if body else f"{hint.value}: ")
        self.field.setFocus()
        self.field.setCursorPosition(len(self.field.text()))

    def _cycle(self, step: int) -> None:
        """Up and Down walk through the earlier texts."""
        texts = [i for i, h in enumerate(self.hints) if h.kind == "text"]
        if not texts:
            return
        if self._at not in texts:
            self._at = texts[0] if step > 0 else texts[-1]
        else:
            at = texts.index(self._at) + step
            self._at = texts[at % len(texts)]
        self.use_hint(self._at)
