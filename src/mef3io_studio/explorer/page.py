"""The Explore page: open recordings, list them, and show what is in them.

Nothing here touches a recording's files. Every read goes through a
background task (``workers.tasks``) and the recording source, which runs
mef3io in a helper process, so the window never waits for the disk or for
the library. Each task shows its progress and can be stopped.

Read-only, except "Pack into one file" / "Unpack into a folder", which
always write a NEW recording next to (or away from) the original and never
replace anything, and "Save times as a table", which writes only the file
the user chose.

Below the list, the user switches between the **details** of one recording
and the **combined timeline** of several (``combined.py``): the selected
recordings when two or more are selected, otherwise every recording in the
list. Its layout is worked out in short slices between other events
(``workers.slices``), so the window never waits for it.

Privacy: patient details are hidden until "Show" for that recording; every
identifying string the page learns (recording names, patient details,
passwords) is registered with the log filter as soon as it arrives; task
titles name no recording.
"""

from __future__ import annotations

import os
from datetime import UTC, tzinfo

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
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
from ..common.theme import LIGHT, Theme
from ..model.coverage import (
    STATUS_LOCKED,
    STATUS_READING,
    STATUS_READY,
    STATUS_UNREADABLE,
    STATUS_WRONG_PASSWORD,
    RecordingInput,
    combine_steps,
)
from ..model.errors import UserMessage
from ..model.export import LEVEL_PARTS, LEVEL_RECORDINGS, ExportItem
from ..model.recording import InspectOutcome, is_recording_location, location_key
from ..model.source import LocalRecordingSource, RecordingSource
from ..model.summary import as_json, as_text, build_summary
from ..model.timefmt import format_count, resolve_zone, zone_label
from ..workers.jobs import JobManager
from ..workers.slices import SlicedRunner
from ..workers.tasks import (
    check_recording,
    export_times,
    open_recordings,
    pack_or_unpack,
    scan_and_open,
)
from .combined import CombinedTimelinePanel
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
from .privacy import PrivacyPanel
from .state import RecordingEntry
from .table import COL_START, EntryRole, RecordingSortProxy, RecordingTableModel

TABS = (
    "Overview",
    "Channels",
    "Timeline",
    "Notes and events",
    "Patient details",
    "Privacy check",
    "History",
    "Check for problems",
)


#: Wait this long after the last change before laying out the combined
#: timeline again (a scan delivers recordings in quick bursts).
LAYOUT_DELAY_MS = 120

EXPORT_WARNING = (
    "This file will contain the dates of the recordings, which can identify people. "
    "Store it like the recordings themselves."
)


def _plural(n: int, one: str) -> str:
    return format_count(n, one)


def coverage_status(entry: RecordingEntry) -> str:
    """Whether a recording has times to draw, and if not, why."""
    outcome = entry.outcome
    if outcome is None or entry.busy == "Reading…":
        return STATUS_READING
    if outcome.needs_password:
        return STATUS_WRONG_PASSWORD if outcome.wrong_password else STATUS_LOCKED
    if outcome.details is None:
        return STATUS_UNREADABLE
    return STATUS_READY


def file_name(entry: RecordingEntry) -> str:
    """The recording's folder or file name, with its .mefd / .mefd.tar."""
    return os.path.basename(os.path.normpath(entry.location))


def timeline_labels(entries: list[RecordingEntry]) -> dict[str, str]:
    """What to call each recording on the timeline: its name, as in the
    list, unless another one has the same name (a folder and its
    single-file copy): then its file name, and if that is still the same,
    the name of the folder it is in. (Two recordings whose folders also
    share a name still read the same; the tooltip and the list's full
    location tell them apart.)"""
    by_name: dict[str, list[RecordingEntry]] = {}
    for e in entries:
        by_name.setdefault(e.name.casefold(), []).append(e)
    labels: dict[str, str] = {}
    for group in by_name.values():
        if len(group) == 1:
            continue
        files: dict[str, list[RecordingEntry]] = {}
        for e in group:
            files.setdefault(file_name(e).casefold(), []).append(e)
        for same in files.values():
            for e in same:
                if len(same) == 1:
                    labels[e.key] = file_name(e)
                else:
                    folder = os.path.basename(os.path.dirname(os.path.normpath(e.location)))
                    labels[e.key] = f"{file_name(e)} in {folder}"
    return labels


