"""Edit mode of the signal viewer: the switch, the banner, the draft, adding,
changing and taking out notes and events, importing, exporting and saving
(docs/design/viewer.md, "Annotations", "Edit mode").

New notes are written by appending to a recording's notes. A stored note is taken
out (Delete on it, one line asks first) or changed (saved from the draft) with mef3io's
``remove_annotations``: a change is "remove the old one and add the new one" in one library
call, and what was removed cannot be brought back from the app.

The window's GUI thread never calls the library: reading a file for import
and saving run as tasks (``JobManager``), and the viewer's data is reopened
by the save task after the write.
"""

from __future__ import annotations

import dataclasses
import logging
import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
)

from ..common.privacy import register_identity
from ..model import annotation_draft as ad
from ..model import annotation_save as asave
from ..model import timefmt
from ..model import viewer_marks as vm
from ..model.errors import UserMessage
from ..workers.jobs import JobManager
from .edit_dialogs import ImportPreview, NoteEditor, SavePreview, ask_unsaved
from .inline_note import Hint, InlineNote

log = logging.getLogger(__name__)

BANNER_TEXT = "Edit mode: changes are a draft until you save"
#: How many of the last texts typed are kept for one-key reuse (this window only).
RECENT_TEXTS = 8
#: An import file bigger than this is refused (it is read in one go).
MAX_IMPORT_BYTES = 64 << 20


@dataclasses.dataclass
class SaveOutcome:
    """What the save task hands back to the window."""

    written: list[int]
    uids: set
    records: list | None
    locked: tuple
    error: UserMessage | None = None
    stopped: bool = False
    #: The notes were stored, but the signal could not be shown to be unchanged
    #: (or the notes read back were not the ones written): the window reopens
    #: the recordings, as it always did.
    needs_reopen: bool = False
    #: What the status line says when it went well (empty = the usual "added" sentence).
    message: str = ""
    #: The stored notes that were taken out of their recordings.
    stored: tuple = ()


