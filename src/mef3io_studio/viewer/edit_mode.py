"""Edit mode of the signal viewer: the switch, the banner, the draft, adding,
changing and taking out notes and events, importing, exporting and saving
(docs/design/viewer.md, "Annotations", "Edit mode").

Only ADDITIONS are written, by appending to a recording's notes. A change to
or a removal of a stored note stays in the draft, is shown, and waits for a
new copy (a later version); nothing here writes it.

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
    QLabel,
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

log = logging.getLogger(__name__)

BANNER_TEXT = "Edit mode: changes are a draft until you save"
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
        self.on = False
        self.saving = False
        #: The selected display Mark (a draft one, or a stored one).
        self.selected = None
        self.last_job = None
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
        self.save_button.setEnabled(d.dirty and not self.saving)
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
        self.count_label.setText(", ".join(parts) if parts else "No changes yet")

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
        sel = w.canvas.selection
        if sel is not None:
            return int(sel[0]), int(sel[1])
        if w._pointer_t is not None:
            return int(w._pointer_t), None
        return int(w.state.t0 + w.page_us() // 2), None

    def _check_values(self, values: dict, replacing_stored=None) -> str:
        item = ad.DraftItem(
            0, values["time"], values["end"], values["label"], "", values["channel"]
        )
        r = ad.route(item, self.win.data.holders_at)
        return r.reason if r.status == "outside" else ""

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
        problem = self._check_values({"time": time, "end": end, "label": label, "channel": channel})
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

    def _make_editor(self, title: str) -> NoteEditor:
        return NoteEditor(
            self.win.zone,
            self.quick_types(),
            self.win._channels,
            self._check_values,
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
        editor = self._make_editor("Change a note or event")
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

    def remove_selected(self) -> bool:
        if self._busy():
            return False
        m = self._target()
        if m is None:
            return False
        if m.state in ("new", "changed"):
            self.draft.remove(m.uid)
        elif m.state == "removed":
            w = self.win
            w.status_message.setText("This note is already taken out. Undo to bring it back.")
            return False
        else:
            self.draft.remove(m)
        self.select(None)
        self.refresh()
        return True

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
        self.win.status_message.setText(
            f"{timefmt.format_count(n, 'note or event', 'notes and events')} added to the draft."
        )
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

    def ask_save(self, plan_for):
        """Show what will be written and ask (replaced in tests): returns
        ``(choices, passwords)`` - the choices for whole-recording notes that
        fall where several recordings hold data, and the other password of
        each protected recording - or None to stop."""
        w = self.win
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
        if self.saving or not self.draft.dirty:
            return False
        data = w.data

        def plan_for(choices):
            return self.draft.save_plan(data.holders_at, choices)

        first = plan_for({})
        if first.count == 0 and not first.ambiguous and not first.refused:
            # Only changes to stored notes: shown, never written here.
            n = first.needs_copy
            self.show_problem(
                UserMessage(
                    "Nothing to add yet",
                    f"{timefmt.format_count(n, 'change')} to stored notes need a new copy "
                    "of the recording. That is available in a later version, so nothing can "
                    "be saved now. The changes stay in the draft.",
                )
            )
            return False
        answer = self.ask_save(plan_for)
        if answer is None:
            return False
        choices, passwords = answer
        plan = plan_for(choices)
        if not plan.can_save:
            return False
        self._start_save(plan, passwords, then_off, then_close)
        return True

    def _start_save(self, plan, passwords: dict, then_off: bool, then_close: bool) -> None:
        w = self.win
        data = w.data
        controller = w.controller
        self.saving = True
        w.begin_data_change()
        self._sync_buttons()

        def work(ctx):
            result = asave.write_plan(
                data,
                plan,
                passwords,
                ctx.check_cancelled,
                lambda done, total: ctx.report(done, total, "Adding the notes and events"),
                register_identity,
            )
            records, locked, error = None, (), result.error
            if result.uids:
                ctx.report(0, None, "Reading the recordings again")
                try:
                    controller.scheduler.drain(60.0)
                    data.reopen()
                except Exception:  # noqa: BLE001 - the notes are stored; the window must say so
                    error = error or UserMessage(
                        "The notes were added, but they cannot be shown",
                        "The new notes and events are in the recordings, but the viewer could "
                        "not read the recordings again. Close this window and open the "
                        "recordings again.",
                    )
                else:
                    try:
                        records, locked = data.annotations(), tuple(data.annotations_locked)
                    except Exception:  # noqa: BLE001 - the list then says it could not be read
                        records, locked = None, ()
            return SaveOutcome(result.written, result.uids, records, locked, error, result.stopped)

        handle = self.jobs.submit("Adding notes and events", work)
        self.last_job = handle
        handle.succeeded.connect(lambda out: self._saved(out, plan, then_off, then_close))
        handle.failed.connect(self._save_crashed)

    def _saved(self, out: SaveOutcome, plan, then_off: bool, then_close: bool) -> None:
        w = self.win
        self.saving = False
        if out.uids:
            # Exactly what was appended leaves the draft (a second save must not add it again).
            self.draft.written(out.uids)
            w.end_data_change(out.records, out.locked)
        else:
            w.end_data_change(None, (), reopened=False)
        self.select(None)
        self.refresh()
        if out.error is not None:
            self.show_problem(out.error)
        else:
            w.status_message.setText(
                f"{timefmt.format_count(len(out.uids), 'note or event', 'notes and events')} added."
            )
            if then_off:
                self._apply_mode(False)
            if then_close:
                w.close()
        self.save_finished.emit(out)

    def _save_crashed(self, message) -> None:
        self.saving = False
        self.win.end_data_change(None, (), reopened=False)
        self._sync_buttons()
        if message is not None:
            self.show_problem(message)
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