def coverage_input(entry: RecordingEntry, label: str = "") -> RecordingInput:
    status = coverage_status(entry)
    details = entry.details if status == STATUS_READY else None
    return RecordingInput(entry.key, entry.name, details, status, label)


class _FitInner(QWidget):
    """The page's content inside its scroll area. Its toolbar wraps (height
    depends on width), and for such a widget QScrollArea takes the
    PREFERRED height as the minimum, so the page would always scroll. This
    reports the minimum instead: the page fills the window and scrolls only
    when the window is shorter than that."""

    def __init__(self, toolbar, body: QWidget, margins: tuple[int, int, int, int]):
        super().__init__()
        self._toolbar, self._body = toolbar, body
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*margins)
        layout.addLayout(toolbar)
        layout.addWidget(body, 1)

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        layout = self.layout()
        margins = layout.contentsMargins()
        inner_width = width - margins.left() - margins.right()
        return (
            margins.top()
            + margins.bottom()
            + self._toolbar.heightForWidth(inner_width)
            + layout.spacing()
            + self._body.minimumSizeHint().height()
        )


class _ListBox(QWidget):
    """The recordings list and its row of buttons. The buttons wrap onto
    more lines when narrow, so the minimum height depends on the width:
    reported here, so the splitter never squeezes the wrapped row away."""

    def __init__(self, table: QTableView, actions, parent=None):
        super().__init__(parent)
        self._table, self._actions = table, actions
        self._wrapped = -1
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(table, 1)
        layout.addLayout(actions)
        # At least a few rows of the list, so a short window scrolls the
        # page rather than showing a header with no rows.
        rows = table.verticalHeader().defaultSectionSize()
        table.setMinimumHeight(
            table.horizontalHeader().sizeHint().height()
            + rows * 3
            + table.horizontalScrollBar().sizeHint().height()  # usually showing
            + 2 * table.frameWidth()
        )

    def _actions_height(self) -> int:
        width = self.width() if self.width() > 0 else self.sizeHint().width()
        return self._actions.heightForWidth(width)

    def minimumSizeHint(self):  # noqa: N802
        hint = super().minimumSizeHint()
        hint.setHeight(
            self._table.minimumHeight() + self.layout().spacing() + self._actions_height()
        )
        return hint

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        wrapped = self._actions_height()
        if wrapped != self._wrapped:
            self._wrapped = wrapped
            self.updateGeometry()  # the splitter and the page re-read the minimum


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
        buttons = FlowLayout()
        buttons.addWidget(self.unlock)
        buttons.addWidget(self.check)
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
    #: "Join or trim these…": the keys of the selected recordings.
    join_requested = Signal(list)
    #: "View signals": the selected RecordingEntry objects (their paths,
    #: passwords and privacy state stay in memory, in the entries).
    view_requested = Signal(list)

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
        # A row that wraps rather than make the window wider (laptops,
        # large text): the buttons first, then where times are shown.
        toolbar = FlowLayout()
        toolbar.addWidget(self.open_button)
        toolbar.addWidget(self.scan_button)
        toolbar.addWidget(self.zone_caption)
        toolbar.addWidget(self.zone_button)

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
        header.setResizeContentsPrecision(40)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.selectionModel().currentRowChanged.connect(self._on_current_changed)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)

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
        self.view_button = QPushButton("View signals", self)
        self.view_button.setToolTip("Look at the signals of the selected recordings")
        self.view_button.clicked.connect(self.view_selected)
        self.join_button = QPushButton("Join or trim these…", self)
        self.join_button.setToolTip(
            "Keep part of the selected recordings, or join them into one new recording"
        )
        self.join_button.clicked.connect(self.join_selected)
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
        actions.addWidget(self.view_button)
        actions.addWidget(self.join_button)
        actions.addWidget(self.remove_button)

        list_box = _ListBox(self.table, actions, self)
        self.list_box = list_box

        # -- the detail panel -----------------------------------------------------
        self.detail_heading = QLabel("", self)
        self.detail_heading.setFont(scaled_font(self, 1.25, bold=True))
        self.detail_heading.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # Never wider than the room it gets: a long name is cut, not the window widened.
        self.detail_heading.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.details_mode_button = QToolButton(self)
        self.details_mode_button.setText("Details")
        self.details_mode_button.setToolTip("The details of the recording selected in the list")
        self.combined_mode_button = QToolButton(self)
        self.combined_mode_button.setText("Combined timeline")
        self.combined_mode_button.setToolTip(
            "The selected recordings on one timeline (all of them when fewer than two are selected)"
        )
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for button in (self.details_mode_button, self.combined_mode_button):
            button.setCheckable(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            self.mode_group.addButton(button)
        self.details_mode_button.setChecked(True)
        self.combined_mode_button.toggled.connect(self._on_mode_toggled)
        self.status_view = StatusView(self)
        self.status_view.unlock_requested.connect(lambda: self.unlock(self.current_entry()))
        self.status_view.check_requested.connect(lambda: self.check(self.current_entry()))
        self.overview = OverviewPanel(self)
        self.channels = ChannelsPanel(self)
        self.timeline = TimelinePanel(self)
        self.notes = NotesPanel(self)
        self.patient = PatientPanel(self)
        self.patient.reveal_changed.connect(self._on_reveal_changed)
        self.privacy = PrivacyPanel(self)
        self.privacy.reveal_changed.connect(self._on_reveal_changed)
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
            self.privacy,
            self.history,
            self.check_panel,
        )
        for title, panel in zip(TABS, self.panels, strict=True):
            self.tabs.addTab(panel, title)
        self.nothing_selected = QLabel("Select a recording in the list to see its details.")
        self.nothing_selected.setObjectName("Muted")
        self.nothing_selected.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.combined = CombinedTimelinePanel(self)
        self.combined.recording_clicked.connect(self._on_combined_clicked)
        self.combined.recording_activated.connect(self._on_combined_activated)
        self.combined.export_requested.connect(lambda level: self.export_times(level))
        self.detail_stack = QStackedWidget(self)
        self.detail_stack.addWidget(self.nothing_selected)
        self.detail_stack.addWidget(self.status_view)
        self.detail_stack.addWidget(self.tabs)
        self.detail_stack.addWidget(self.combined)
        detail_box = QWidget(self)
        detail_layout = QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(0, int(em * 0.4), 0, 0)
        heading_row = QHBoxLayout()
        heading_row.setContentsMargins(0, 0, 0, 0)
        heading_row.addWidget(self.detail_heading, 1)
        heading_row.addWidget(self.details_mode_button)
        heading_row.addWidget(self.combined_mode_button)
        detail_layout.addLayout(heading_row)
        detail_layout.addWidget(self.detail_stack, 1)

        # The combined timeline's layout: worked out in short slices, a
        # moment after the last change, only while it is on screen.
        self._layout_runner = SlicedRunner(self)
        self._layout_runner.done.connect(self._on_layout_done)
        self._layout_runner.failed.connect(lambda: self.combined.set_computing(False))
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(LAYOUT_DELAY_MS)
        self._layout_timer.timeout.connect(self._start_layout)
        self._layout_dirty = True
        self._layout_keys: tuple[str, ...] | None = None
        self._coverage_cache: dict = {}

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

        # Everything sits in a scroll area: on a very narrow window (or very
        # large text) the page scrolls instead of forcing the window, and
        # the sidebar next to it, to change size. Platforms measure widgets
        # differently, so no single minimum width can be relied on.
        inner = _FitInner(toolbar, self.body, (int(em), int(em * 0.8), int(em), int(em * 0.6)))
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("Panel")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # keep focus on the list
        self.scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll)

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
        self.combined.set_theme(theme)

    def set_zone(self, zone: tzinfo) -> None:
        self.zone = zone
        label = zone_label(zone)
        caption = "Times are shown in UTC" if label == "UTC" else f"Times are shown in {label} time"
        self.zone_caption.setText(caption)
        self.zone_caption.setToolTip(caption)
        self.model.set_zone(zone)
        self.combined.set_zone(zone)
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
        self.join_button.setEnabled(bool(self._joinable(selected)))
        self.view_button.setEnabled(bool(self._viewable(selected)))
        can_pack = current is not None and current.details is not None and not current.busy
        self.pack_button.setEnabled(can_pack)
        if current is not None and current.outcome is not None and current.outcome.packed:
            self.pack_button.setText("Unpack into a folder…")
        else:
            self.pack_button.setText("Pack into one file…")

    def _on_current_changed(self, *_args) -> None:
        self._show_current()
        self._update_actions()

    def _on_selection_changed(self, *_args) -> None:
        self._update_actions()
        self._schedule_layout()

    def _show_current(self) -> None:
        entry = self.current_entry()
        if self.combined_mode_button.isChecked():
            self.combined.set_current(entry.key if entry is not None else None)
            self._show_combined_heading()
            self.detail_stack.setCurrentWidget(self.combined)
            return
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

    def refresh_entry(self, entry: RecordingEntry) -> None:
        """Show ``entry`` again after something outside changed it (the
        signal viewer's Show button set ``revealed``)."""
        if self.model.row_of(entry) >= 0:
            self._entry_changed(entry)

    def _entry_changed(self, entry: RecordingEntry) -> None:
        self.model.refresh(entry)
        if entry is self.current_entry():
            self._show_current()
        self._update_actions()
        self._schedule_layout()

    # ------------------------------------------------------------------
    # The combined timeline
    # ------------------------------------------------------------------
    def timeline_entries(self) -> list[RecordingEntry]:
        """The recordings on the combined timeline: the selected ones when
        two or more are selected, otherwise every recording in the list."""
        rows = self.table.selectionModel().selectedRows()
        if len(rows) >= 2:
            return [self.proxy.data(r, EntryRole) for r in rows]
        return list(self.model.entries)

    def _timeline_caption(self) -> str:
        if len(self.table.selectionModel().selectedRows()) >= 2:
            return "Showing the recordings selected in the list."
        return "Showing every recording in the list. Select two or more to show only those."

    def _show_combined_heading(self) -> None:
        n = len(self.timeline_entries())
        self.detail_heading.setText(f"{_plural(n, 'recording')} on one timeline")

    def show_combined(self, combined: bool = True) -> None:
        (self.combined_mode_button if combined else self.details_mode_button).setChecked(True)

    def _on_mode_toggled(self, combined: bool) -> None:
        if combined:
            self._schedule_layout(now=True)
        else:
            # Laid out again when shown; nothing pending may touch the
            # Details view meanwhile.
            self._layout_timer.stop()
            self._layout_runner.cancel()
            self.combined.set_computing(False)
            self._layout_dirty = True
        self._show_current()

    def _schedule_layout(self, now: bool = False) -> None:
        self._layout_dirty = True
        if not self.combined_mode_button.isChecked():
            return  # worked out when it is shown
        if now:
            self._layout_timer.stop()
            self._start_layout()
        else:
            self._layout_timer.start()

    def _start_layout(self) -> None:
        if not self._layout_dirty or not self.combined_mode_button.isChecked():
            return  # (still dirty when hidden: laid out when shown)
        self._layout_dirty = False
        entries = self.timeline_entries()
        labels = timeline_labels(entries)
        items = [coverage_input(e, labels.get(e.key, "")) for e in entries]
        keys = tuple(sorted(i.key for i in items))
        cache = self._coverage_cache
        self.combined.set_caption(self._timeline_caption())
        self._show_combined_heading()
        self.combined.set_computing(True)

        def work():
            layout, new_cache = yield from combine_steps(items, cache)
            return layout, new_cache, keys

        self._layout_runner.submit(work())

    def _on_layout_done(self, result) -> None:
        if result is None:
            return
        layout, cache, keys = result
        # Keep what was worked out for every recording still listed, not
        # only the ones shown: going back to all of them is then instant.
        listed = {e.key for e in self.model.entries}
        kept = {k: v for k, v in self._coverage_cache.items() if k in listed}
        kept.update(cache)
        self._coverage_cache = kept
        # Fit when the set of recordings differs from the one on screen
        # (not from the last one asked for: that one may have been dropped).
        refit = keys != self._layout_keys
        self._layout_keys = keys
        self.combined.set_layout(layout, refit=refit)
        entry = self.current_entry()
        self.combined.set_current(entry.key if entry is not None else None)

    def _on_combined_clicked(self, key: str) -> None:
        entry = self.model.entry_for(key)
        if entry is None:
            return
        if len(self.table.selectionModel().selectedRows()) >= 2:
            # Keep the selection (it is what the timeline shows); only move
            # the current recording, as Ctrl+arrow keys would.
            row = self.model.row_of(entry)
            index = self.proxy.mapFromSource(self.model.index(row, 0))
            self.table.selectionModel().setCurrentIndex(
                index, QItemSelectionModel.SelectionFlag.NoUpdate
            )
            self.table.scrollTo(index)
        else:
            self.select_entry(entry)

    def _on_combined_activated(self, key: str) -> None:
        self._on_combined_clicked(key)
        self.show_combined(False)

    # ------------------------------------------------------------------
    # Save times as a table
    # ------------------------------------------------------------------
    def confirm_export(self, count: int, revealed: int, level: str = LEVEL_RECORDINGS) -> bool:
        """The warning before a table with dates is written. ``revealed``:
        how many rows will carry patient details (what Show revealed)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("The table will contain dates")
        box.setText(EXPORT_WARNING)
        more = (
            f"It covers {_plural(count, 'recording')} and lists their names as the list shows them."
        )
        if level == LEVEL_PARTS:
            more += " This table has no patient details."
        elif revealed:
            more += (
                " You chose Show for the patient details of "
                + ("this recording" if count == 1 else f"{revealed} of them")
                + ", so those details are included too (names that need the full-access "
                "password read locked). The others read [hidden]."
            )
        else:
            more += " Patient details are not included: they read [hidden]."
        box.setInformativeText(more)
        save = box.addButton("Choose where to save…", QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton("Don't save", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        return box.clickedButton() is save

    def choose_export_path(self, level: str) -> str:
        """Ask where to save; the dialog asks before replacing a file."""
        dialog = QFileDialog(self, "Save the times as a table")
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setNameFilters(["Tables for spreadsheets (*.csv)"])
        dialog.setDefaultSuffix("csv")
        dialog.selectFile("recording-parts.csv" if level == LEVEL_PARTS else "recording-times.csv")
        if not dialog.exec():
            return ""
        files = dialog.selectedFiles()
        return files[0] if files else ""

    def export_times(
        self,
        level: str = LEVEL_RECORDINGS,
        entries: list[RecordingEntry] | None = None,
        path: str | None = None,
    ):
        """Save the times of ``entries`` (default: the combined timeline's
        recordings) as a CSV table. Returns the task's handle, or None."""
        entries = self.timeline_entries() if entries is None else entries
        if not entries:
            return None
        # What is saved is fixed NOW, before the dialogs: they let other
        # events run (a re-read may finish meanwhile), and the warning must
        # describe exactly the table that is written.
        labels = timeline_labels(entries)
        items = [
            ExportItem(coverage_input(e, labels.get(e.key, "")), file_name(e), e.revealed)
            for e in entries
        ]
        # Counted the way the table decides: whatever Show reveals is
        # written for a recording that is open, even when only the basic
        # password opened it (its description is then written; the names
        # read "locked"). The per-part table has no patient details.
        revealed = (
            0
            if level == LEVEL_PARTS
            else sum(1 for i in items if i.revealed and i.recording.details is not None)
        )
        if not self.confirm_export(len(items), revealed, level):
            return None
        if path is None:
            path = self.choose_export_path(level)
            if not path:
                return None
        handle = self.jobs.submit(
            "Saving the times of recordings as a table", export_times, items, self.zone, level, path
        )

        def done(rows: int) -> None:
            handle.record.result_text = f"Saved a table with {_plural(rows, 'row')}"
            self.status.emit(f"Saved the table ({_plural(rows, 'row')}).")

        handle.succeeded.connect(done)
        handle.failed.connect(self._show_message)
        return handle

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

    def open_paths(self, paths: list[str], interactive: bool = True, password: str = ""):
        """Open recordings. Anything that is not named like a recording is
        handed to a scan (in the background: even checking whether a path is
        a folder can hang on a disconnected drive), which looks inside a
        folder or says plainly that it is not a recording. Returns the task's
        handle (or None)."""
        recordings, folders = [], []
        for path in paths:
            path = os.fspath(path)
            if is_recording_location(path):
                recordings.append(path)
            else:
                folders.append(path)
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
            entry = RecordingEntry(
                location=os.path.abspath(path), busy="Reading…", password=password
            )
            new.append(entry)
        if not new:
            return None
        self.model.add(new)
        self._refresh_body()
        if self.current_entry() is None or len(new) == 1:
            self.select_entry(new[0])
        return self._submit_open(new, prompt=interactive and len(new) == 1)

    def open_for_privacy_check(self, path: str, password: str = ""):
        """Open a recording (normally a copy the anonymizer just made) and
        show its Privacy check, so the first sight of the copy is the proof.
        Returns the opening task's handle (None when it was already listed)."""
        handle = self.open_and_show(path, password)
        self.tabs.setCurrentWidget(self.privacy)
        return handle

    def open_and_show(self, path: str, password: str = ""):
        """Open a recording the app just made (a joined one) and select it.
        Returns the opening task's handle (None when it was already listed)."""
        self.show_combined(False)
        handle = self.open_paths([path], interactive=False, password=password)
        entry = self.model.entry_for(location_key(path))
        if entry is not None:
            self.select_entry(entry)
        return handle

    def _submit_open(self, entries: list[RecordingEntry], prompt: bool):
        requests = [(e.location, e.password) for e in entries]
        title = "Opening a recording" if len(entries) == 1 else f"Opening {len(entries)} recordings"
        handle = self.jobs.submit(title, open_recordings, self.source, requests)
        job = handle.record.id
        for entry in entries:
            # This task now owns the entry: an older task for the same
            # recording (removed and opened again) can no longer touch it.
            entry.open_job = job
        handle.published.connect(lambda items: self._on_outcomes(items, prompt, False, job))
        keys = [e.key for e in entries]
        handle.finished.connect(lambda: self._open_finished(keys, job))
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

    def _open_finished(self, keys: list[str], job: int) -> None:
        # Recordings the task never answered for (it was stopped or failed).
        changed = False
        for key in keys:
            entry = self.model.entry_for(key)
            if entry is not None and entry.open_job == job:
                changed = True
                entry.open_job = None
                entry.busy = ""
                if entry.outcome is None:
                    self.model.remove([entry])
                else:
                    self._entry_changed(entry)
        if changed:  # every answered row was already shown as it arrived
            self._refresh_body()
            self._show_current()

    def _fit_columns(self) -> None:
        # Measured from a sample of rows (setResizeContentsPrecision), so
        # this stays cheap however many recordings are listed.
        self.table.resizeColumnsToContents()

    def _on_outcomes(
        self,
        outcomes: list[InspectOutcome],
        prompt: bool,
        create: bool,
        job: int | None = None,
    ) -> None:
        """``create``: a scan's results (new rows; never overwrite a row
        that is already listed). Otherwise an open task's results, applied
        only to rows that task still owns."""
        # One re-sort for the whole batch, not one per recording (each would
        # re-sort the list on the GUI thread).
        self.proxy.setDynamicSortFilter(False)
        try:
            applied = self._apply_outcomes(outcomes, prompt, create, job)
        finally:
            self.proxy.setDynamicSortFilter(True)  # sorts once, now
        self._fit_columns()  # after the re-sort: widths come from a sample of rows
        for entry in applied:
            if prompt and entry.needs_password:
                self.unlock(entry)

    def _apply_outcomes(self, outcomes, prompt: bool, create: bool, job) -> list:
        added: list[RecordingEntry] = []
        applied: list[RecordingEntry] = []
        for outcome in outcomes:
            # (The task registered every identifying value with the log
            # filter before publishing it, on its own thread.)
            entry = self.model.entry_for(location_key(outcome.location))
            if entry is None:
                if not create:
                    continue  # removed from the list while it was being read
                entry = RecordingEntry(location=os.path.abspath(outcome.location))
                entry.outcome = outcome
                added.append(entry)
                continue
            if create or entry.open_job != job:
                continue  # listed meanwhile, or opened again by a newer task
            entry.open_job = None
            entry.outcome = outcome
            entry.busy = ""
            applied.append(entry)
        if applied or added:
            self._schedule_layout()
        if applied:
            self.model.refresh_all()
            current = self.current_entry()
            if current is not None and current in applied:
                self._show_current()
            self._update_actions()
        if added:
            self.model.add(added)
            self._refresh_body()
            if self.current_entry() is None:
                self.select_entry(added[0])
        return applied

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
        entry.password = password  # registered with the log filter by the task
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
            widget = progress
            if self._check_progress.get(entry.key) is progress:
                del self._check_progress[entry.key]
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
        listed = [e for e in entries if e.outcome is not None]
        summaries = []
        for i, e in enumerate(listed):
            summary = build_summary(
                e.outcome,
                self.zone,
                show=e.revealed,
                check=e.check,
                app_version=app_version(),
                library_version=mef3io_version(),
            )
            if len(listed) > 1:
                # Hidden names read the same; a neutral number tells them apart.
                summary = {"item": f"{i + 1} of {len(listed)}", **summary}
            summaries.append(summary)
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
            entry.password,
        )
        record = handle.record

        def done(new_location: str) -> None:
            record.result_text = "Done: the new copy was checked against the original"
            self.status.emit("Packed into one file." if packing else "Unpacked into a folder.")
            # A copy keeps its password protection: open it with the same one.
            self.open_paths([new_location], interactive=False, password=entry.password)

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
        self._schedule_layout()

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
        menu.addAction(
            "Save their times as a table (CSV)…",
            lambda: self.export_times(LEVEL_RECORDINGS, self.selected_entries()),
        )
        menu.addAction("Show them on one timeline", lambda: self.show_combined(True))
        view = menu.addAction("View signals", self.view_selected)
        view.setEnabled(bool(self._viewable(self.selected_entries())))
        join = menu.addAction("Join or trim these…", self.join_selected)
        join.setEnabled(bool(self._joinable(self.selected_entries())))
        menu.addSeparator()
        menu.addAction("Remove from the list", self.remove_selected)
        menu.popup(self.table.viewport().mapToGlobal(pos))
        self._dialogs.append(menu)
        menu.aboutToHide.connect(lambda: self._forget_menu(menu))

    @staticmethod
    def _joinable(entries: list[RecordingEntry]) -> list[str]:
        return [e.key for e in entries if e.outcome is not None and not e.busy]

    @staticmethod
    def _viewable(entries: list[RecordingEntry]) -> list[RecordingEntry]:
        """Recordings that can be looked at: read, not busy, and either not
        password-protected or opened with a password."""
        return [e for e in entries if e.outcome is not None and not e.busy and not e.needs_password]

    def view_selected(self) -> None:
        """ "View signals": the signal viewer, with exactly the selected recordings."""
        entries = self._viewable(self.selected_entries())
        if entries:
            self.view_requested.emit(entries)

    def join_selected(self) -> None:
        """Open Join or trim with exactly the selected recordings (the
        joiner orders them by time and says why any cannot be used)."""
        keys = self._joinable(self.selected_entries())
        if keys:
            self.join_requested.emit(keys)

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
