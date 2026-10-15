"""The Explore page: open recordings, list them, and show what is in them.

Nothing here touches a recording's files. Every read goes through a
background task (``workers.tasks``) and the recording source, which runs
mef3io in a helper process, so the window never waits for the disk or for
the library. Each task shows its progress and can be stopped.

Read-only, except "Pack into one file" / "Unpack into a folder", which
always write a NEW recording next to (or away from) the original and never
replace anything.

Privacy: patient details are hidden until "Show" for that recording; every
identifying string the page learns (recording names, patient details,
passwords) is registered with the log filter as soon as it arrives; task
titles name no recording.
"""

from __future__ import annotations

import os
from datetime import UTC, tzinfo

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableView,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .._version import app_version, mef3io_version
from ..common import icons
from ..common.activity import JobProgressWidget
from ..common.dialogs import MessageDialog, TechnicalDetails
from ..common.flow import FlowLayout
from ..common.pages import scaled_font
from ..common.privacy import register_identity
from ..common.theme import LIGHT, Theme
from ..model.errors import UserMessage
from ..model.recording import InspectOutcome, is_recording_location, location_key
from ..model.source import LocalRecordingSource, RecordingSource
from ..model.summary import as_json, as_text, build_summary
from ..model.timefmt import format_count, resolve_zone, zone_label
from ..workers.jobs import JobManager
from ..workers.tasks import check_recording, open_recordings, pack_or_unpack, scan_and_open
from .dialogs import PasswordDialog
from .panels import (
    ChannelsPanel,
    CheckPanel,
    HistoryPanel,
    NotesPanel,
    OverviewPanel,
    PatientPanel,
    TimelinePanel,
)
from .state import RecordingEntry
from .table import COL_START, EntryRole, RecordingSortProxy, RecordingTableModel

TABS = (
    "Overview",
    "Channels",
    "Timeline",
    "Notes and events",
    "Patient details",
    "History",
    "Check for problems",
)


def _plural(n: int, one: str) -> str:
    return format_count(n, one)


