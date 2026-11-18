"""The run half of a batch window, shared by "Make new recordings" and "Cut
into pieces": check up front (a task), run the ONE batch runner
(``workers.tasks.run_batch``), show each result, Stop, resume from a journal.

A window that uses it is a ``QDialog`` that has these members: ``jobs``,
``keep_awake``, ``passwords`` (a ``PasswordsStep``), ``messages``,
``progress_slot``, ``results_table``, ``summary``, ``open_button``, the run
state set in ``__init__`` (``batch``, ``results``, ``leftovers``, ``state``,
``stopped``, ``finished_run``, ``handle``, ``_busy``, ``_open_password``,
``_journal``, ``_preflight_problems``) and the methods ``go``, ``blocker``,
``make_batch`` and ``_changed``; plus the class constants ``STEP_WHERE`` (the
last step before the run), ``STEP_RUN``, ``NOUN`` (what is made: "new
recordings") and ``UNIT`` (what one is: "Recording"). Nothing here adds a
runner or keeps a password.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..common.activity import JobProgressWidget
from ..common.dialogs import TechnicalDetails
from ..common.pages import label as _label
from ..model import anonymize, pipeline
from ..model import journal as journal_
from ..model.errors import UserMessage
from ..workers.tasks import BatchState, preflight_batch, run_batch

CHECK_STEP_NOTE = (
    "This looks at every recording and at where the new ones will go, and writes nothing. If "
    "something is wrong you are told what and what to do, and every choice you made is "
    "kept. Stop ends the check and leaves everything as it was."
)
RUN_STEP_NOTE = (
    "Stop ends the one being made and removes what it had written. Recordings that are "
    "already saved and checked stay; the originals are never changed."
)
CHANGED_DURING_CHECK = (
    "A choice was changed while the check was running, so nothing was made: the check must "
    "judge exactly what is written. Look at the choices, then start again."
)
CHECK_STOPPED = "The check was stopped. Nothing was made. Your choices are kept."
NOT_CLOSED_TEXT = (
    "The window was not closed because the check or the copying is still going on. Press "
    "Stop on this page first (nothing half-made is left behind), then close the window."
)


class BatchRunMixin:
    STEP_WHERE = 0
    STEP_RUN = 0
    #: What is made, and one of it, in the window's words.
    NOUN = "new recordings"
    UNIT = "Recording"
    ONE = "a new recording"

    # -- step 6: running ------------------------------------------------------------------
    def _build_run(self) -> QWidget:
        page = QWidget(self)
        self.progress_slot = QVBoxLayout()
        self.messages = QVBoxLayout()
        self.results_table = QTableWidget(0, 2, page)
        self.results_table.setHorizontalHeaderLabels([self.UNIT, "Result"])
        self.results_table.verticalHeader().hide()
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.results_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.summary = _label()
        self.closing_note = _label()
        self.closing_note.setObjectName("Caution")
        self.closing_note.setVisible(False)
        layout = QVBoxLayout(page)
        self.run_heading = _label(f"Making the {self.NOUN}", bold=True)
        layout.addWidget(self.run_heading)
        layout.addWidget(self.closing_note)
        layout.addLayout(self.progress_slot)
        layout.addLayout(self.messages)
        layout.addWidget(self.results_table, 1)
        layout.addWidget(self.summary)
        return page

    def start(self):
        """Check everything that can be checked up front (in the
        background), then run the whole batch unattended."""
        if self._busy or self.step != self.STEP_WHERE or self.blocker():
            return None
        batch = self.make_batch()
        self._preflight_problems = []
        self._reset_run()
        self._set_busy(True)
        self.go(self.STEP_RUN)
        handle = self.jobs.submit("Checking everything before starting", preflight_batch, batch)
        self.handle = handle
        handle.succeeded.connect(lambda verdict: self._checked_if_current(batch, verdict))
        handle.failed.connect(self._check_failed)
        handle.cancelled.connect(self._check_stopped)
        self.run_heading.setText(
            f"Step 1 of 2: checking everything before the {self.NOUN} are made"
        )
        self.progress_slot.addWidget(
            JobProgressWidget(handle, self, step="Step 1 of 2", note=CHECK_STEP_NOTE)
        )
        return handle

    def _checked_if_current(self, batch: pipeline.Batch, verdict: dict) -> None:
        """The run starts only from a check of the choices that are on the
        wizard now (they cannot be changed from the run page; this holds for
        any other way a choice could change). A requested Stop always wins,
        also over a check that returned normally."""
        if self.handle is not None and self.handle.record.cancel_requested:
            self._check_stopped()
            return
        if pipeline.what_is_written(self.make_batch()) != pipeline.what_is_written(batch):
            self._clear(self.progress_slot)
            self._back_to_choices(CHANGED_DURING_CHECK)
            return
        self._checked(batch, verdict)

    def _back_to_choices(self, *problems: str) -> None:
        """Step 1 stopped or refused: nothing was written, every choice (the
        passwords too: they were not cleared) is as it was."""
        self._preflight_problems = list(problems)
        self._set_busy(False)
        self.go(self.STEP_WHERE)

    def _check_stopped(self) -> None:
        self._clear(self.progress_slot)
        self._back_to_choices(CHECK_STOPPED)

    def back_to_choices(self) -> None:
        """After a run (also a stopped one) back to the last step with every
        choice kept; the passwords were cleared when the run began, so they
        are asked for again. While a check or a run goes on: no way back."""
        if self.finished_run and not self._busy:
            self.go(self.STEP_WHERE)

    def show_last_result(self) -> None:
        """The outcome of the last run stays here until the next run starts."""
        if self.finished_run and not self._busy:
            self.go(self.STEP_RUN)

    def _reset_run(self) -> None:
        self.results = {}
        self.leftovers = []
        self.state = None
        self.stopped = False
        self.finished_run = False
        self._clear(self.messages)
        self._clear(self.progress_slot)
        self.results_table.setRowCount(0)
        self.summary.setText("")
        self.open_button.setVisible(False)

    def _checked(self, batch: pipeline.Batch, verdict: dict) -> None:
        self._clear(self.progress_slot)
        problems = list(verdict.get("problems", []))
        names = verdict.get("names", {})
        for n, item in enumerate(batch.items):
            message = names.get(item.request.key)
            if message is not None:
                problems.append(
                    f"{self.UNIT} {n + 1} of {len(batch.items)}: {message.title}. {message.text}"
                )
        if problems:
            self._back_to_choices(*["Nothing was made yet. " + p for p in problems])
            return
        self.run(batch)

    def _check_failed(self, message: UserMessage) -> None:
        self._clear(self.progress_slot)
        if self._journal is None:
            self._preflight_problems = [f"{message.title}. {message.text}"]
            self._set_busy(False)
            self.go(self.STEP_WHERE)
            return
        self._show_problems([f"{message.title}. {message.text}"])
        self._set_busy(False)

    def _show_problems(self, problems: list[str]) -> None:
        frame = QFrame()
        frame.setObjectName("Warning")
        inner = QVBoxLayout(frame)
        inner.addWidget(_label("Nothing was made yet", bold=True))
        for text in problems:
            inner.addWidget(_label(text))
        self.messages.addWidget(frame)

    def run(self, batch: pipeline.Batch, journal=None):
        """Run the batch. The task gets the passwords; the window keeps a
        copy without any, plus the copies' full-access password for "Show
        them in Explore" only."""
        self.batch = pipeline.without_passwords(batch)
        output = next(
            (i.request.output_password for i in batch.items if i.request.output_password), None
        )
        self._open_password = output[1] if output else ""
        self.passwords.forget()
        self._set_busy(True)
        self.go(self.STEP_RUN)
        self._fill_results()
        n = len(batch.items)
        title = f"Making {self.ONE}" if n == 1 else f"Making {n} {self.NOUN}"
        self.run_heading.setText(f"Step 2 of 2: {title[0].lower()}{title[1:]}")
        handle = self.jobs.submit(title, run_batch, batch, journal)
        self.handle = handle
        handle.published.connect(self._published)
        handle.cancelled.connect(lambda: setattr(self, "stopped", True))
        handle.failed.connect(
            lambda message: self.messages.addWidget(_label(f"{message.title}. {message.text}"))
        )
        handle.finished.connect(self._finished)
        self.progress_slot.addWidget(
            JobProgressWidget(handle, self, step="Step 2 of 2", note=RUN_STEP_NOTE)
        )
        return handle

    def resume(self, journal, password_for, output_password=None, ask_existing=None):
        """Continue an interrupted batch from its journal (passwords were
        never journaled: the caller asked for them again). Everything is
        checked again for the recordings still to do first."""
        batch = pipeline.from_payload(journal.payload, password_for, output_password)
        batch = replace(batch, keep_awake=self.keep_awake)
        self._journal = journal
        self._reset_run()
        self._set_busy(True)
        self.go(self.STEP_RUN)
        handle = self.jobs.submit(
            "Checking everything before continuing",
            preflight_batch,
            batch,
            list(journal.states),
            output_password[1] if output_password else "",
            journal,
        )
        self.handle = handle

        def done(verdict: dict) -> None:
            self._clear(self.progress_slot)
            problems = list(verdict.get("problems", []))
            existing = verdict.get("existing", [])
            if not problems and existing:
                # A name alone proves nothing about a new recording: keep it
                # as made (not confirmed) or ask for it to be moved.
                if ask_existing is not None and ask_existing(existing, len(batch.items)) == "keep":
                    for n in existing:
                        journal.payload["items"][n]["unconfirmed"] = True
                        journal.mark(n, journal_.DONE)
                else:
                    problems.append(
                        "A result with a new recording's name is already in the folder and the "
                        "app cannot tell whether it made it. It was left as it is. Move or "
                        "rename it, then continue."
                    )
            if problems:
                self._show_problems(problems)
                self._set_busy(False)
                return
            self.run(pipeline.with_originals(batch, verdict.get("originals", {})), journal)

        handle.succeeded.connect(done)
        handle.failed.connect(self._check_failed)
        handle.cancelled.connect(lambda: self._set_busy(False))
        self.progress_slot.addWidget(JobProgressWidget(handle, self))
        return handle

    def _published(self, items: list) -> None:
        for item in items:
            if isinstance(item, anonymize.Leftover):
                self.leftovers.append(item)
            elif isinstance(item, BatchState):
                self.state = item
            elif isinstance(item, anonymize.CopyResult):
                self.results[item.key] = item
        self._fill_results()

    def result_text(self, result: anonymize.CopyResult | None) -> str:
        if result is None:
            return "Waiting"
        if not result.ok:
            return result.message.title if result.message else "Not made"
        if "made before the interruption, not confirmed" in result.notes:
            return "Made before the interruption (not confirmed)"
        if "made earlier" in result.notes:
            return "Saved and checked (earlier)"
        return "Saved and checked"

    def _fill_results(self) -> None:
        if self.batch is None:
            return
        items = self.batch.items
        self.results_table.setRowCount(len(items))
        for n, item in enumerate(items):
            cells = (
                f"{n + 1} of {len(items)}: {item.request.name}",
                self.result_text(self.results.get(item.request.key)),
            )
            for col, text in enumerate(cells):
                self.results_table.setItem(n, col, QTableWidgetItem(text))
            message = getattr(self.results.get(item.request.key), "message", None)
            if message is not None:
                self.results_table.item(n, 1).setToolTip(message.text)

    def stop(self) -> None:
        if self.handle is not None:
            self.handle.cancel()

    def _finished(self) -> None:
        self._clear(self.progress_slot)
        self.finished_run = True
        self._set_busy(False)
        saved = [r for r in self.results.values() if r.ok]
        total = len(self.batch.items) if self.batch is not None else len(saved)
        text = f"Finished: {len(saved)} of {total} {self.NOUN} saved and checked."
        if self.state is not None and self.state.paused:
            text = (
                f"Paused: {len(saved)} of {total} done. The files could not be reached. Check "
                "the drive; the app offers to continue the next time it starts."
            )
        elif self.stopped:
            text = (
                f"Stopped: {len(saved)} of {total} saved and checked. Nothing half-made was "
                "left behind and the originals were not changed."
            )
        if self.leftovers:
            text += (
                " Some unfinished files could not be removed; the app will offer to remove them."
            )
        self.summary.setText(text)
        for result in self.results.values():
            if not result.ok and result.message is not None and result.message.detail:
                self.messages.addWidget(TechnicalDetails(result.message.detail))
                break
        self.open_button.setVisible(bool(saved))
        self._changed()

    def _open_results(self) -> None:
        paths = [r.destination for r in self.results.values() if r.ok]
        password, self._open_password = self._open_password, ""
        if paths:
            self.open_requested.emit(paths, password)

    # -- housekeeping -------------------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._changed()

    def _clear(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _wipe(self) -> None:
        """Forget every password (memory only)."""
        self.passwords.forget()
        self._open_password = ""

    def done(self, result) -> None:
        self._wipe()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._busy:
            # Not closed, and nothing is cleared: the run is still going on.
            event.ignore()
            self._say_not_closed()
            return
        self._wipe()
        super().closeEvent(event)

    def _say_not_closed(self) -> None:
        self.closing_note.setText(NOT_CLOSED_TEXT)
        self.closing_note.setVisible(True)

    def reject(self) -> None:
        if self._busy:
            self._say_not_closed()
            return
        super().reject()