class EditMode(QObject):
    """Owned by the window (``window.edit``)."""

    #: The draft or the mode changed: the window redraws notes and lists.
    changed = Signal()
    #: A save (or import) task ended; for tests and the window.
    save_finished = Signal(object)

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self.draft = ad.Draft()
        self.draft.stored_keys = self._stored_keys
        self.draft.stored_pairs = self._stored_pairs
        self._keys_of: list | None = None  # the marks the keys below were made from
        self._keys: frozenset = frozenset()
        self.on = False
        self.saving = False
        #: A save ended not knowing what it wrote and the recordings could not be
        #: read again: Save stays off, so nothing is added twice.
        self.stale = False
        #: The selected display Mark (a draft one, or a stored one).
        self.selected = None
        self.last_job = None
        #: The last texts typed in the inline field, newest first. In memory for
        #: this window only: a note's text can hold patient details, so it is
        #: never written to the preferences, a file or a log.
        self.recent_texts: list[str] = []
        self._inline: InlineNote | None = None
        #: What the inline field is for: ("new", time, end) or ("change", uid).
        self._inline_for: tuple | None = None
        self._types_of: list | None = None
        self._types: list[str] = []
        self._types_shown: tuple = ()
        #: N turned edit mode on (it was off): Esc on its field turns it off again.
        self._on_by_n = False
        self._pairs_of: list | None = None
        self._pairs: frozenset = frozenset()
        self._jobs: JobManager | None = None
        self.banner = QFrame(window)
        self.banner.setObjectName("EditBanner")
        self.banner.setVisible(False)
        row = QHBoxLayout(self.banner)
        row.setContentsMargins(8, 3, 8, 3)
        self.banner_label = QLabel(BANNER_TEXT, self.banner)
        self.banner_label.setObjectName("Caption")
        self.count_label = QLabel("", self.banner)
        self.count_label.setObjectName("Muted")
        self.undo_button = QPushButton("Undo", self.banner)
        self.undo_button.setToolTip("Undo the last change to the draft (Ctrl+Z)")
        self.redo_button = QPushButton("Redo", self.banner)
        self.redo_button.setToolTip("Redo (Ctrl+Shift+Z)")
        self.save_button = QPushButton("Save…", self.banner)
        self.save_button.setToolTip("See what will be added to the recordings, then add it")
        self.discard_button = QPushButton("Discard the draft", self.banner)
        for w in (self.banner_label, self.count_label):
            row.addWidget(w)
        row.addStretch(1)
        for w in (self.undo_button, self.redo_button, self.save_button, self.discard_button):
            row.addWidget(w)
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        self.save_button.clicked.connect(lambda: self.save())
        self.discard_button.clicked.connect(self.discard)
        self.style_banner(window.theme)
        self._sync_buttons()

    # -- look ----------

    def style_banner(self, theme) -> None:
        self.banner.setStyleSheet(
            f"#EditBanner {{ background: {theme.accent_soft}; border-bottom: 1px solid "
            f"{theme.border}; }}"
        )

    # -- the mode ----------

    @property
    def jobs(self) -> JobManager:
        if self._jobs is None:
            self._jobs = self.win.jobs or JobManager(self)
        return self._jobs

    def dispose(self) -> None:
        """At the window's close: only a manager made here is stopped."""
        if self._jobs is not None and self._jobs.parent() is self:
            self._jobs.dispose()

    def _stored_keys(self) -> frozenset:
        """:func:`ad.mark_key` of every stored note, made once per read of the
        notes (the window replaces ``marks`` with each read)."""
        marks = self.win.marks
        if marks is not self._keys_of:
            self._keys = frozenset(ad.mark_key(m) for m in marks)
            self._keys_of = marks
        return self._keys

    def _stored_pairs(self) -> frozenset:
        """``(recording, key)`` of every stored note: the same note in another recording is
        not a copy of it."""
        marks = self.win.marks
        if marks is not self._pairs_of:
            self._pairs = frozenset((m.recording, ad.mark_key(m)) for m in marks)
            self._pairs_of = marks
        return self._pairs

    def quick_types(self) -> tuple[str, ...]:
        s = self.win.settings
        return ad.parse_quick_types(s.get("viewer/quick_types") if s else "")

    def _busy(self) -> bool:
        """True (and the reason in the status line) while a save runs: the
        draft cannot change then."""
        if self.saving:
            self.win.status_message.setText("Wait until the notes are saved.")
        return self.saving

    def set_on(self, on: bool) -> bool:
        """Turn edit mode on or off. Turning it off with an unsaved draft
        asks Save / Discard / Cancel. Returns the mode afterwards."""
        if on == self.on:
            return self.on
        if self._busy():
            return self.on
        if on:
            archives = [
                i for i in range(len(self.win.data.recordings)) if self.win.data.is_archive(i)
            ]
            if archives:
                who = vm.join_letters(self.win._letters_of(archives))
                noun = "Recording" if len(archives) == 1 else "Recordings"
                self.win.last_refusal = (
                    f"{noun} {who} {'is' if len(archives) == 1 else 'are'} packed in a single "
                    "file (.mefd.tar), and notes cannot be added to those. Unpack "
                    "it first, then open it again."
                )
                self.win.status_message.setText(self.win.last_refusal)
                return self.on
        if not on and self.draft.dirty:
            answer = self.ask_unsaved("leave")
            if answer == "stay":
                return self.on
            if answer == "save":
                if not self.save(then_off=True):
                    return self.on
                return self.on  # turned off when the save ends
            self.draft.clear()
        self._apply_mode(on)
        return self.on

    def _apply_mode(self, on: bool) -> None:
        self.on = on
        self._on_by_n = False
        w = self.win
        (w.features.add if on else w.features.discard)("edit")
        self.banner.setVisible(on)
        w.notes_tab.set_editing(on)
        w.edit_button.blockSignals(True)
        w.edit_button.setChecked(on)
        w.edit_button.blockSignals(False)
        if not on:
            self.selected = None
        w.help.set_features(frozenset(w.features))
        self.refresh()

    def ask_unsaved(self, what: str) -> str:
        """Save / Discard / Cancel (replaced in tests)."""
        return ask_unsaved(self.win, what)

    def confirm_close(self) -> bool:
        """The window is closing: True when it may. Asks when a draft is unsaved."""
        if self._busy():
            return False
        if not self.draft.dirty:
            return True
        answer = self.ask_unsaved("close")
        if answer == "discard":
            self.draft.clear()
            return True
        if answer == "save":
            self.save(then_close=True)
        return False

    # -- selection and views ----------

    def refresh(self) -> None:
        w = self.win
        w._rebuild_types()
        w._refresh_note_list()
        w._update_marks_view()
        self._sync_buttons()
        self.changed.emit()

    def _sync_buttons(self) -> None:
        d = self.draft
        w = self.win
        for b in (
            w.notes_tab.add_button,
            w.notes_tab.edit_button,
            w.notes_tab.remove_button,
            w.notes_tab.import_button,
        ):
            b.setEnabled(not self.saving)
        self.undo_button.setEnabled(d.can_undo and not self.saving)
        self.redo_button.setEnabled(d.can_redo and not self.saving)
        self.save_button.setEnabled(d.dirty and not self.saving and not self.stale)
        self.discard_button.setEnabled(d.dirty and not self.saving)
        n_add = len(d.additions())
        n_copy = d.stored_changes
        parts = []
        if n_add:
            parts.append(
                f"{timefmt.format_count(n_add, 'new note or event', 'new notes and events')}"
            )
        if n_copy:
            parts.append(f"{timefmt.format_count(n_copy, 'change')} to stored notes")
        text = ", ".join(parts) if parts else "No changes yet"
        if self.stale:
            text += ". Saving is off: open the recordings again."
        self.count_label.setText(text)
        # the quiet reminder in the status bar: what is not in the recordings yet
        n_all = n_add + n_copy
        label = getattr(self.win, "status_draft", None)  # not built yet at construction
        if label is not None:
            label.setText(
                f"{timefmt.format_count(n_all, 'note', 'notes')} not saved yet" if n_all else ""
            )
            label.setVisible(bool(n_all))

    def select(self, mark) -> None:
        self.selected = mark
        self.win.chips.selected_mark = mark
        self.win.chips.update()
        self.win.notes_tab.select_mark(mark)

    def on_chip(self, chip) -> None:
        if self.on and chip.mark is not None:
            self.select(chip.mark)

    def on_row(self, mark) -> None:
        if self.on:
            self.select(mark)

    # -- adding ----------

    def _anchor(self) -> tuple[int, int | None]:
        """The time (and end, for a selection) to add at: the Shift+drag
        selection, else the pointer, else the middle of the page."""
        w = self.win
        lo, length = int(w.state.t0), int(w.page_us())
        hi = lo + length
        sel = w.canvas.selection
        if sel is not None and int(sel[0]) < hi and int(sel[1]) > lo:
            return int(sel[0]), int(sel[1])
        # never a time that is not on the page shown (a pointer time from before a page turn)
        if w._pointer_t is not None and lo <= int(w._pointer_t) < hi:
            return int(w._pointer_t), None
        return lo + length // 2, None

    def _check_values(self, values: dict, ignore_uid: int | None = None) -> str:
        item = ad.DraftItem(
            0, values["time"], values["end"], values["label"], "", values["channel"]
        )
        r = ad.route(item, self.win.data.holders_at)
        if r.status == "outside":
            return r.reason
        same = ad.DraftItem(
            0, values["time"], values["end"], values["label"], values.get("text", ""),
            values["channel"],
        )  # fmt: skip
        return self.draft.duplicate_reason(same, ignore_uid=ignore_uid)

    def add_quick(self, number: int) -> None:
        """Keys 1 to 9: the quick type. A plain note opens the editor (it
        needs words); the others are added at once, with no text."""
        if self._busy():
            return
        types = self.quick_types()
        if not 1 <= number <= len(types):
            return
        label = types[number - 1]
        if label.casefold() == ad.PLAIN.casefold():
            self.open_new(label=label)
            return
        time, end = self._anchor()
        self.add(time, end, label)

    def add(self, time: int, end: int | None, label: str = ad.PLAIN, text: str = "",
            channel: str | None = None):  # fmt: skip
        """Add to the draft; refused (with the reason in the status line) when no
        recording holds data there."""
        if self._busy():
            return None
        problem = self._check_values(
            {"time": time, "end": end, "label": label, "text": text, "channel": channel}
        )
        if problem:
            self.win.last_refusal = problem
            self.win.status_message.setText(problem)
            return None
        try:
            item = self.draft.add(time, end, label, text, channel)
        except ValueError as exc:
            self.win.last_refusal = str(exc)
            self.win.status_message.setText(str(exc))
            return None
        self.refresh()
        self.select_item(item)
        return item

    def select_item(self, item) -> None:
        for m in self.win._display_marks():
            if m.uid == item.uid and m.state in ("new", "changed"):
                self.select(m)
                return

    def _make_editor(self, title: str, ignore_uid: int | None = None) -> NoteEditor:
        return NoteEditor(
            self.win.zone,
            self.quick_types(),
            self.win._channels,
            lambda values: self._check_values(values, ignore_uid),
            title,
            self.win,
        )

    def ask_note(self, editor: NoteEditor) -> dict | None:
        """Run the editor (replaced in tests)."""
        if editor.exec():
            return editor.values()
        return None

    def open_new(self, time: int | None = None, end: int | None = None, label: str = ad.PLAIN,
                 channel: str | None = None):  # fmt: skip
        if self._busy():
            return None
        if not self.on:
            return None
        if time is None:
            time, end = self._anchor()
        editor = self._make_editor("Add a note or event")
        editor.set_values(time, end, label, "", channel)
        values = self.ask_note(editor)
        if values is None:
            return None
        try:
            item = self.draft.add(**values_to_add(values))
        except ValueError as exc:
            self.win.last_refusal = str(exc)
            self.win.status_message.setText(str(exc))
            return None
        self.refresh()
        self.select_item(item)
        return item

    def add_here(self) -> None:
        """The key ``A`` and the notes list's Add button."""
        self.open_new()

    # -- adding fast: the key N and the inline field ----------

    @property
    def inline(self) -> InlineNote:
        if self._inline is None:
            self._inline = InlineNote(self.win.canvas)
            self._inline.kept.connect(self._inline_kept)
            self._inline.dropped.connect(self._inline_dropped)
        return self._inline

    def _recording_types(self) -> list[str]:
        """The types of note the recordings already hold (their quick labels:
        what the type list shows), most used first, then the quick types."""
        marks = self.win.marks
        shown = self._all_shown_state()
        if marks is not self._types_of or shown != self._types_shown:
            counts = vm.type_counts(marks, self.win._shown)
            self._types = [label for key, label, _n in counts if key.startswith(vm.QUICK_PREFIX)]
            self._types_of = marks
            self._types_shown = shown
        out = list(self._types)
        for label in self.quick_types():
            if label.casefold() != ad.PLAIN.casefold() and label.casefold() not in {
                x.casefold() for x in out
            }:
                out.append(label)
        return out

    def _all_shown_state(self) -> tuple:
        w = self.win
        return tuple(bool(w._shown(i)) for i in range(len(w.data.recordings)))

    def _details_shown(self) -> bool:
        """Whether the details of every recording are shown (no hidden text can be near)."""
        return all(self._all_shown_state())

    def hints(self) -> list[Hint]:
        """What the inline field offers for one key: the last texts typed, then
        types (about six in all, the texts first but at most four)."""
        # texts typed while details were shown are offered only while they are shown
        texts = self.recent_texts[:4] if self._details_shown() else []
        out = [Hint("text", t) for t in texts]
        for label in self._recording_types():
            if len(out) >= 6:
                break
            out.append(Hint("type", label))
        return out

    def remember_text(self, text: str) -> None:
        text = " ".join(text.split())
        if not text:
            return
        self.recent_texts = [t for t in self.recent_texts if t.casefold() != text.casefold()]
        self.recent_texts.insert(0, text)
        del self.recent_texts[RECENT_TEXTS:]

    def quick_note(self) -> None:
        """The key N: a note at the pointer (or over the selection) in a few
        keystrokes. Turns edit mode on when it is off."""
        if self._busy():
            return
        if self.inline.is_open:
            self.inline.field.setFocus()
            return
        started = not self.on
        if started and not self.set_on(True):
            return
        self._on_by_n = started
        time, end = self._anchor()
        problem = self._check_values(
            {"time": time, "end": end, "label": ad.PLAIN, "text": "", "channel": None}
        )
        if problem and problem not in (ad.DUPLICATE_STORED, ad.DUPLICATE_DRAFT):
            self.win.last_refusal = problem
            self.win.status_message.setText(problem)
            self._leave_if_by_n()
            return
        self._inline_for = ("new", time, end)
        when = timefmt.format_time(time, self.win.zone, 3)
        head = f"Note at {when}" + (
            f" lasting {timefmt.format_span((end - time) / 1e6)}" if end is not None else ""
        )
        self._place_inline(time, head, "")

    def change_text(self, uid: int) -> None:
        """Change the text of a draft note in the inline field."""
        if self._busy():
            return
        item = self.draft.get(uid)
        if item is None:
            return
        self._inline_for = ("change", uid)
        when = timefmt.format_time(item.time, self.win.zone, 3)
        self._place_inline(item.time, f"Change the note at {when}", item.stored_text)

    def _place_inline(self, time: int, head: str, text: str) -> None:
        w = self.win
        c = w.canvas
        row = w._pointer_row if w._pointer_t is not None else None
        y = c.row_y(row) + c.row_height * 0.5 if row is not None else c.plot_rect().top() + 8
        self.inline.open_at(c.x_of(time), y, head, text, self.hints())

    def _leave_if_by_n(self) -> None:
        """N turned edit mode on and nothing came of it (Esc, a refusal): it goes off again, as
        it was. Once the draft holds something the mode is the person's."""
        if self._on_by_n and self.on and not self.draft.dirty and not self.saving:
            self.set_on(False)
        elif self.draft.dirty:
            self._on_by_n = False

    def _inline_dropped(self) -> None:
        self._inline_for = None
        self._leave_if_by_n()
        self.win.canvas.setFocus()

    def _inline_kept(self, typed: str, save: bool) -> None:
        what, self._inline_for = self._inline_for, None
        self.win.canvas.setFocus()
        if what is None or self._busy():
            return
        try:
            self._inline_kept_note(what, typed, save)
        finally:
            self._leave_if_by_n()

    def _inline_kept_note(self, what, typed: str, save: bool) -> None:
        label, text = ad.split_text(typed.strip())
        if what[0] == "change":
            item = self.draft.get(what[1])
            if item is None:
                return
            try:
                item = self.draft.update(item.uid, label=label, text=text)
            except ValueError as exc:
                self.win.last_refusal = str(exc)
                self.win.status_message.setText(str(exc))
                return
            self.remember_text(typed)
            self.refresh()
            self.select_item(item)
            return
        _kind, time, end = what
        item = self.add(time, end, label, text)
        if item is None:
            return
        self.remember_text(typed)
        if end is not None:
            self.win.canvas.clear_selection()
        self.win.status_message.setText(
            "Note added to the draft. Ctrl+S saves it; it is not in the recording yet."
        )
        if save:
            self.save()

    # -- changing and taking out ----------

    def _target(self):
        m = self.selected
        if m is None:
            self.win.status_message.setText("Select a note or event first.")
            return None
        return m

    def edit_selected(self):
        if self._busy():
            return None
        m = self._target()
        if m is None:
            return None
        w = self.win
        if m.state in ("new", "changed"):
            item = self.draft.get(m.uid)
            if item is None:
                return None
            values0 = (item.time, item.end, item.label, item.text, item.channel)
            stored = None
        else:
            if m.state == "removed" or self.draft.is_removed(m):
                w.status_message.setText("This note is taken out. Undo to bring it back.")
                return None
            if not w._shown(m.recording):
                w.last_refusal = (
                    "Show the details of this recording first (the Show button in the list): "
                    "changing a note shows its text."
                )
                w.status_message.setText(w.last_refusal)
                return None
            stored = m
            label, text = ad.split_text(m.text)
            values0 = (m.time, m.end, label, text, m.channel)
        changed = item if stored is None else self.draft.changed_version(stored)
        own = getattr(changed, "uid", None)
        editor = self._make_editor("Change a note or event", own)
        editor.set_values(*values0)
        values = self.ask_note(editor)
        if values is None:
            return None
        changes = values_to_add(values)
        try:
            if stored is None:
                item = self.draft.update(item.uid, **changes)
            else:
                item = self.draft.edit_stored(stored, **changes)
        except ValueError as exc:
            w.last_refusal = str(exc)
            w.status_message.setText(str(exc))
            return None
        self.refresh()
        self.select_item(item)
        return item

    def remove_note(self) -> bool:
        """The key Delete and the menu entry: a note of the DRAFT leaves it (Ctrl+Z brings
        it back). A STORED note is taken out of its recording after one plain line asks
        (:meth:`_remove_stored`); that cannot be undone."""
        if self._busy():
            return False
        m = self._target()
        if m is None:
            return False
        return self.remove_mark(m)

    def remove_mark(self, m) -> bool:
        w = self.win
        if m.state in ("new", "changed"):
            item = self.draft.get(m.uid)
            if item is None:
                return False
            self.draft.remove(m.uid)
            self.select(None)
            self.refresh()
            w.status_message.setText("The note left the draft. Ctrl+Z brings it back.")
            return True
        return self._remove_stored(m)

    # -- taking out a STORED note (mef3io 2.4): ask once, then one task ----------

    def _identical_copies(self, m) -> int:
        """How many stored notes the viewer shows that are exactly ``m`` (1 = only itself)."""
        return max(1, sum(1 for x in self.win.marks if x == m and not x.state))

    def _refuse_here(self, text: str) -> bool:
        self.win.last_refusal = text
        self.win.status_message.setText(text)
        return False

    def _remove_stored(self, m) -> bool:
        """Delete / the menu on a stored note: one plain line asks first (this cannot be
        undone from the app), then a task takes it out of the recording and the notes are
        read again. A note stored twice is said so, and one copy or all of them can go."""
        w = self.win
        reason = asave.stored_removal_reason()
        if reason is not None:
            return self._refuse_here(reason)
        if w.data.is_archive(m.recording):
            return self._refuse_here(
                "A single-file recording cannot be changed: unpack it in Explore first."
            )
        copies = self._identical_copies(m)
        where = w.recording_label(m.recording)
        if copies == 1:
            line = f"Remove this note from {where}? This cannot be undone."
        else:
            times = "twice" if copies == 2 else f"{copies} times"
            line = (
                f"This note is stored {times} in {where}. Remove one copy, or all of them? "
                "This cannot be undone."
            )
        answer = self.ask_remove_stored(line, copies)
        if not answer:
            return False
        keep = 1 if answer == "one" else copies
        self._run_stored([asave.StoredChange(m, copies, keep)])
        return True

    def ask_remove_stored(self, line: str, copies: int):
        """The one line before a stored note is taken out (replaced in tests). Returns
        ``"all"`` (remove it, with every copy), ``"one"`` (remove one of several identical
        copies) or None (do nothing)."""
        verb = "Replace" if line.startswith("Replace") else "Remove"
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            f"{verb} the note?",
            line,
            QMessageBox.StandardButton.NoButton,
            self.win,
        )
        role = QMessageBox.ButtonRole.DestructiveRole
        one = box.addButton("Remove one copy", role) if copies > 1 else None
        every = box.addButton(f"Remove all {copies}" if copies > 1 else verb, role)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        if one is not None and clicked is one:
            return "one"
        return "all" if clicked is every else None

    def ask_other_password(self, label: str, letter: str) -> str | None:
        """The password the app does not hold, for a protected recording (replaced in
        tests): its basic or full-access password, or None."""
        text, ok = QInputDialog.getText(
            self.win,
            "Password needed",
            f"To change the notes of recording {letter}, both its passwords are needed.\n"
            f"Type its {label}:",
            QLineEdit.EchoMode.Password,
        )
        return text if ok and text else None

    def _run_stored(
        self, changes, then_off: bool = False, then_close: bool = False, continue_with=None
    ) -> None:
        """Take the stored notes of ``changes`` out (and add their replacements) as ONE task:
        the facts about protection are read first (a task), the other password is asked for,
        then the library is called on the task's thread, and only the notes are read again."""
        w = self.win
        data = w.data
        self.saving = True  # the draft is locked from here (the facts, the question, the task)
        self._sync_buttons()

        def ready() -> None:
            if data is not w.data:  # the window moved on
                self._prepare_ended()
                return
            passwords: dict[int, str] = {}
            for index in sorted({c.mark.recording for c in changes}):
                label = self.other_password_label(index)
                if label:
                    typed = self.ask_other_password(label, w._rec_letter(index))
                    if not typed:
                        self._prepare_ended()
                        return
                    passwords[index] = typed
            self._start_stored(changes, passwords, then_off, then_close, continue_with)

        if data.facts_loaded:
            ready()
            return
        handle = self.jobs.submit("Checking the recordings", lambda ctx: data.read_facts())
        handle.succeeded.connect(lambda _r: ready())
        handle.failed.connect(self._prepare_failed)
        handle.cancelled.connect(lambda: self._prepare_failed(None))

    def _start_stored(self, changes, passwords, then_off, then_close, continue_with) -> None:
        w = self.win
        data = w.data
        removed = sum(c.remove for c in changes if c.replacement is None)
        changed = sum(1 for c in changes if c.replacement is not None)
        said = []
        if removed:
            said.append(f"{timefmt.format_count(removed, 'note')} removed")
        if changed:
            said.append(f"{timefmt.format_count(changed, 'note')} changed")
        message = " and ".join(said) + "."

        def work(ctx):
            result = asave.remove_stored_notes(
                data,
                changes,
                passwords,
                ctx.check_cancelled,
                lambda done, total: ctx.report(done, total, "Taking out the notes"),
                register_identity,
                ctx.cancel_token,
            )
            records, locked, again = None, (), False
            if result.written:
                ctx.report(0, None, "Reading the notes again")
                try:
                    # the signal did not change: only the notes are read again
                    if data.refresh_notes(dict(result.net), result.signals_same):
                        records, locked = data.annotations(), tuple(data.annotations_locked)
                    else:
                        again = True
                except Exception:  # noqa: BLE001 - the window then reopens
                    again = True
            return SaveOutcome(
                result.written, result.uids, records, locked, result.error, result.stopped,
                again, message if result.error is None else "", tuple(result.marks),
            )  # fmt: skip

        try:
            handle = self.jobs.submit("Taking out notes", work)
        except Exception:  # noqa: BLE001 - the draft must never stay locked
            log.exception("the removal could not be started")
            self._prepare_ended()
            self.show_problem(
                UserMessage(
                    "The notes could not be changed",
                    "Something went wrong before anything was changed. Nothing was removed.",
                )
            )
            return
        self.last_job = handle
        handle.succeeded.connect(
            lambda out: self._stored_done(out, then_off, then_close, continue_with)
        )
        handle.failed.connect(self._stored_unknown)
        handle.cancelled.connect(lambda: self._stored_unknown(None))

    def _stored_done(self, out: SaveOutcome, then_off, then_close, continue_with) -> None:
        w = self.win
        if out.uids:
            self.draft.written(out.uids)  # the changes that were stored leave the draft
        self.draft.stored_done(out.stored)
        if out.needs_reopen:
            self._reopen_after_save(out, False, False)  # the draft stays locked
            return
        self.saving = False
        if out.written:
            w.notes_refreshed(out.records, out.locked)
        if continue_with is not None and out.error is None:
            self.select(None)
            self.refresh()
            continue_with()
            return
        self._after_save(out, then_off, then_close)

    def _stored_unknown(self, message) -> None:
        """The task ended without an outcome: what the recording holds is not known, so the
        recordings are read again and the notes shown are what they hold now."""
        out = SaveOutcome([], set(), None, (), message, message is None)
        if message is None:
            out.message = "The removal was stopped."
        self._reopen_after_save(out, False, False)

    def note_menu(self, menu, mark) -> None:
        """The entries of the menu on a note (right-click on its chip, its row in the
        list, or its mark on the traces)."""
        if self.on and not self.saving:
            self.select(mark)
        if mark.state in ("new", "changed"):
            menu.addAction("Change the text", lambda: self.change_text(mark.uid))
            menu.addAction("Remove this note (Delete)", lambda: self.remove_mark(mark))
            return
        remove = menu.addAction("Remove this note", lambda: self.remove_mark(mark))
        reason = asave.stored_removal_reason()
        remove.setEnabled(reason is None and not self.saving and mark.state != "removed")
        if reason is not None:
            remove.setToolTip(reason)
            note = menu.addAction(reason)  # the reason is read without hovering
            note.setEnabled(False)

    def remove_selected(self) -> bool:
        if self._busy():
            return False
        m = self._target()
        if m is None:
            return False
        if m.state in ("new", "changed"):
            self.draft.remove(m.uid)
            self.select(None)
            self.refresh()
            return True
        if m.state == "removed":
            self.win.status_message.setText("This note is already taken out.")
            return False
        return self._remove_stored(m)

    # -- undo / redo / discard ----------

    def undo(self) -> None:
        if self._busy():
            return
        if self.draft.undo():
            self.select(None)
            self.refresh()

    def redo(self) -> None:
        if self._busy():
            return
        if self.draft.redo():
            self.select(None)
            self.refresh()

    def discard(self) -> None:
        if self._busy():
            return
        if self.draft.dirty:
            self.draft.clear()
            self.select(None)
            self.refresh()

    # -- import ----------

    def import_file(self, path: str | None = None):
        """Read a CSV or JSON file as a task, then show what it holds; the
        user decides whether it enters the draft."""
        if not self.on or self.saving:
            return None
        w = self.win
        if path is None:
            path, _ = QFileDialog.getOpenFileName(
                w, "Add notes and events from a file", "", "Notes (*.csv *.json)"
            )
        if not path:
            return None
        kind = "json" if path.lower().endswith(".json") else "csv"
        holders = w.data.holders_at

        def work(ctx):
            ctx.report(0, None, "Reading the file")
            if os.path.getsize(path) > MAX_IMPORT_BYTES:
                result = ad.ImportResult(fatal="The file is too big to import.")
            else:
                with open(path, encoding="utf-8-sig", newline="") as handle:
                    text = handle.read()
                ctx.check_cancelled()
                result = ad.read_rows(text, kind)
            ctx.report(0, None, "Checking where the rows fall")
            return ad.preview_import(result, holders)

        handle = self.jobs.submit("Reading a file of notes and events", work)
        self.last_job = handle
        handle.succeeded.connect(self._import_read)
        handle.failed.connect(self._job_failed)
        return handle

    def ask_import(self, preview: ad.ImportPreview) -> bool:
        """Show the import preview (replaced in tests)."""
        return ImportPreview(preview, self.win.zone, self.win).exec() == 1

    def _import_read(self, preview) -> None:
        if not self.on or self._busy():
            return
        if not self.ask_import(preview) or not preview.can_add:
            return
        n = self.draft.add_many(r.as_row() for r in preview.usable)
        left = self.draft.skipped_duplicates
        text = f"{timefmt.format_count(n, 'note or event', 'notes and events')} added to the draft."
        if left:
            text += (
                f" {timefmt.format_count(left, 'row')} left out: "
                f"{'it is' if left == 1 else 'they are'} already stored or in the draft."
            )
        self.win.status_message.setText(text)
        self.refresh()

    # -- export ----------

    def export_marks(self, kind: str, scope: str = "listed", path: str | None = None) -> bool:
        """Save notes and events as CSV or JSON (times in the site zone with
        the offset, plus microseconds). Refused while any recording involved
        hides its details, unless the user shows them."""
        w = self.win
        marks = [m for m in (w._display_marks() if scope == "all" else w.notes_tab.rows)]
        marks = [m for m in marks if m.state != "removed"]
        if not marks:
            w.last_refusal = "There are no notes or events to save."
            w.status_message.setText(w.last_refusal)
            return False
        hidden = sorted({m.recording for m in marks if not w._shown(m.recording)})
        if hidden:
            if not w.ask_show(hidden):
                w.last_refusal = (
                    "Not saved: the details of recording "
                    + vm.join_letters(w._letters_of(hidden))
                    + " are hidden. Show them first."
                )
                w.status_message.setText(w.last_refusal)
                return False
            for i in hidden:
                w.show_recording_details(i)
            if any(not w._shown(i) for i in hidden):
                w.last_refusal = "Not saved: the recordings could not be shown."
                w.status_message.setText(w.last_refusal)
                return False
        if not w.confirm("Save the notes and events?", ad.EXPORT_NOTE):
            return False
        if path is None:
            path, _ = QFileDialog.getSaveFileName(
                w, "Save the notes and events", f"notes.{kind}", f"{kind.upper()} (*.{kind})"
            )
        if not path:
            return False
        entries = []
        for m in marks:
            label, text = ad.split_text(m.text) if m.code in ("Note", "EDFA") else (None, m.text)
            if label is None:
                _key, label = vm.kind_for(m, True)
            entries.append((m.time, m.end, label, text, m.channel))
        rows = ad.export_rows(entries, w.zone)
        body = ad.to_csv(rows) if kind == "csv" else ad.to_json(rows)
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(body)
        except OSError:
            w.last_refusal = "The file could not be written. Nothing was saved."
            w.status_message.setText(w.last_refusal)
            return False
        w.status_message.setText("The notes and events were saved.")
        return True

    # -- saving ----------

    def other_password_label(self, index: int) -> str | None:
        """For a recording with passwords: which one the app does NOT hold
        (it must be asked for), else None."""
        facts = self.win.data.facts(index)
        if facts is None or not facts[0]:
            return None
        return "basic password" if facts[1] == "full" else "full-access password"

    def quick_save_text(self, plan) -> str | None:
        """The one line asked before a save that ONLY ADDS notes to recordings
        that need no password typed: how many, and into which recording. None
        when the save needs the full preview (a choice of recording, a note
        outside every recording, changes to stored notes, a password)."""
        if plan.count == 0 or plan.ambiguous or plan.refused or plan.needs_copy:
            return None
        if any(self.other_password_label(i) for i in plan.by_recording):
            return None
        w = self.win
        where = vm.join_letters([w.recording_label(i) for i in sorted(plan.by_recording)])
        n = plan.count
        return (
            f"Save {timefmt.format_count(n, 'new note', 'new notes')} into {where}? "
            "A saved note can be removed again, but not brought back."
        )

    def ask_quick_save(self, text: str) -> bool:
        """One line, Save or Cancel (replaced in tests)."""
        box = QMessageBox(
            QMessageBox.Icon.Question,
            "Save the new notes?",
            text,
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
            self.win,
        )
        box.setDefaultButton(QMessageBox.StandardButton.Save)
        box.exec()
        return box.standardButton(box.clickedButton()) == QMessageBox.StandardButton.Save

    def save_key(self) -> None:
        """Ctrl+S."""
        if self.inline.is_open:  # the note being written is kept first
            self.inline.keep_now(save=True)
            return
        if not self.draft.dirty:
            self.win.status_message.setText("There are no new notes to save.")
            return
        self.save()

    def ask_save(self, plan_for):
        """Show what will be written and ask (replaced in tests): returns
        ``(choices, passwords)`` - the choices for whole-recording notes that
        fall where several recordings hold data, and the other password of
        each protected recording - or None to stop. A save that only adds notes
        is one line (:meth:`quick_save_text`); anything else shows the preview."""
        w = self.win
        line = self.quick_save_text(plan_for({}))
        if line is not None:
            return ({}, {}) if self.ask_quick_save(line) else None
        dialog = SavePreview(
            plan_for, w.recording_label, w._rec_letter, w.zone, w, self.other_password_label
        )
        if dialog.exec() != 1:
            return None
        return dialog.choices(), dialog.passwords()

    def show_problem(self, message: UserMessage) -> None:
        """Say what went wrong, in plain words (replaced in tests)."""
        self.win.last_refusal = message.text
        self.win.status_message.setText(message.title)
        box = QMessageBox(QMessageBox.Icon.Warning, message.title, message.text, parent=self.win)
        box.exec()

    def save(self, then_off: bool = False, then_close: bool = False) -> bool:
        """Preview, confirm, then append the draft's new notes and events to
        their recordings as one task. Returns whether a save was started."""
        w = self.win
        if self.saving or self.stale or not self.draft.dirty:
            return False
        data = w.data

        def plan_for(choices):
            return self.draft.save_plan(data.holders_at, choices)

        changes = self._stored_changes()
        if changes:
            # Changed stored notes are written first, each as "remove + add" in one library
            # call; whatever is new in the draft follows with the usual save.
            return self._save_stored(changes, then_off, then_close)
        first = plan_for({})
        if first.count == 0 and first.duplicates and not first.ambiguous and not first.refused:
            # Everything new is already stored: nothing to write, and nothing twice.
            self._drop_duplicates(first)
            return False
        if first.count == 0 and not first.ambiguous and not first.refused:
            return False  # nothing left to write
        if not data.facts_loaded:
            # What the preview needs (which recordings are protected) is read
            # once, on a task thread: opening and paging never inspect.
            self.saving = True  # the draft is locked from here (reading the facts, the preview)
            self._sync_buttons()
            handle = self.jobs.submit("Checking the recordings", lambda ctx: data.read_facts())
            handle.succeeded.connect(
                lambda _r: self._ask_then_save(data, plan_for, then_off, then_close)
            )
            handle.failed.connect(self._prepare_failed)
            # A stopped task (or one removed from the queue) ends the same way,
            # without a message: nothing was asked, nothing was written.
            handle.cancelled.connect(lambda: self._prepare_failed(None))
            return True
        return self._ask_then_save(data, plan_for, then_off, then_close)

    def _stored_changes(self) -> list:
        """The stored notes the draft changes (and any it takes out): what a save writes by
        removing the old note and adding its new version in ONE library call. A note stored
        twice has only one copy changed."""
        out = []
        for stored, item in self.draft.changed_stored():
            record = item.record()
            if item.channel:
                record["channel"] = item.channel
            out.append(
                asave.StoredChange(
                    stored, self._identical_copies(stored), 1, replacement=record, uid=item.uid
                )
            )
        for stored in self.draft.removed_stored():
            copies = self._identical_copies(stored)
            out.append(asave.StoredChange(stored, copies, copies))
        return out

    def _save_stored(self, changes, then_off: bool, then_close: bool) -> bool:
        """Ask once (the old text cannot be brought back), then replace the stored notes, then
        save what is new in the draft. Returns whether a task was started."""
        w = self.win
        reason = asave.stored_removal_reason()
        if reason is not None:
            self._refuse_here(reason)
            return False
        for c in changes:
            if w.data.is_archive(c.mark.recording):
                self._refuse_here(
                    "A single-file recording cannot be changed: unpack it in Explore first."
                )
                return False
        where = vm.join_letters(
            [w.recording_label(i) for i in sorted({c.mark.recording for c in changes})]
        )
        n = len(changes)
        line = (
            f"Replace {timefmt.format_count(n, 'stored note')} in {where} with the changed "
            f"{'version' if n == 1 else 'versions'}? The old "
            f"{'wording' if n == 1 else 'wordings'} cannot be brought back."
        )
        if not self.ask_remove_stored(line, 1):
            return False
        more = bool(self.draft.additions())
        self._run_stored(
            changes,
            then_off and not more,
            then_close and not more,
            (lambda: self.save(then_off, then_close)) if more else None,
        )
        return True

    def _drop_duplicates(self, plan) -> None:
        """Items that equal a stored note leave the draft: a note is never stored twice."""
        if not plan.duplicates:
            return
        self.draft.written({i.uid for i in plan.duplicates})
        self.select(None)
        self.refresh()
        n = len(plan.duplicates)
        self.win.status_message.setText(
            f"{timefmt.format_count(n, 'note was', 'notes were')} already stored, so "
            f"{'it was' if n == 1 else 'they were'} not added again."
        )

    def _prepare_ended(self) -> None:
        """The save did not start (stopped, refused, failed): the draft is
        free again and the buttons say so. Same outcome as a cancelled
        preview always had: nothing is saved, the mode stays as it was and
        the window stays open (``then_off`` / ``then_close`` do not happen)."""
        self.saving = False
        self._sync_buttons()

    def _prepare_failed(self, message) -> None:
        self._prepare_ended()
        if message is not None:
            self.show_problem(message)

    def _ask_then_save(self, data, plan_for, then_off: bool, then_close: bool) -> bool:
        if data is not self.win.data:  # the window moved on
            self._prepare_ended()
            return False
        try:
            answer = self.ask_save(plan_for)
            plan = plan_for(answer[0]) if answer is not None else None
        except Exception:  # noqa: BLE001 - the draft must never stay locked
            log.exception("the save preview failed")
            self._prepare_ended()
            self.show_problem(
                UserMessage(
                    "The notes could not be saved",
                    "Something went wrong before anything was added. Nothing was saved, and "
                    "the changes stay in the draft.",
                )
            )
            return False
        if plan is None or not plan.can_save:
            self._prepare_ended()
            return False
        choices, passwords = answer
        self._start_save(plan, passwords, then_off, then_close)
        return True

    def _start_save(self, plan, passwords: dict, then_off: bool, then_close: bool) -> None:
        w = self.win
        data = w.data
        self.saving = True
        touched = {"writing": False}  # set once a write call is about to be made
        try:
            self._sync_buttons()

            def check(ctx):
                ctx.check_cancelled()  # a stop here is before the write: nothing was written
                touched["writing"] = True

            def work(ctx):
                result = asave.write_plan(
                    data,
                    plan,
                    passwords,
                    lambda: check(ctx),
                    lambda done, total: ctx.report(done, total, "Adding the notes and events"),
                    register_identity,
                )
                records, locked, again = None, (), False
                if result.uids:
                    ctx.report(0, None, "Reading the notes again")
                    appended = {
                        i: sum(1 for it in share.items if it.uid in result.uids)
                        for i, share in plan.by_recording.items()
                    }
                    try:
                        # the signal did not change: only the notes are read again, and the
                        # page on screen, the tile cache and the strip stay as they are
                        if data.refresh_notes(
                            {i: n for i, n in appended.items() if n}, result.signals_same
                        ):
                            records, locked = data.annotations(), tuple(data.annotations_locked)
                        else:
                            again = True
                    except Exception:  # noqa: BLE001 - the window then reopens, as it always did
                        again = True
                return SaveOutcome(
                    result.written, result.uids, records, locked, result.error, result.stopped,
                    again,
                )  # fmt: skip

            handle = self.jobs.submit("Adding notes and events", work)
        except Exception:  # noqa: BLE001 - the draft must never stay locked
            log.exception("the save could not be started")
            self._save_ended(None, False, started=False)
            self.show_problem(
                UserMessage(
                    "The notes could not be saved",
                    "Something went wrong before anything was added. Nothing was saved, and "
                    "the changes stay in the draft.",
                )
            )
            return
        self.last_job = handle
        handle.succeeded.connect(lambda out: self._saved(out, plan, then_off, then_close))
        handle.failed.connect(lambda m: self._save_ended(m, touched["writing"]))
        # Stopped before it ran, or a stop that came from inside the library.
        handle.cancelled.connect(lambda: self._save_ended(None, touched["writing"], stopped=True))

    def _saved(self, out: SaveOutcome, plan, then_off: bool, then_close: bool) -> None:
        w = self.win
        if plan.duplicates:  # the same as a stored note: never written, and no longer a draft
            self.draft.written({i.uid for i in plan.duplicates})
        if out.uids:
            # Exactly what was appended leaves the draft (a second save must not add it again).
            self.draft.written(out.uids)
        if out.needs_reopen:
            self._reopen_after_save(out, then_off, then_close)  # the draft stays locked
            return
        self.saving = False
        if out.uids:
            w.notes_refreshed(out.records, out.locked)
        self._after_save(out, then_off, then_close)

    def _after_save(self, out: SaveOutcome, then_off: bool, then_close: bool) -> None:
        w = self.win
        self.select(None)
        self.refresh()
        if out.error is not None:
            self.show_problem(out.error)
        else:
            w.status_message.setText(
                out.message
                or f"{timefmt.format_count(len(out.uids), 'note or event', 'notes and events')} "
                "added."
            )
            if then_off:
                self._apply_mode(False)
            if then_close:
                w.close()
        self.save_finished.emit(out)

    def _reopen_after_save(self, out: SaveOutcome, then_off: bool, then_close: bool) -> None:
        """The notes are stored but the signal could not be shown to be unchanged:
        the recordings are opened again (the page waits), as every save once did."""
        w = self.win
        data, controller = w.data, w.controller

        def work(ctx):
            ctx.report(0, None, "Reading the recordings again")
            controller.scheduler.drain(60.0)
            controller.drain_strip(60.0)  # a read of the index must end first
            data.reopen()
            return data.annotations(), tuple(data.annotations_locked)

        def done(result) -> None:
            self.saving = False
            out.records, out.locked = result
            w.end_data_change(out.records, out.locked)
            self._after_save(out, then_off, then_close)

        def failed(_message=None) -> None:
            self.saving = False
            # The notes on screen are the old ones: a note added again would be stored twice.
            self.stale = True
            w.end_data_change(None, (), reopened=False)
            self._sync_buttons()
            out.error = out.error or UserMessage(
                "The notes were added, but they cannot be shown",
                "The new notes and events are in the recordings, but the viewer could not "
                "read the recordings again. Close this window and open the recordings again.",
            )
            self._after_save(out, False, False)

        try:
            w.begin_data_change()
            handle = self.jobs.submit("Reading the recordings again", work)
        except Exception:  # noqa: BLE001
            log.exception("the recordings could not be read again")
            failed()
            return
        self.last_job = handle
        handle.succeeded.connect(done)
        handle.failed.connect(failed)
        handle.cancelled.connect(failed)

    def _save_ended(self, message, writing: bool, stopped: bool = False, started=True) -> None:
        """The task ended without an outcome (failed, or stopped from outside the
        write loop). The loop reports a stop and a failure itself, with exactly
        what was appended; getting here means that report was lost. When a write
        may have begun, the recordings may hold notes the draft still lists: they
        are read again (off the GUI thread), what is stored now leaves the draft,
        and only then is saving possible again."""
        if writing and started:
            self._look_again(message, stopped)
            return
        self.saving = False
        self._sync_buttons()
        if stopped and message is None and started:
            self.win.status_message.setText("The save was stopped. Nothing was added.")
        if message is not None:
            self.show_problem(message)
        self.save_finished.emit(None)

    def _look_again(self, message, stopped: bool) -> None:
        """After a save that ended in an unknown place: reopen and read the notes
        again, then drop from the draft every item that is stored now."""
        w = self.win
        data, controller = w.data, w.controller
        before = list(w.marks)
        self.saving = True  # the draft stays locked while the recordings are read
        self._sync_buttons()

        def work(ctx):
            ctx.report(0, None, "Reading the recordings again")
            controller.scheduler.drain(60.0)
            controller.drain_strip(60.0)
            data.reopen()
            return data.annotations(), tuple(data.annotations_locked)

        try:
            w.begin_data_change()  # they are opened again
            handle = self.jobs.submit("Reading the recordings again", work)
        except Exception:  # noqa: BLE001 - the window must say what is left to do
            log.exception("the recordings could not be read again")
            self._not_looked(stopped)
            return
        self.last_job = handle
        handle.succeeded.connect(lambda out: self._looked(out, before, stopped))
        handle.failed.connect(lambda _m: self._not_looked(stopped))
        handle.cancelled.connect(lambda: self._not_looked(stopped))

    def _looked(self, out, before, stopped: bool) -> None:
        w = self.win
        records, locked = out
        marks = vm.build_marks(records)
        uids = ad.items_now_stored(self.draft.additions(), before, marks)
        self.draft.written(uids)
        self.saving = False
        w.end_data_change(records, locked)
        self.select(None)
        self.refresh()
        n = len(uids)
        said = (
            f"{timefmt.format_count(n, 'note or event', 'notes and events')} had already been "
            "added to the recordings and left the draft."
            if n
            else "Nothing had been added to the recordings."
        )
        rest = " The others stay in the draft." if self.draft.dirty else ""
        self.show_problem(
            UserMessage(
                "The save was stopped" if stopped else "The save ended in an unexpected place",
                said + rest,
            )
        )
        self.save_finished.emit(None)

    def _not_looked(self, stopped: bool) -> None:
        """The recordings could not be read again: what they hold is not known."""
        self.saving = False
        self.stale = True
        self.win.end_data_change(None, (), reopened=False)
        self._sync_buttons()
        self.show_problem(
            UserMessage(
                "The save was stopped" if stopped else "The save ended in an unexpected place",
                "Some notes and events may already have been added to the recordings, and the "
                "viewer could not read them again to see. Saving is switched off so that "
                "nothing is added twice. Close this window and open the recordings again.",
            )
        )
        self.save_finished.emit(None)

    def _job_failed(self, message) -> None:
        if message is not None:
            self.show_problem(message)

    # -- menus ----------

    def context(self, menu, row: int, t: int) -> None:
        """Right-click entries (edit mode only)."""
        if not self.on or self.saving:
            return
        menu.addSeparator()
        channel = (
            self.win.canvas.rows[row].channel if 0 <= row < len(self.win.canvas.rows) else None
        )
        menu.addAction("Add a note or event here…", lambda: self.open_new(t, None, ad.PLAIN, None))
        if channel:
            menu.addAction(
                f"Add a note or event to channel {channel} here…",
                lambda: self.open_new(t, None, ad.PLAIN, channel),
            )
        quick = menu.addMenu("Add a quick type here")
        for n, label in enumerate(self.quick_types(), 1):
            if label.casefold() == ad.PLAIN.casefold():
                continue
            quick.addAction(f"{n}  {label}", lambda lab=label: self.add(t, None, lab))


def values_to_add(values: dict) -> dict:
    """The editor's values as arguments for the draft."""
    return {k: values[k] for k in ("time", "end", "label", "text", "channel")}