class DropZone(QFrame):
    """Shown while the list is empty: what to do first."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DropZone")
        em = self.fontMetrics().height()
        self.icon = QLabel(self)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading = QLabel("Open a recording to see what is in it", self)
        heading.setFont(scaled_font(self, 1.4, bold=True))
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.setWordWrap(True)
        body = QLabel(
            "Drop recordings here (folders ending in .mefd, or single files ending in "
            ".mefd.tar), or use the buttons above. To find every recording in a folder "
            "and its subfolders, use Scan a folder. Opening a recording never changes it, "
            "and its signal is not loaded.",
            self,
        )
        body.setObjectName("Muted")
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(em * 2, em * 2, em * 2, em * 2)
        layout.addStretch(1)
        layout.addWidget(self.icon)
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addStretch(1)

    def set_theme(self, theme: Theme) -> None:
        side = self.fontMetrics().height() * 3
        self.icon.setPixmap(icons.icon("explorer", theme.accent).pixmap(side, side))


class StatusView(QWidget):
    """For a recording without details: still reading, locked, or unreadable."""

    unlock_requested = Signal()
    check_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.heading = QLabel("", self)
        self.heading.setFont(scaled_font(self, 1.15, bold=True))
        self.heading.setWordWrap(True)
        self.body = QLabel("", self)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.unlock = QPushButton("Enter the password…", self)
        self.unlock.setProperty("primary", True)
        self.unlock.clicked.connect(self.unlock_requested)
        self.check = QPushButton("Check for problems", self)
        self.check.clicked.connect(self.check_requested)
        buttons = QHBoxLayout()
        buttons.addWidget(self.unlock)
        buttons.addWidget(self.check)
        buttons.addStretch(1)
        self.details_slot = QVBoxLayout()
        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addWidget(self.body)
        layout.addLayout(buttons)
        layout.addLayout(self.details_slot)
        layout.addStretch(1)
        self.technical: TechnicalDetails | None = None

    def show_entry(self, entry: RecordingEntry) -> None:
        if self.technical is not None:
            self.technical.setParent(None)
            self.technical.deleteLater()
            self.technical = None
        outcome = entry.outcome
        self.unlock.setVisible(False)
        self.check.setVisible(False)
        if outcome is None or entry.busy == "Reading…":
            self.heading.setText("Reading the recording's details…")
            self.body.setText("This happens in the background; the window stays usable.")
            return
        message: UserMessage | None = outcome.error
        if outcome.needs_password:
            self.heading.setText(
                "That password did not open this recording"
                if outcome.wrong_password
                else "This recording is password-protected"
            )
            self.body.setText(
                "Enter its password to see what is in it. With the basic password, "
                "everything except the patient details is shown. Passwords don't hide "
                "the signal, the recording's name or its timing. The password is kept "
                "only while the app is open."
            )
            self.unlock.setVisible(True)
        elif message is not None:
            self.heading.setText(message.title)
            self.body.setText(message.text)
            self.check.setVisible(True)
            self.check.setEnabled(not entry.busy)
        if message is not None and message.detail:
            self.technical = TechnicalDetails(message.detail, self)
            self.details_slot.addWidget(self.technical)


class ExplorerPage(QWidget):
    """The Explore module."""

    #: Emitted with a short plain message for the status bar.
    status = Signal(str)

    def __init__(
        self,
        jobs: JobManager,
        source: RecordingSource | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.jobs = jobs
        self.source = source or LocalRecordingSource()
        self.zone: tzinfo = UTC
        self.theme: Theme = LIGHT
        self._dialogs: list = []
        #: The progress of each running check, by recording.
        self._check_progress: dict[str, JobProgressWidget] = {}
        self.setAcceptDrops(True)
        em = self.fontMetrics().height()

        # -- toolbar ---------------------------------------------------------
        self.open_button = QToolButton(self)
        self.open_button.setText("Open a recording")
        self.open_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.open_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        open_menu = QMenu(self.open_button)
        self.open_folder_action = open_menu.addAction("A recording folder (.mefd)…")
        self.open_folder_action.triggered.connect(self.choose_folder_recording)
        self.open_file_action = open_menu.addAction("A single-file recording (.mefd.tar)…")
        self.open_file_action.triggered.connect(self.choose_file_recording)
        self.open_button.setMenu(open_menu)
        self.scan_button = QPushButton("Scan a folder…", self)
        self.scan_button.setToolTip("Find every recording in a folder and its subfolders")
        self.scan_button.clicked.connect(self.choose_scan_folder)
        self.zone_caption = QLabel("", self)
        self.zone_caption.setObjectName("Muted")
        self.zone_caption.setMinimumWidth(1)
        self.zone_button = QToolButton(self)
        self.zone_button.setText("Change…")
        self.zone_button.setToolTip("Choose the time zone the times are shown in")
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.open_button)
        toolbar.addWidget(self.scan_button)
        toolbar.addStretch(1)
        zone_row = QHBoxLayout()
        zone_row.addWidget(self.zone_caption, 1)
        zone_row.addWidget(self.zone_button)
        toolbar.addLayout(zone_row)

        # -- the list ------------------------------------------------------------
        self.model = RecordingTableModel(self)
        self.proxy = RecordingSortProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView(self)
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(COL_START, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(em + 12)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.selectionModel().currentRowChanged.connect(self._on_current_changed)
        self.table.selectionModel().selectionChanged.connect(self._update_actions)

        self.copy_button = QToolButton(self)
        self.copy_button.setText("Copy summary")
        self.copy_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        copy_menu = QMenu(self.copy_button)
        self.copy_text_action = copy_menu.addAction("As text")
        self.copy_text_action.triggered.connect(lambda: self.copy_summary("text"))
        self.copy_json_action = copy_menu.addAction("As JSON (for scripts and logs)")
        self.copy_json_action.triggered.connect(lambda: self.copy_summary("json"))
        self.copy_button.setMenu(copy_menu)
        self.pack_button = QPushButton("Pack into one file…", self)
        self.pack_button.clicked.connect(self.pack_current)
        self.check_selected_button = QPushButton("Check for problems", self)
        self.check_selected_button.clicked.connect(self.check_selected)
        self.remove_button = QPushButton("Remove from the list", self)
        self.remove_button.setToolTip("Only removes it from this list; nothing is deleted")
        self.remove_button.clicked.connect(self.remove_selected)
        self.count_label = QLabel("", self)
        self.count_label.setObjectName("Muted")
        actions = FlowLayout()
        actions.addWidget(self.count_label)
        actions.addWidget(self.check_selected_button)
        actions.addWidget(self.copy_button)
        actions.addWidget(self.pack_button)
        actions.addWidget(self.remove_button)

        list_box = QWidget(self)
        list_layout = QVBoxLayout(list_box)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.addWidget(self.table, 1)
        list_layout.addLayout(actions)

        # -- the detail panel -----------------------------------------------------
        self.detail_heading = QLabel("", self)
        self.detail_heading.setFont(scaled_font(self, 1.25, bold=True))
        self.detail_heading.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_view = StatusView(self)
        self.status_view.unlock_requested.connect(lambda: self.unlock(self.current_entry()))
        self.status_view.check_requested.connect(lambda: self.check(self.current_entry()))
        self.overview = OverviewPanel(self)
        self.channels = ChannelsPanel(self)
        self.timeline = TimelinePanel(self)
        self.notes = NotesPanel(self)
        self.patient = PatientPanel(self)
        self.patient.reveal_changed.connect(self._on_reveal_changed)
        self.history = HistoryPanel(self)
        self.check_panel = CheckPanel(self)
        self.check_panel.check_requested.connect(lambda: self.check(self.current_entry()))
        self.tabs = QTabWidget(self)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self.panels = (
            self.overview,
            self.channels,
            self.timeline,
            self.notes,
            self.patient,
            self.history,
            self.check_panel,
        )
        for title, panel in zip(TABS, self.panels, strict=True):
            self.tabs.addTab(panel, title)
        self.nothing_selected = QLabel("Select a recording in the list to see its details.")
        self.nothing_selected.setObjectName("Muted")
        self.nothing_selected.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_stack = QStackedWidget(self)
        self.detail_stack.addWidget(self.nothing_selected)
        self.detail_stack.addWidget(self.status_view)
        self.detail_stack.addWidget(self.tabs)
        detail_box = QWidget(self)
        detail_layout = QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(0, int(em * 0.4), 0, 0)
        detail_layout.addWidget(self.detail_heading)
        detail_layout.addWidget(self.detail_stack, 1)

        self.splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.splitter.setObjectName("ExplorerSplitter")
        self.splitter.addWidget(list_box)
        self.splitter.addWidget(detail_box)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)

        self.drop_zone = DropZone(self)
        self.body = QStackedWidget(self)
        self.body.addWidget(self.drop_zone)
        self.body.addWidget(self.splitter)
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(int(em), int(em * 0.8), int(em), int(em * 0.6))
        layout.addLayout(toolbar)
        layout.addWidget(self.body, 1)

        self._remove_action = QAction("Remove from the list", self)
        self._remove_action.setShortcut(Qt.Key.Key_Delete)
        self._remove_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._remove_action.triggered.connect(self.remove_selected)
        self.table.addAction(self._remove_action)

        self.set_zone(UTC)
        self._refresh_body()
        self._update_actions()

    # ------------------------------------------------------------------
    # Appearance and time zone
    # ------------------------------------------------------------------
    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.drop_zone.set_theme(theme)
        self.timeline.set_theme(theme)

    def set_zone(self, zone: tzinfo) -> None:
        self.zone = zone
        label = zone_label(zone)
        self.zone_caption.setText(
            "Times are shown in UTC" if label == "UTC" else f"Times are shown in {label} time"
        )
        self.model.set_zone(zone)
        self._fit_columns()
        self._show_current()

    def set_zone_name(self, name: str) -> None:
        self.set_zone(resolve_zone(name))

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------
    def entries(self) -> list[RecordingEntry]:
        return list(self.model.entries)

    def current_entry(self) -> RecordingEntry | None:
        index = self.table.selectionModel().currentIndex()
        if not index.isValid():
            return None
        return self.proxy.data(index, EntryRole)

    def selected_entries(self) -> list[RecordingEntry]:
        rows = self.table.selectionModel().selectedRows()
        entries = [self.proxy.data(r, EntryRole) for r in rows]
        current = self.current_entry()
        if not entries and current is not None:
            entries = [current]
        return entries

    def select_entry(self, entry: RecordingEntry) -> None:
        row = self.model.row_of(entry)
        if row < 0:
            return
        index = self.proxy.mapFromSource(self.model.index(row, 0))
        self.table.selectionModel().setCurrentIndex(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        self.table.scrollTo(index)

    def _refresh_body(self) -> None:
        self.body.setCurrentWidget(self.splitter if self.model.entries else self.drop_zone)
        n = len(self.model.entries)
        self.count_label.setText(_plural(n, "recording") if n else "")

    def _update_actions(self, *_args) -> None:
        selected = self.selected_entries()
        current = self.current_entry()
        self.remove_button.setEnabled(bool(selected))
        self.copy_button.setEnabled(any(e.outcome is not None for e in selected))
        self.check_selected_button.setEnabled(
            any(e.outcome is not None and not e.busy and not e.needs_password for e in selected)
        )
        can_pack = current is not None and current.details is not None and not current.busy
        self.pack_button.setEnabled(can_pack)
        if current is not None and current.outcome is not None and current.outcome.packed:
            self.pack_button.setText("Unpack into a folder…")
        else:
            self.pack_button.setText("Pack into one file…")

    def _on_current_changed(self, *_args) -> None:
        self._show_current()
        self._update_actions()

    def _show_current(self) -> None:
        entry = self.current_entry()
        if entry is None:
            self.detail_heading.setText("")
            self.detail_stack.setCurrentWidget(self.nothing_selected)
            return
        self.detail_heading.setText(entry.name)
        if entry.details is None:
            self.status_view.show_entry(entry)
            self.detail_stack.setCurrentWidget(self.status_view)
            return
        for panel in self.panels:
            panel.show_entry(entry, self.zone)
        self.check_panel.set_progress(self._check_progress.get(entry.key))
        self.detail_stack.setCurrentWidget(self.tabs)

    def _entry_changed(self, entry: RecordingEntry) -> None:
        self.model.refresh(entry)
        if entry is self.current_entry():
            self._show_current()
        self._update_actions()

    # ------------------------------------------------------------------
    # Opening
    # ------------------------------------------------------------------
    def choose_folder_recording(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Open a recording folder (.mefd)")
        if folder:
            self.open_paths([folder])

    def choose_file_recording(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Open a single-file recording",
            "",
            "Single-file recordings (*.mefd.tar);;All files (*)",
        )
        if paths:
            self.open_paths(paths)

    def choose_scan_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Scan a folder for recordings")
        if folder:
            self.scan_folder(folder)

    def open_paths(self, paths: list[str], interactive: bool = True):
        """Open recordings. A folder that is not itself a recording is
        scanned instead. Returns the task's handle (or None)."""
        recordings, folders = [], []
        for path in paths:
            path = os.fspath(path)
            if is_recording_location(path):
                recordings.append(path)
            elif os.path.isdir(path):
                folders.append(path)
            else:
                self._show_message(
                    UserMessage(
                        "This is not a recording",
                        "Recordings are folders ending in .mefd, or single files ending in "
                        ".mefd.tar. Nothing was opened. Choose a recording, or use Scan a "
                        "folder to find the recordings inside a folder.",
                        "",
                    )
                )
        for folder in folders:
            self.scan_folder(folder)
        if not recordings:
            return None
        new = []
        for path in recordings:
            existing = self.model.entry_for(location_key(path))
            if existing is not None:
                if len(recordings) == 1:
                    self.select_entry(existing)
                continue
            entry = RecordingEntry(location=os.path.abspath(path), busy="Reading…")
            register_identity(entry.name)
            new.append(entry)
        if not new:
            return None
        self.model.add(new)
        self._refresh_body()
        if self.current_entry() is None or len(new) == 1:
            self.select_entry(new[0])
        return self._submit_open(new, prompt=interactive and len(new) == 1)

    def _submit_open(self, entries: list[RecordingEntry], prompt: bool):
        requests = [(e.location, e.password) for e in entries]
        title = "Opening a recording" if len(entries) == 1 else f"Opening {len(entries)} recordings"
        handle = self.jobs.submit(title, open_recordings, self.source, requests)
        handle.published.connect(lambda items: self._on_outcomes(items, prompt, create=False))
        keys = [e.key for e in entries]
        handle.finished.connect(lambda: self._open_finished(keys))
        handle.failed.connect(self._show_message)
        return handle

    def scan_folder(self, folder: str):
        known = frozenset(e.key for e in self.model.entries)
        handle = self.jobs.submit(
            "Looking for recordings in a folder", scan_and_open, self.source, folder, known
        )
        handle.published.connect(lambda items: self._on_outcomes(items, False, create=True))
        handle.succeeded.connect(self._scan_done)
        handle.failed.connect(self._show_message)
        self.status.emit("Looking for recordings…")
        return handle

    def _scan_done(self, result) -> None:
        found = len(result.locations)
        text = f"Found {_plural(found, 'recording')}"
        if result.folders_skipped:
            text += f"; {_plural(result.folders_skipped, 'folder')} could not be read"
        self.status.emit(text + ".")

    def _open_finished(self, keys: list[str]) -> None:
        # Recordings the task never answered for (it was stopped or failed).
        for key in keys:
            entry = self.model.entry_for(key)
            if entry is not None and entry.busy == "Reading…":
                entry.busy = ""
                if entry.outcome is None:
                    self.model.remove([entry])
                else:
                    self._entry_changed(entry)
        self._refresh_body()
        self._show_current()

    def _fit_columns(self) -> None:
        if len(self.model.entries) <= 500:  # beyond that, keep it cheap
            self.table.resizeColumnsToContents()

    def _on_outcomes(self, outcomes: list[InspectOutcome], prompt: bool, create: bool) -> None:
        added: list[RecordingEntry] = []
        for outcome in outcomes:
            if outcome.details is not None:
                register_identity(*outcome.details.identity_values())
            entry = self.model.entry_for(location_key(outcome.location))
            if entry is None:
                if not create:
                    continue  # removed from the list while it was being read
                entry = RecordingEntry(location=os.path.abspath(outcome.location))
                register_identity(entry.name)
                entry.outcome = outcome
                added.append(entry)
                continue
            entry.outcome = outcome
            entry.busy = ""
            self._entry_changed(entry)
        if added:
            self.model.add(added)
            self._refresh_body()
            if self.current_entry() is None:
                self.select_entry(added[0])
        self._fit_columns()
        for outcome in outcomes:
            entry = self.model.entry_for(location_key(outcome.location))
            if prompt and entry is not None and outcome.needs_password:
                self.unlock(entry)

    # ------------------------------------------------------------------
    # Passwords
    # ------------------------------------------------------------------
    def unlock(self, entry: RecordingEntry | None) -> None:
        if entry is None:
            return
        wrong = entry.outcome is not None and entry.outcome.wrong_password
        dialog = PasswordDialog(entry.name, wrong=wrong, parent=self)
        dialog.accepted.connect(lambda: self._unlock_with(entry, dialog.password()))
        dialog.finished.connect(lambda _r: self._forget_dialog(dialog))
        self._dialogs.append(dialog)
        dialog.open()

    def _forget_dialog(self, dialog) -> None:
        if dialog in self._dialogs:
            self._dialogs.remove(dialog)
        dialog.deleteLater()

    def _unlock_with(self, entry: RecordingEntry, password: str) -> None:
        if not password or self.model.row_of(entry) < 0:
            return
        register_identity(password)
        entry.password = password
        entry.busy = "Reading…"  # the old outcome stays until the new one arrives
        self._entry_changed(entry)
        self._submit_open([entry], prompt=True)

    # ------------------------------------------------------------------
    # Patient details
    # ------------------------------------------------------------------
    def _on_reveal_changed(self, shown: bool) -> None:
        entry = self.current_entry()
        if entry is None:
            return
        entry.revealed = shown
        self._show_current()

    # ------------------------------------------------------------------
    # Check for problems
    # ------------------------------------------------------------------
    def check(self, entry: RecordingEntry | None):
        if entry is None or entry.busy or entry.needs_password:
            return None
        entry.busy = "Checking…"
        entry.check = None
        entry.check_error = None
        self._entry_changed(entry)
        handle = self.jobs.submit(
            "Checking a recording for problems",
            check_recording,
            self.source,
            entry.location,
            entry.password,
        )
        progress = JobProgressWidget(handle, self)
        progress.hide()
        self._check_progress[entry.key] = progress
        if entry is self.current_entry():
            self._show_current()

        def done(report):
            entry.check = report

        def failed(message):
            entry.check_error = message

        def finished():
            entry.busy = ""
            widget = self._check_progress.pop(entry.key, None)
            if self.check_panel.progress_widget is widget:
                self.check_panel.set_progress(None)
            if widget is not None:
                widget.deleteLater()
            if self.model.row_of(entry) >= 0:
                self._entry_changed(entry)

        handle.succeeded.connect(done)
        handle.failed.connect(failed)
        handle.finished.connect(finished)
        return handle

    def check_selected(self) -> None:
        for entry in self.selected_entries():
            if entry.outcome is not None:
                self.check(entry)

    # ------------------------------------------------------------------
    # Copy summary
    # ------------------------------------------------------------------
    def summary_text(self, entries: list[RecordingEntry], fmt: str) -> str:
        summaries = [
            build_summary(
                e.outcome,
                self.zone,
                show=e.revealed,
                check=e.check,
                app_version=app_version(),
                library_version=mef3io_version(),
            )
            for e in entries
            if e.outcome is not None
        ]
        return as_json(summaries) if fmt == "json" else as_text(summaries)

    def confirm_copy_with_details(self, count: int) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("The summary will include patient details")
        box.setText(
            "Patient details are shown for "
            + ("this recording" if count == 1 else f"{count} of these recordings")
            + ", so the summary will include them, with the real dates. Copy it anyway?"
        )
        box.setInformativeText(
            "Paste it only where patient details are allowed. To copy without them, "
            "choose Hide under Patient details first."
        )
        copy = box.addButton("Copy with patient details", QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton("Don't copy", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        return box.clickedButton() is copy

    def copy_summary(self, fmt: str = "text") -> str | None:
        entries = [e for e in self.selected_entries() if e.outcome is not None]
        if not entries:
            return None
        revealed = sum(1 for e in entries if e.revealed)
        if revealed and not self.confirm_copy_with_details(revealed):
            return None
        text = self.summary_text(entries, fmt)
        QGuiApplication.clipboard().setText(text)
        self.status.emit(
            "Copied the summary"
            + (" with patient details." if revealed else ". Names and dates are hidden.")
        )
        return text

    # ------------------------------------------------------------------
    # Pack / unpack
    # ------------------------------------------------------------------
    def pack_current(self) -> None:
        entry = self.current_entry()
        if entry is None or entry.details is None:
            return
        packing = not entry.outcome.packed
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose where to save the single-file copy"
            if packing
            else "Choose where to save the unpacked folder",
        )
        if folder:
            self.pack(entry, folder)

    def pack(self, entry: RecordingEntry, destination_folder: str):
        packing = not entry.outcome.packed
        if packing:
            title = "Packing a recording into one file"
        else:
            title = "Unpacking a recording into a folder"
        handle = self.jobs.submit(
            title,
            pack_or_unpack,
            entry.location,
            destination_folder,
            entry.outcome.size_bytes,
        )
        record = handle.record

        def done(new_location: str) -> None:
            record.result_text = "Done: the new copy was checked against the original"
            self.status.emit("Packed into one file." if packing else "Unpacked into a folder.")
            self.open_paths([new_location], interactive=False)

        handle.succeeded.connect(done)
        handle.failed.connect(self._show_message)
        return handle

    # ------------------------------------------------------------------
    # Removing
    # ------------------------------------------------------------------
    def remove_selected(self) -> None:
        doomed = self.selected_entries()
        if not doomed:
            return
        self.model.remove(doomed)
        self._refresh_body()
        self._show_current()
        self._update_actions()

    # ------------------------------------------------------------------
    # Menus, messages, drag and drop
    # ------------------------------------------------------------------
    def _context_menu(self, pos) -> None:
        if not self.table.indexAt(pos).isValid():
            return
        menu = QMenu(self)
        menu.addAction("Check for problems", self.check_selected)
        menu.addAction("Copy summary as text", lambda: self.copy_summary("text"))
        menu.addAction("Copy summary as JSON", lambda: self.copy_summary("json"))
        menu.addSeparator()
        menu.addAction("Remove from the list", self.remove_selected)
        menu.popup(self.table.viewport().mapToGlobal(pos))
        self._dialogs.append(menu)
        menu.aboutToHide.connect(lambda: self._forget_menu(menu))

    def _forget_menu(self, menu) -> None:
        if menu in self._dialogs:
            self._dialogs.remove(menu)

    def _show_message(self, message: UserMessage) -> None:
        dialog = MessageDialog(message, self)
        dialog.finished.connect(lambda _r: self._forget_dialog(dialog))
        self._dialogs.append(dialog)
        dialog.open()

    @staticmethod
    def _local_paths(mime) -> list[str]:
        if not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._local_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = self._local_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.open_paths(paths)


__all__ = ["ExplorerPage"]
