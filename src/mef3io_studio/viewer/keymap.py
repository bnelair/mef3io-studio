"""ONE table of keys, driving both the key handling and the help overlay
(docs/design/viewer.md, "Navigation"), so the two cannot drift.

Keys are written in Qt's portable text (``Shift+Right``). On macOS the
spec's "Ctrl+Up / Ctrl+Down" for row spacing is "Alt+Up / Alt+Down"
(Ctrl+Arrow is taken by the system there).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

MAC = sys.platform == "darwin"


@dataclass(frozen=True)
class KeyEntry:
    action: str
    keys: tuple[str, ...]
    text: str
    #: A held key repeats navigation only; writing never repeats.
    repeat: bool = False
    #: A feature the window must have for the key to work ("" = always).
    feature: str = ""


_SPACING = ("Alt+Up", "Alt+Down") if MAC else ("Ctrl+Up", "Ctrl+Down")

KEYMAP: tuple[KeyEntry, ...] = (
    KeyEntry("page_forward", ("Right",), "Page forward", True),
    KeyEntry("page_back", ("Left",), "Page back", True),
    KeyEntry("half_forward", ("Shift+Right",), "Half a page forward", True),
    KeyEntry("half_back", ("Shift+Left",), "Half a page back", True),
    KeyEntry("second_forward", ("Alt+Right",), "One second forward", True),
    KeyEntry("second_back", ("Alt+Left",), "One second back", True),
    KeyEntry("start", ("Home",), "Go to the start"),
    KeyEntry("end", ("End",), "Go to the end"),
    KeyEntry("sens_up", ("Up", "+", "="), "Larger signals (more sensitive)", True),
    KeyEntry("sens_down", ("Down", "-"), "Smaller signals (less sensitive)", True),
    KeyEntry("spacing_up", (_SPACING[0],), "More space between rows", True),
    KeyEntry("spacing_down", (_SPACING[1],), "Less space between rows", True),
    KeyEntry("rows_up", ("Shift+Up", "PgUp"), "Channels up by a screenful", True),
    KeyEntry("rows_down", ("Shift+Down", "PgDown"), "Channels down by a screenful", True),
    KeyEntry("timebase_1", ("Alt+1",), "Timebase 10 mm/s"),
    KeyEntry("timebase_2", ("Alt+2",), "Timebase 15 mm/s"),
    KeyEntry("timebase_3", ("Alt+3",), "Timebase 30 mm/s"),
    KeyEntry("timebase_4", ("Alt+4",), "Timebase 60 mm/s"),
    KeyEntry("timebase_5", ("Alt+5",), "Timebase 120 mm/s"),
    KeyEntry("note_back", ("[",), "Previous note or event", False, "notes"),
    KeyEntry("note_forward", ("]",), "Next note or event", False, "notes"),
    KeyEntry("boundary_back", ("{",), "Previous recording boundary"),
    KeyEntry("boundary_forward", ("}",), "Next recording boundary"),
    KeyEntry("goto", ("G",), "Go to a time"),
    KeyEntry("jump", ("Ctrl+K",), "Jump (times, days, moves, recording letters)"),
    KeyEntry("channels", ("C",), "Choose channels"),
    KeyEntry("types", ("T",), "Types of notes and events", False, "notes"),
    KeyEntry("note_list", ("M",), "List of notes and events", False, "notes"),
    KeyEntry("montage", ("B",), "Next montage"),
    KeyEntry("filters", ("F",), "Filters"),
    KeyEntry("export_start", ("S",), "Set the export start at the pointer", False, "export"),
    KeyEntry("export_end", ("E",), "Set the export end at the pointer", False, "export"),
    KeyEntry(
        "add_note",
        ("A", "1", "2", "3", "4", "5", "6", "7", "8", "9"),
        "Add a note (edit mode only)",
        False,
        "edit",
    ),  # fmt: skip
    KeyEntry(
        "undo", ("Ctrl+Z",), "Undo the last change to the draft (edit mode only)", False, "edit"
    ),
    KeyEntry(
        "redo",
        ("Ctrl+Shift+Z", "Ctrl+Y"),
        "Redo (edit mode only)",
        False,
        "edit",
    ),
    KeyEntry("escape", ("Esc",), "Clear the selection and close panels"),
    KeyEntry("help", ("?", "Shift+/"), "Show this help"),
)

NAVIGATION_ACTIONS = frozenset(e.action for e in KEYMAP if e.repeat)

TIMEBASE_ACTIONS = {f"timebase_{i}": v for i, v in enumerate((10, 15, 30, 60, 120), 1)}


def _lookup() -> dict[str, KeyEntry]:
    table: dict[str, KeyEntry] = {}
    for entry in KEYMAP:
        for key in entry.keys:
            table[QKeySequence(key).toString(QKeySequence.SequenceFormat.PortableText)] = entry
    return table


_TABLE = _lookup()


def key_text(event: QKeyEvent) -> str:
    """The portable text of a key press (``Shift+Right``, ``?``)."""
    mods = event.modifiers()
    key = event.key()
    if key in (
        Qt.Key.Key_Shift,
        Qt.Key.Key_Control,
        Qt.Key.Key_Alt,
        Qt.Key.Key_Meta,
        Qt.Key.Key_unknown,
    ):
        return ""
    text = event.text()
    # Printable punctuation: the typed character decides ("+", "{", "?"), the
    # modifier Shift is already part of it.
    if text and text.isprintable() and not text.isalnum() and text != " ":
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        if ctrl == alt:  # none, or Ctrl+Alt = AltGr on Windows: the produced character decides
            return QKeySequence(text).toString(QKeySequence.SequenceFormat.PortableText)
    keypad = mods & ~Qt.KeyboardModifier.KeypadModifier
    return QKeySequence(keypad.value | key).toString(QKeySequence.SequenceFormat.PortableText)


def entry_for(event: QKeyEvent) -> KeyEntry | None:
    """The table entry a key press means, or None."""
    text = key_text(event)
    return _TABLE.get(text) if text else None


def keys_text(entry: KeyEntry) -> str:
    return "  /  ".join(
        QKeySequence(k).toString(QKeySequence.SequenceFormat.NativeText) for k in entry.keys[:3]
    ) + ("  ..." if len(entry.keys) > 3 else "")


class HelpOverlay(QWidget):
    """The key table as a panel over the traces, generated from KEYMAP."""

    def __init__(self, parent=None, features: frozenset[str] = frozenset()):
        super().__init__(parent)
        self.setObjectName("KeyHelp")
        self.setAutoFillBackground(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.features = features
        self.rows: list[tuple[str, str]] = []
        self._labels: list[tuple[KeyEntry, QLabel]] = []
        box = QVBoxLayout(self)
        title = QLabel("Keys and mouse", self)
        title.setObjectName("Caption")
        box.addWidget(title)
        for entry in KEYMAP:
            label = QLabel("", self)
            self._labels.append((entry, label))
            box.addWidget(label)
        mouse = QLabel(
            "<b>Mouse</b><br>Wheel: move in time. Shift+wheel: move through channels. "
            "Ctrl+wheel: zoom the timebase at the pointer. Drag: pan. Shift+drag: measure. "
            "Double-click: centre the page there. Right-click: more choices. "
            "Drag an export edge to move it; with it selected, Left and Right move it by "
            "one sample.",
            self,
        )
        mouse.setWordWrap(True)
        box.addWidget(mouse)
        self.set_features(features)
        self.hide()

    def set_features(self, features: frozenset[str]) -> None:
        """Show which keys work now (the edit keys only in edit mode)."""
        self.features = features
        self.rows = []
        for entry, label in self._labels:
            available = not entry.feature or entry.feature in features
            text = entry.text
            if not available:
                text = text if entry.feature == "edit" else f"{text} (not available yet)"
            keys = keys_text(entry)
            self.rows.append((keys, text))
            label.setText(f"<b>{keys}</b> &nbsp; {text}")
            label.setEnabled(available)
