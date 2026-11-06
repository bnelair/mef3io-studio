"""The "Join or trim" page: one new recording from parts of others.

Steps: recordings (ticked from the Explore list, as on the Anonymize page),
what to keep of each (whole, the first or last so long, or between two
times; in time order, with a preview of the kept parts and the gaps
between them), channels (the anonymizer's picker), the new recording (name,
folder, optional password protection), then saving it. NOTHING is written
before "Check and save": that first runs mef3io's own checks (which write
nothing) in a helper process, and only then makes the recording, in a
helper process too, with one progress bar and Stop. The originals are never
changed.

The rules (what each choice keeps, overlaps, the one mef3io call that
makes the result from the originals) live in ``model.join``; the widgets never import
mef3io. Recordings come from the Explore list (opened with their
passwords there): no recording is opened twice.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, tzinfo

from PySide6.QtCore import QDate, QDateTime, QTime, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDateTimeEdit,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..anonymizer.page import (
    BLOCKER_PLAN_WORDS,
    Choice,
    DatesStep,
    DetailsAndTextBox,
    RecordingsStep,
    _clear,
    _item,
    _label,
    _scroll,
    _table,
    channel_selection_for,
)
from ..anonymizer.picker import ChannelPicker
from ..common.activity import JobProgressWidget
from ..common.dialogs import TechnicalDetails
from ..common.flow import FlowLayout
from ..common.pages import scaled_font
from ..common.passwords import PasswordsStep
from ..common.theme import LIGHT, Theme
from ..explorer.combined import ZOOM_STEP
from ..explorer.state import RecordingEntry
from ..model import join as join_model
from ..model.anonymize import (
    blocker_words,
    destination_for,
    inside_any_recording,
    name_problem,
)
from ..model.channels import ChannelSelection
from ..model.coverage import RecordingInput, combine_steps
from ..model.join import (
    KEEP_BETWEEN,
    KEEP_FIRST,
    KEEP_LAST,
    KEEP_WHOLE,
    RECORDS_DROP,
    RECORDS_KEEP,
    SECOND_US,
    Anonymous,
    JoinPlan,
    JoinRequest,
    JoinResult,
    JoinSource,
    Keep,
    Layout,
    plan_join,
    problem_text,
    window_keeps,
)
from ..model.recording import ACCESS_BASIC
from ..model.shiftplan import Blocker
from ..model.timefmt import (
    format_count,
    format_span,
    format_time,
    format_utc_us,
    from_local,
    parse_utc_text,
    parse_whole_number,
    resolve_zone,
    to_datetime,
)
from ..workers.jobs import JobManager
from ..workers.slices import SlicedRunner
from ..workers.tasks import check_join, join_recordings
from .trimmap import END, LABELS, START, TrimMapWidget

#: Wait this long after the last change of the Explore list before listing
#: the recordings again (a scan delivers them in quick bursts).
REFRESH_DELAY_MS = 150

# Channels come before "What to keep": two parts that overlap in time are
# fine when they share no kept channel, so the overlap rule needs the
# channel choice.
STEPS = ("Recordings", "Channels", "What to keep", "Privacy", "New recording", "Save")
STEP_RECORDINGS, STEP_CHANNELS, STEP_KEEP, STEP_PRIVACY, STEP_NEW, STEP_RUN = range(len(STEPS))

#: The "Keep" choices, in the order the list shows them.
KEEP_LABELS = (
    (KEEP_WHOLE, "The whole recording"),
    (KEEP_FIRST, "The first…"),
    (KEEP_LAST, "The last…"),
    (KEEP_BETWEEN, "From … to …"),
)

PASSWORD_EXPLANATION = (
    "A password-protected recording needs two passwords. The basic password opens the "
    "recording settings and descriptions; the full-access password also opens the patient "
    "details and the text of notes and events. Passwords do not hide the signal, the "
    "recording's name or its timing. There is no way to get a forgotten password back, and "
    "the app never stores passwords. The originals' passwords are not carried over."
)

PROTECTED_SOURCES = (
    "Some of the chosen recordings are password-protected. The new recording will NOT be, "
    "unless you protect it with two new passwords here."
)

HOW_IT_WORKS = (
    "Several recordings: the part of each that you keep is joined straight into the new "
    "recording in one go, which is then checked against the originals. Nothing is saved "
    "in between. The originals are never changed."
)

NOTES_INTRO = (
    "Notes and events can hold text that names the patient. You can keep them in the new "
    "recording or leave them all out."
)
NOTES_KEEP = "Keep them"
NOTES_LEAVE_OUT = "Leave them out"
NOTES_LEFT_OUT_NOTE = (
    "Every note and event is left out of the new recording, with its time. The signal is "
    "not affected, and the originals keep theirs."
)

HOW_IT_WORKS_ANONYMOUS = (
    "The part of each recording that you keep is made anonymous as it is read from the "
    "original and joined straight into the new recording, in one go: a copy with the "
    "patient's details is never written. The new recording is then checked. The originals "
    "are never changed."
)

PRIVACY_KEEP = "Keep the patient details (as in the originals)"
PRIVACY_ANONYMOUS = "Make it anonymous"
DATES_KEEP = "Keep the real dates"
DATES_MOVE = "Move every date by the same number of whole days (recommended)"
DATES_KEEP_WARNING = (
    "The real dates stay in the new recording: it still shows when the patient was recorded."
)

FILE_NOTE = (
    "The new recording is made in a hidden temporary folder next to the file and packed, the "
    "file is compared with what it was packed from, and only then it is given its name. The "
    "temporary folder is always removed at the end, also when you stop or something goes "
    "wrong. For a moment the chosen drive needs room for about twice the size of what you "
    "keep."
)

SAVE_FOLDER = "A folder (.mefd), as the originals"
SAVE_FILE = "One file (.mefd.tar), which this app opens directly"

WHY_NOT_USABLE = {
    "busy": "Still being read…",
    "unreadable": "It could not be read.",
    "times": (
        "Its times cannot be read without doubt (an older way of hiding dates is set in a "
        "way that cannot be undone safely)."
    ),
}


# --------------------------------------------------------------------------
# Step 1: recordings (the anonymizer's step, with the joiner's rules)


class JoinRecordingsStep(RecordingsStep):
    COLUMNS = ("Recording", "Channels", "Start", "Can it be used?")
    INTRO = (
        "Choose the recordings to join, from the list in Explore: one to keep only part of "
        "it, or several to join parts of them into one new recording. The originals are "
        "never changed."
    )
    ALL_LABEL = "Tick all that can be used"

    def _reason(self, entry: RecordingEntry) -> str | None:
        details = entry.details
        if entry.busy:
            return WHY_NOT_USABLE["busy"]
        if details is None:
            if entry.needs_password:
                return BLOCKER_PLAN_WORDS[Blocker.LOCKED]
            return WHY_NOT_USABLE["unreadable"]
        if details.access == ACCESS_BASIC:
            return BLOCKER_PLAN_WORDS[Blocker.LOCKED]
        if details.anonymize_blockers:
            return " ".join(dict.fromkeys(blocker_words(c) for c, _ in details.anonymize_blockers))
        if not details.times_verified:
            return WHY_NOT_USABLE["times"]
        if details.start is None or details.end is None or details.end <= details.start:
            return WHY_NOT_USABLE["unreadable"]
        return None


# --------------------------------------------------------------------------
# Step 2: what to keep


class DurationInput(QWidget):
    """Hours, minutes and seconds."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hours = QSpinBox()
        self.hours.setRange(0, 999_999)
        self.hours.setSuffix(" h")
        self.minutes = QSpinBox()
        self.minutes.setRange(0, 59)
        self.minutes.setSuffix(" min")
        self.seconds = QSpinBox()
        self.seconds.setRange(0, 59)
        self.seconds.setSuffix(" s")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for box in (self.hours, self.minutes, self.seconds):
            box.valueChanged.connect(self.changed)
            layout.addWidget(box)
        layout.addStretch(1)

    def microseconds(self) -> int:
        total = self.hours.value() * 3600 + self.minutes.value() * 60 + self.seconds.value()
        return total * 1_000_000

    def set_seconds(self, seconds: int) -> None:
        hours, rest = divmod(max(0, int(seconds)), 3600)
        self.hours.setValue(hours)
        self.minutes.setValue(rest // 60)
        self.seconds.setValue(rest % 60)


class TimeInput(QDateTimeEdit):
    """A date and time in the site time zone (shown without a zone; the
    step's heading names it)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.setCalendarPopup(True)

    def set_us(self, us: int, zone: tzinfo) -> None:
        moment = to_datetime(us, zone)
        self.setDateTime(
            QDateTime(
                QDate(moment.year, moment.month, moment.day),
                QTime(moment.hour, moment.minute, moment.second),
            )
        )

    def us(self, zone: tzinfo) -> int:
        d, t = self.date(), self.time()
        moment = datetime(d.year(), d.month(), d.day(), t.hour(), t.minute(), t.second())
        return from_local(moment, zone)


class KeepRow(QWidget):
    """The "Keep" choice of one recording. ``state``: an earlier row's
    :meth:`state` to start from (the table makes new rows when the list or
    the time zone changes: Qt deletes a table's cell widgets with its rows)."""

    changed = Signal()

    def __init__(self, entry: RecordingEntry, zone: tzinfo, state=None, parent=None):
        super().__init__(parent)
        self.entry = entry
        self._zone = zone
        self.how = QComboBox()
        for key, label in KEEP_LABELS:
            self.how.addItem(label, key)
        self.duration = DurationInput()
        self.start = TimeInput()
        self.end = TimeInput()
        between = QWidget()
        b = QHBoxLayout(between)
        b.setContentsMargins(0, 0, 0, 0)
        b.addWidget(self.start)
        b.addWidget(QLabel("to"))
        b.addWidget(self.end)
        b.addStretch(1)
        self.inputs = QStackedWidget()
        self.inputs.addWidget(QWidget())
        self.inputs.addWidget(self.duration)
        self.inputs.addWidget(between)
        self.inputs.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self.how)
        layout.addWidget(self.inputs, 1)
        details = entry.details
        how, seconds, start, end = state or (KEEP_WHOLE, 3600, details.start, details.end)
        self.set_how(how)
        self.duration.set_seconds(seconds)
        self.start.set_us(start, zone)
        self.end.set_us(end, zone)
        self.how.currentIndexChanged.connect(self._on_how)
        self.duration.changed.connect(self.changed)
        self.start.dateTimeChanged.connect(lambda *_: self.changed.emit())
        self.end.dateTimeChanged.connect(lambda *_: self.changed.emit())
        self._on_how()

    def state(self) -> tuple[str, int, int, int]:
        """(how, seconds, from, to): the choice, independent of the zone."""
        return (
            self.how.currentData(),
            self.duration.microseconds() // 1_000_000,
            self.start.us(self._zone),
            self.end.us(self._zone),
        )

    def set_how(self, how: str) -> None:
        self.how.setCurrentIndex([k for k, _ in KEEP_LABELS].index(how))

    def _on_how(self, *_args) -> None:
        how = self.how.currentData()
        self.inputs.setCurrentIndex(
            {KEEP_WHOLE: 0, KEEP_FIRST: 1, KEEP_LAST: 1, KEEP_BETWEEN: 2}[how]
        )
        self.changed.emit()

    def keep(self) -> Keep:
        how = self.how.currentData()
        if how in (KEEP_FIRST, KEEP_LAST):
            return Keep(how, duration_us=self.duration.microseconds())
        if how == KEEP_BETWEEN:
            # Whole seconds on screen: a "to" at the recording's last second
            # reaches its end.
            end = self.end.us(self._zone)
            if self.entry.details.end - end < 1_000_000:
                end = max(end, self.entry.details.end)
            return Keep(how, start_us=self.start.us(self._zone), end_us=end)
        return Keep(KEEP_WHOLE)


#: Words for the two ways to choose what to keep.
MODE_WINDOW = "window"
MODE_EACH = "each"
LEFT_OUT_TEXT = "Left out: no data between Start and End"
WAIT_FOR_MAP = "Wait a moment: the map is still being worked out."
MAP_FAILED = "The map could not be drawn. Choose for each recording instead."
NO_DATA_IN_WINDOW = (
    "No recording has data between Start and End. Move the lines so that they enclose some."
)
TIMES_ARE_REAL = (
    "These are the real times of the recordings, as they were recorded (UTC for the "
    "date and time fields and for the number)."
)
TIMES_ARE_REAL_MOVED = (
    " You chose to make the new recording anonymous with the dates moved: its times will "
    "differ from these by the same whole number of days."
)
DATE_HELP = "Write it like 2026-11-04 13:05:22.123456 (UTC). Seconds and the fraction are optional."
NUMBER_HELP = (
    "Write the time as a whole number: microseconds since 1 January 1970, UTC (for "
    "example 1604495122123456)."
)
NUMBER_TIP = (
    "The same moment as a whole number of microseconds since 1 January 1970, UTC. This is how "
    "the recording stores its times."
)


class KeepStep(QWidget):
    changed = Signal()

    COLUMNS = ("Recording", "Start", "End", "Keep", "Kept")

    def __init__(self, page: JoinerPage):
        super().__init__(page)
        self.page = page
        #: Every chosen recording's row, in time order (see :attr:`rows`).
        self.all_rows: list[KeepRow] = []
        self.plan = JoinPlan()
        self.mode = MODE_EACH
        #: The window ``[start, end)`` for every recording, microseconds.
        self.window: tuple[int, int] = (0, 1)
        self.bounds: tuple[int, int] = (0, 1)
        #: Keys of the recordings the window leaves out, and what each
        #: recording keeps in window mode.
        self.left_out: frozenset[str] = frozenset()
        self._window_keeps: dict[str, Keep] = {}
        self._keys: tuple[str, ...] = ()
        self._runs: dict = {}
        self._coverage_cache: dict = {}
        self._layout_keys: tuple[str, ...] | None = None
        self.zone_note = _label(muted=True)
        #: What the signal viewer asked for and what this page holds instead.
        self.viewer_note = _label(muted=True)
        self.viewer_note.setVisible(False)
        self.table = _table(self.COLUMNS)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.summary = _label(bold=True)
        self.problems = _label()
        self.problems.setObjectName("Bad")

        self.mode_window = QRadioButton("The same window for all recordings, set on the map")
        self.mode_each = QRadioButton("Choose for each recording")
        self.mode_each.setChecked(True)
        modes = QButtonGroup(self)
        for b in (self.mode_window, self.mode_each):
            modes.addButton(b)
            b.toggled.connect(self._mode_toggled)
        self.map = TrimMapWidget()
        self.map.setMinimumHeight(round(self.fontMetrics().height() * 11))
        self.map.window_moved.connect(self._map_moved)
        self.map_buttons = FlowLayout()
        self.fit_button = QToolButton()
        self.fit_button.setText("Show everything")
        self.fit_button.setToolTip(
            "Show the whole time span of the recordings. The Home key does the same, unless "
            "the Start or End line is picked: then it moves that line to the first moment."
        )
        self.zoom_in_button = QToolButton()
        self.zoom_in_button.setText("Zoom in")
        self.zoom_in_button.setToolTip("Or turn the mouse wheel over the map")
        self.zoom_out_button = QToolButton()
        self.zoom_out_button.setText("Zoom out")
        self.fit_button.clicked.connect(self.map.fit_all)
        self.zoom_in_button.clicked.connect(lambda: self.map.zoom(ZOOM_STEP * ZOOM_STEP))
        self.zoom_out_button.clicked.connect(lambda: self.map.zoom(1 / (ZOOM_STEP * ZOOM_STEP)))
        for b in (self.fit_button, self.zoom_in_button, self.zoom_out_button):
            self.map_buttons.addWidget(b)
        self.map_buttons.addWidget(
            _label(
                "Drag the Start and End lines (or click a flag, or press S or E, then use "
                "the arrow keys). Shaded parts are not kept.",
                muted=True,
            )
        )

        # The Start and End fields: a date and time, and the same as a number.
        self.date_edit = {START: QLineEdit(), END: QLineEdit()}
        self.number_edit = {START: QLineEdit(), END: QLineEdit()}
        fields = QGridLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.addWidget(_label("Date and time (UTC)", bold=True), 0, 1)
        fields.addWidget(
            _label("Time as a number (microseconds since 1 January 1970, UTC)", bold=True), 0, 2
        )
        for i, which in enumerate((START, END), start=1):
            fields.addWidget(_label(LABELS[which], bold=True), i, 0)
            fields.addWidget(self.date_edit[which], i, 1)
            fields.addWidget(self.number_edit[which], i, 2)
            self.date_edit[which].setPlaceholderText("2026-11-04 13:05:22.123456")
            self.date_edit[which].setAccessibleName(f"{LABELS[which]}: date and time in UTC")
            self.date_edit[which].setToolTip(DATE_HELP)
            self.number_edit[which].setPlaceholderText("1604495122123456")
            self.number_edit[which].setAccessibleName(
                f"{LABELS[which]}: time as a number, microseconds since 1 January 1970, UTC"
            )
            self.number_edit[which].setToolTip(NUMBER_TIP)
            self.date_edit[which].editingFinished.connect(lambda w=which: self._commit(w, True))
            self.number_edit[which].editingFinished.connect(lambda w=which: self._commit(w, False))
        fields.setColumnStretch(1, 1)
        fields.setColumnStretch(2, 1)
        self.field_message = _label()
        self.field_message.setObjectName("Bad")
        self.field_message.setVisible(False)
        self.local_note = _label(muted=True)
        self.real_note = _label(muted=True)
        self.window_box = QWidget()
        wb = QVBoxLayout(self.window_box)
        wb.setContentsMargins(0, 0, 0, 0)
        wb.addLayout(fields)
        wb.addWidget(self.field_message)
        wb.addWidget(self.local_note)
        wb.addWidget(self.real_note)

        self._layout_runner = SlicedRunner(self)
        self._layout_runner.done.connect(self._on_layout_done)
        self._layout_runner.failed.connect(self._on_layout_failed)
        self._map_failed = False

        # The wizard around this step scrolls; the step itself does not, so
        # there is one scrollbar and Back and Next stay in reach.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(
            _label(
                "Choose what to keep of the recordings. The map shows when each one has data. "
                "The new recording holds the kept parts in time order, with the gaps between "
                "them as they really were. Parts may run at the same time (side by side) only "
                "when they hold different channels: two parts that overlap in time must not "
                "share a channel you keep.",
                muted=True,
            )
        )
        layout.addWidget(self.zone_note)
        layout.addWidget(self.viewer_note)
        layout.addWidget(self.mode_window)
        layout.addWidget(self.mode_each)
        layout.addWidget(self.map, 2)
        layout.addLayout(self.map_buttons)
        layout.addWidget(self.window_box)
        self.table.setMinimumHeight(round(self.fontMetrics().height() * 9))
        layout.addWidget(self.table, 1)
        layout.addWidget(self.summary)
        layout.addWidget(self.problems)

    # -- the recordings ----------------------------------------------------------------
    @property
    def rows(self) -> list[KeepRow]:
        """The rows of the recordings that are kept (those the window leaves
        out are not part of the new recording, or of any later step)."""
        return [r for r in self.all_rows if r.entry.key not in self.left_out]

    def set_recordings(self, entries: list[RecordingEntry]) -> None:
        """List ``entries`` in time order, keeping each one's choice."""
        states = {r.entry.key: r.state() for r in self.all_rows}
        entries = sorted(entries, key=lambda e: (e.details.start, e.details.end, e.key))
        self.table.setRowCount(0)  # deletes the old rows' widgets
        zone = self.page.zone
        self.all_rows = []
        for e in entries:
            row = KeepRow(e, zone, states.get(e.key))
            row.changed.connect(self._row_changed)
            self.all_rows.append(row)
        rows = self.all_rows
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            details = row.entry.details
            self.table.setItem(i, 0, _item(row.entry.name))
            self.table.setItem(i, 1, _item(format_time(details.start, zone)))
            self.table.setItem(i, 2, _item(format_time(details.end, zone)))
            self.table.setCellWidget(i, 3, row)
            self.table.setItem(i, 4, _item(""))
        self.table.resizeColumnsToContents()
        self.table.resizeRowsToContents()
        self.zone_note.setText(f"Times are shown in {zone_words(zone)}; type them the same way.")
        keys = tuple(e.key for e in entries)
        if keys != self._keys:
            # A different set of recordings: the window starts as all of it, and
            # several recordings are trimmed with one window by default.
            self._keys = keys
            lo = min((e.details.start for e in entries), default=0)
            hi = max((e.details.end for e in entries), default=1)
            self.bounds = (lo, max(hi, lo + 1))
            self.window = self.bounds
            self.left_out = frozenset()
            self._runs = {}
            self._set_mode(MODE_WINDOW if len(entries) > 1 else MODE_EACH)
        self.map.set_bounds(*self.bounds)
        self.map.zone = zone
        self._show_window()
        if entries:
            self._start_layout(entries)
        else:
            self._layout_runner.cancel()
            self.map.set_layout(None, refit=True)
        self.recompute()

    def set_zone(self) -> None:
        """Show the same choices in the page's (new) time zone."""
        if self.all_rows:
            self.set_recordings([r.entry for r in self.all_rows])
        self.map.set_zone(self.page.zone)

    def set_theme(self, theme: Theme) -> None:
        self.map.set_theme(theme)

    # -- the map ----------------------------------------------------------------------------
    @property
    def layout_ready(self) -> bool:
        """The map (and so the stretches with data) is worked out for the
        recordings on this page."""
        return self._layout_keys == tuple(sorted(r.entry.key for r in self.all_rows))

    def _start_layout(self, entries: list[RecordingEntry]) -> None:
        items = [RecordingInput(e.key, e.name, e.details) for e in entries]
        cache = self._coverage_cache
        keys = tuple(sorted(i.key for i in items))
        self._map_failed = False
        self.mode_window.setEnabled(True)

        def work():
            layout, new_cache = yield from combine_steps(items, cache)
            return layout, new_cache, keys

        self._layout_runner.submit(work())

    def _on_layout_done(self, result) -> None:
        if result is None:
            return
        layout, cache, keys = result
        if keys != tuple(sorted(r.entry.key for r in self.all_rows)):
            return  # an older request, for other recordings
        self._coverage_cache = cache
        self._runs = {r.key: r.runs for r in layout.rows if r.has_data}
        refit = keys != self._layout_keys
        was_ready = self.layout_ready
        self._layout_keys = keys
        self.map.set_layout(layout, refit=refit)
        self.map.set_zone(self.page.zone)
        # Only a window that now leaves a recording out changes anything (a
        # check that is running must not be spoilt by the map arriving).
        before = self.left_out
        self._apply_window()
        if self.left_out != before or not was_ready:
            self.recompute()

    def _on_layout_failed(self) -> None:
        """The map could not be worked out: say so, and go on without it."""
        self._map_failed = True
        self.mode_window.setEnabled(False)
        self._set_mode(MODE_EACH)
        self._show_window()
        self.recompute()

    def _map_moved(self, start: int, end: int) -> None:
        self._set_window(start, end)

    # -- the window -----------------------------------------------------------------------
    def _mode_toggled(self, *_args) -> None:
        mode = MODE_WINDOW if self.mode_window.isChecked() else MODE_EACH
        if mode != self.mode:
            self.mode = mode
            self._show_window()
            self.recompute()

    def _set_mode(self, mode: str) -> None:
        self.mode = mode
        for button, wanted in ((self.mode_window, MODE_WINDOW), (self.mode_each, MODE_EACH)):
            button.blockSignals(True)
            button.setChecked(mode == wanted)
            button.blockSignals(False)

    def _row_changed(self, *_args) -> None:
        """A choice for one recording: that is a choice per recording."""
        if self.mode == MODE_WINDOW:
            self._set_mode(MODE_EACH)
            self._show_window()
        self.recompute()

    def _show_window(self) -> None:
        window = self.mode == MODE_WINDOW
        self.window_box.setVisible(window)
        self.map.set_lines_enabled(window)
        for row in self.all_rows:
            row.setEnabled(not window)
        self.map.set_window(*self.window)
        self._write_fields()

    def set_window(self, start: int, end: int) -> None:
        """Choose the window (as dragging the lines would): switches to the
        window for all recordings."""
        if self.mode != MODE_WINDOW:
            self._set_mode(MODE_WINDOW)
            self._show_window()
        self._set_window(start, end)

    def _set_window(self, start: int, end: int) -> None:
        lo, hi = self.bounds
        start = min(max(int(start), lo), hi - 1)
        end = max(min(int(end), hi), start + 1)
        self.window = (start, end)
        self.map.set_window(start, end)
        self._write_fields()
        self.field_message.setVisible(False)
        self.recompute()

    def _write_fields(self) -> None:
        for which, t in ((START, self.window[0]), (END, self.window[1])):
            self.date_edit[which].setText(format_utc_us(t))
            self.number_edit[which].setText(str(t))
        zone = self.page.zone
        if zone is UTC:
            self.local_note.setVisible(False)
        else:
            self.local_note.setText(
                f"In {zone_words(zone)}: Start {format_time(self.window[0], zone, 6)}, "
                f"End {format_time(self.window[1], zone, 6)}."
            )
            self.local_note.setVisible(True)

    def _say(self, text: str) -> None:
        self.field_message.setText(text)
        self.field_message.setVisible(bool(text))

    def _commit(self, which: str, is_date: bool) -> None:
        """A date or number was typed: move the line there, or say why not
        (the line stays where it is)."""
        edit = (self.date_edit if is_date else self.number_edit)[which]
        text = edit.text()
        current = self.window[0] if which == START else self.window[1]
        if text.strip() in (format_utc_us(current), str(current)):
            return  # unchanged (focus just left the field)
        t = parse_utc_text(text) if is_date else parse_whole_number(text)
        if t is None:
            self._say(
                f"The {LABELS[which]} {'date and time' if is_date else 'number'} cannot be read. "
                + (DATE_HELP if is_date else NUMBER_HELP)
                + f" The {LABELS[which]} line has not moved."
            )
            return
        lo, hi = self.bounds
        note = ""
        if t < lo:
            t, note = (
                lo,
                "That is before the first recording starts, so it was moved to the first moment.",
            )
        elif t > hi:
            t, note = (
                hi,
                "That is after the last recording ends, so it was moved to the last moment.",
            )
        start, end = self.window
        if which == START:
            if t >= end:
                self._say("Start must be before End. The Start line has not moved.")
                return
            start = t
        else:
            if t <= start:
                self._say("End must be after Start. The End line has not moved.")
                return
            end = t
        self._set_window(start, end)
        self._say(note)

    def _apply_window(self) -> None:
        """Work out what each recording keeps in window mode (and which
        are left out)."""
        if self.mode != MODE_WINDOW:
            self.left_out = frozenset()
            self._window_keeps = {}
            return
        layouts = {
            r.entry.key: Layout(
                r.entry.details.start,
                r.entry.details.end,
                tuple(c.name for c in r.entry.details.channels),
            )
            for r in self.all_rows
        }
        keeps, left = window_keeps(layouts, self.window[0], self.window[1], self._runs or None)
        self._window_keeps = keeps
        self.left_out = frozenset(left)

    def keep_of(self, row: KeepRow) -> Keep:
        if self.mode == MODE_WINDOW:
            found = self._window_keeps.get(row.entry.key)
            if found is not None:
                return found
        return row.keep()

    def set_dates_moved(self, moved: bool) -> None:
        self.real_note.setText(TIMES_ARE_REAL + (TIMES_ARE_REAL_MOVED if moved else ""))

    # -- what the page and the later steps ask --------------------------------------------
    def sources(self) -> list[JoinSource]:
        self._apply_window()
        return [
            JoinSource(r.entry.key, r.entry.location, self.keep_of(r), r.entry.password)
            for r in self.rows
        ]

    def layouts(self) -> dict:
        return {
            r.entry.key: Layout(
                r.entry.details.start,
                r.entry.details.end,
                tuple(c.name for c in r.entry.details.channels),
                r.entry.outcome.size_bytes,
                r.entry.outcome.stamp,
            )
            for r in self.rows
        }

    def known_layouts(self) -> dict:
        """What Explore knows of the chosen recordings, to hand to the check
        and the run so they do not open them again. Left out: a recording
        whose times Explore could not confirm (the full-access password was
        not given): the check opens that one and says so."""
        layouts = self.layouts()
        return {
            r.entry.key: layouts[r.entry.key]
            for r in self.rows
            if not r.entry.details.offset_assumed_zero
            and None not in (r.entry.details.start, r.entry.details.end)
        }

    def recompute(self, *_args) -> None:
        self._apply_window()
        self.plan = plan_join(
            self.sources(), self.layouts(), excluded=self.page.selection.excluded, changes=True
        )
        names = {r.entry.key: r.entry.name for r in self.all_rows}
        overlapping = self.plan.overlapping
        shown = {}
        left_out_words = {}
        total = 0.0
        self.preview_rows = []
        for i, row in enumerate(self.all_rows):
            key = row.entry.key
            span = self.plan.spans.get(key)
            problem = self.plan.problem_of(key)
            if key in self.left_out:
                text = LEFT_OUT_TEXT
                left_out_words[key] = LEFT_OUT_TEXT
            elif problem is not None:
                text = problem_text(problem, names)
            elif span is not None:
                total += span.seconds
                text = ("All of it" if span.whole else format_span(span.seconds)) + (
                    " (overlaps)" if key in overlapping else ""
                )
            else:
                text = ""
            if span is not None and key not in self.left_out:
                shown[key] = (span.start, span.end)
            item = self.table.item(i, 4)
            if item is not None:
                item.setText(text)
            details = row.entry.details
            self.preview_rows.append(
                (row.entry.name, details.start, details.end, span, key in overlapping)
            )
        self.map.set_kept(shown, left_out_words)
        self.table.resizeColumnToContents(4)
        self.summary.setText(
            f"The new recording will hold about {format_span(total)} of signal."
            if self.rows and total
            else ""
        )
        words = list(dict.fromkeys(problem_text(p, names) for p in self.plan.problems))
        if self._map_failed and self.mode == MODE_EACH:
            words = [MAP_FAILED, *words]
        if self.mode == MODE_WINDOW and self.all_rows and not self.layout_ready:
            words = [WAIT_FOR_MAP]
        elif self.mode == MODE_WINDOW and self.all_rows and not self.rows:
            words = [NO_DATA_IN_WINDOW]
        self.problems.setText("\n".join(words))
        self.problems.setVisible(bool(words))
        self.changed.emit()

    @property
    def ok(self) -> bool:
        waiting = self.mode == MODE_WINDOW and not self.layout_ready
        return bool(self.rows) and self.plan.ok and not waiting


def zone_words(zone: tzinfo) -> str:
    return "UTC" if zone is UTC else f"the site time zone ({getattr(zone, 'key', zone)})"


# --------------------------------------------------------------------------
# Step 4: the new recording


class PrivacyStep(QWidget):
    """Keep the patient details, or make the new recording anonymous: the
    Anonymize page's own choices for ONE copy (its dates step and its
    clear-and-text box are used here), with no separate pseudonym table:
    there is one new recording, named on the next step."""

    changed = Signal()

    def __init__(self, page: JoinerPage):
        super().__init__(page)
        self.page = page
        #: What ``DatesStep`` reads: the chosen recordings, as the Anonymize
        #: page's ``Choice`` objects.
        self.choices: list[Choice] = []
        self.keep_details = QRadioButton(PRIVACY_KEEP)
        self.anonymous = QRadioButton(PRIVACY_ANONYMOUS)
        self.keep_details.setChecked(True)
        group = QButtonGroup(self)
        for b in (self.keep_details, self.anonymous):
            group.addButton(b)
            b.toggled.connect(self._update)
        intro = _label(
            "Choose whether the new recording still says who the patient is. An anonymous "
            "recording is made straight from the originals: a copy with the patient's "
            "details is never written to your computer.",
            muted=True,
        )
        self.dates_keep = QRadioButton(DATES_KEEP)
        self.dates_move = QRadioButton(DATES_MOVE)
        self.dates_move.setChecked(True)
        dates_group = QButtonGroup(self)
        for b in (self.dates_keep, self.dates_move):
            dates_group.addButton(b)
            b.toggled.connect(self._update)
        self.dates_warning = _label(DATES_KEEP_WARNING)
        self.dates_warning.setObjectName("Bad")
        self.dates = DatesStep(self, single=True)
        self.dates.changed.connect(self._update)
        self.pseudonym = QLineEdit()
        self.pseudonym.setPlaceholderText("The same as the name of the new recording")
        self.pseudonym.textChanged.connect(self._update)
        self.details = DetailsAndTextBox(self)
        self.details.changed.connect(self._update)
        self.span_note = _label(muted=True)
        self.problem = _label()
        self.problem.setObjectName("Bad")
        self.notes_keep = QRadioButton(NOTES_KEEP)
        self.notes_leave_out = QRadioButton(NOTES_LEAVE_OUT)
        self.notes_keep.setChecked(True)
        notes_group = QButtonGroup(self)
        for b in (self.notes_keep, self.notes_leave_out):
            notes_group.addButton(b)
            b.toggled.connect(self._update)
        self.notes_note = _label(NOTES_LEFT_OUT_NOTE, muted=True)
        form = QFormLayout()
        form.addRow("Pseudonym (stored as the patient's code):", self.pseudonym)
        self.options = QWidget()
        inner = QVBoxLayout(self.options)
        inner.setContentsMargins(int(self.fontMetrics().height()), 0, 0, 0)
        inner.addWidget(_label("Dates", bold=True))
        inner.addWidget(self.dates_keep)
        inner.addWidget(self.dates_warning)
        inner.addWidget(self.dates_move)
        inner.addWidget(self.dates)
        inner.addWidget(self.span_note)
        inner.addWidget(_label("Name", bold=True))
        inner.addLayout(form)
        inner.addWidget(self.details)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(intro)
        layout.addWidget(self.keep_details)
        layout.addWidget(self.anonymous)
        layout.addWidget(self.options)
        layout.addWidget(_label("Notes and events", bold=True))
        layout.addWidget(_label(NOTES_INTRO, muted=True))
        layout.addWidget(self.notes_keep)
        layout.addWidget(self.notes_leave_out)
        layout.addWidget(self.notes_note)
        layout.addWidget(self.problem)
        layout.addStretch(1)
        self._update()

    @property
    def zone(self) -> tzinfo:
        return self.page.zone

    def is_anonymous(self) -> bool:
        return self.anonymous.isChecked()

    def set_recordings(self) -> None:
        self.choices = [Choice(r.entry) for r in self.page.keep.rows]
        self.dates.set_recordings(self.choices)
        self._update()

    def shift_us(self) -> int | None:
        """How far every time is moved; None = the dates are kept."""
        if self.dates_keep.isChecked():
            return None
        plan = self.dates.plan
        planned = plan.recordings[0] if plan is not None and plan.recordings else None
        return planned.shift_us if planned is not None else None

    def records(self) -> str:
        """``RECORDS_KEEP`` or ``RECORDS_DROP`` (mef3io's ``records=``)."""
        return RECORDS_DROP if self.notes_leave_out.isChecked() else RECORDS_KEEP

    def choice(self) -> Anonymous | None:
        if not self.is_anonymous():
            return None
        shift = self.shift_us()
        if shift is None and self.dates_move.isChecked():
            # Fail closed: "move the dates" without a plan must never turn
            # into "keep the real dates".
            raise ValueError("The dates are to be moved, but there is no plan for it.")
        return Anonymous(
            shift_us=shift,
            pseudonym=self.pseudonym.text().strip(),
            clear=self.details.clear(),
            text=self.details.text(),
        )

    def problem_text(self) -> str:
        if not self.is_anonymous():
            return ""
        if self.dates_move.isChecked() and not self.dates.ok:
            return "Fix what is listed above first."
        pseudonym = self.pseudonym.text().strip()
        if pseudonym:
            found = name_problem(pseudonym)
            if found:
                return f"Pseudonym: {found}"
        return ""

    def _refresh_note(self) -> None:
        text = ""
        if self.is_anonymous() and self.page.keep.rows:
            plan = plan_join(
                self.page.keep.sources(),
                self.page.keep.layouts(),
                excluded=self.page.selection.excluded,
                changes=True,
            )
            if plan.ok and plan.spans:
                start = min(s.start for s in plan.spans.values())
                end = max(s.end for s in plan.spans.values())
                shift = self.shift_us()
                if self.dates_move.isChecked() and shift is None:
                    text = ""
                elif shift is None:
                    text = (
                        "The new recording will run from "
                        f"{format_time(start, self.zone)} to {format_time(end, self.zone)}."
                    )
                else:
                    text = (
                        "The new recording will run from "
                        f"{format_time(start + shift, UTC)} to {format_time(end + shift, UTC)} "
                        "(UTC)."
                    )
        self.span_note.setText(text)
        self.span_note.setVisible(bool(text))

    def _update(self, *_args) -> None:
        anonymous = self.is_anonymous()
        self.options.setVisible(anonymous)
        leaving_out = self.notes_leave_out.isChecked()
        self.notes_note.setVisible(leaving_out)
        # Nothing to remove the text of when there are no notes.
        self.details.show_text_choice(not leaving_out)
        moving = anonymous and self.dates_move.isChecked()
        self.dates.setVisible(moving)
        self.dates_warning.setVisible(anonymous and self.dates_keep.isChecked())
        self._refresh_note()
        text = self.problem_text()
        self.problem.setText(text)
        self.problem.setVisible(bool(text))
        self.changed.emit()

    def reset(self) -> None:
        self.keep_details.setChecked(True)
        self.notes_keep.setChecked(True)
        self.pseudonym.clear()


class NewRecordingStep(QWidget):
    changed = Signal()

    def __init__(self, page: JoinerPage):
        super().__init__(page)
        self.page = page
        self.folder = ""
        self.name = QLineEdit()
        self.name.textChanged.connect(self._update)
        self.folder_label = _label(muted=True)
        self.folder_button = QPushButton("Choose the folder…")
        self.folder_button.clicked.connect(self._choose_folder)
        self._auto_name = ""
        self.save_folder = QRadioButton(SAVE_FOLDER)
        self.save_file = QRadioButton(SAVE_FILE)
        self.save_folder.setChecked(True)
        save_group = QButtonGroup(self)
        for b in (self.save_folder, self.save_file):
            save_group.addButton(b)
            b.toggled.connect(self._update)
        self.file_note = _label(FILE_NOTE, muted=True)
        form = QFormLayout()
        form.addRow("Name of the new recording:", self.name)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(self.folder_button)
        form.addRow("Saved in:", folder_row)
        form.addRow("Save as:", self._stack(self.save_folder, self.save_file))
        self.passwords = PasswordsStep(
            self,
            what="the new recording",
            explanation=PASSWORD_EXPLANATION,
            none_label="No password",
        )
        self.passwords.changed.connect(self._update)
        self.protected_note = _label(PROTECTED_SOURCES)
        self.protected_note.setObjectName("Caution")
        self.how = _label(HOW_IT_WORKS, muted=True)
        self.problem = _label()
        self.problem.setObjectName("Bad")
        self.findings = QVBoxLayout()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(form)
        layout.addWidget(self.file_note)
        layout.addWidget(_label("Password protection", bold=True))
        layout.addWidget(self.protected_note)
        layout.addWidget(self.passwords)
        layout.addWidget(self.how)
        layout.addWidget(self.problem)
        layout.addLayout(self.findings)
        layout.addStretch(1)

    @staticmethod
    def _stack(*widgets) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        for w in widgets:
            layout.addWidget(w)
        return box

    @property
    def one_file(self) -> bool:
        return self.save_file.isChecked()

    def prepare(self) -> None:
        """Fill in a name and folder the first time (from the first
        recording), and say what applies to these recordings. An anonymous
        recording is never given a name made from an original's name."""
        rows = self.page.keep.rows
        if not rows:
            return
        first = rows[0].entry
        anonymous = self.page.privacy.is_anonymous()
        if not self.folder:
            self.folder = os.path.dirname(first.location)
        if anonymous:
            auto = "SUBJ-001"
        else:
            auto = f"{first.name}-{'joined' if len(rows) > 1 else 'part'}"
        if not self.name.text() or self.name.text() == self._auto_name:
            self._auto_name = auto
            self.name.setText(auto)
        self.protected_note.setVisible(
            any(r.entry.details.password_protected for r in rows)
            and not self.passwords.both.isChecked()
        )
        self.how.setText(HOW_IT_WORKS_ANONYMOUS if anonymous else HOW_IT_WORKS)
        self.how.setVisible(len(rows) > 1 or anonymous)
        self.set_findings([])
        self._update()

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Choose where to save the new recording", self.folder
        )
        if folder:
            self.folder = folder
            self._update()

    def destination(self) -> str:
        name = self.name.text().strip()
        if self.one_file:
            return os.path.join(self.folder, f"{name}.mefd.tar")
        return destination_for(self.folder, name)

    def problem_text(self) -> str:
        name = self.name.text()
        found = name_problem(name)
        if found:
            return found
        if not self.folder or not os.path.isdir(self.folder):
            return "Choose a folder that exists."
        dst = self.destination()
        if os.path.lexists(dst):
            return "A file or folder with that name is already in the chosen folder."
        if not self.one_file and os.path.lexists(dst + ".partial"):
            return (
                "A folder ending in .partial, left by an earlier run that did not finish, is "
                "in the way. If nothing else is running, delete it, or choose another name."
            )
        if inside_any_recording(dst, [r.entry.location for r in self.page.keep.rows]):
            return "Choose a folder that is not inside a recording."
        password = self.passwords.problem_text()
        if password:
            return password
        plan = plan_join(
            self.page.keep.sources(),
            self.page.keep.layouts(),
            excluded=self.page.selection.excluded,
            changes=self.page.has_changes(),
        )
        if not plan.ok:
            names = {r.entry.key: r.entry.name for r in self.page.keep.rows}
            return problem_text(plan.problems[0], names)
        return ""

    def _update(self, *_args) -> None:
        self.folder_label.setText(self.folder or "No folder chosen")
        self.protected_note.setVisible(
            any(r.entry.details.password_protected for r in self.page.keep.rows)
            and not self.passwords.both.isChecked()
        )
        self.file_note.setVisible(self.one_file)
        text = self.problem_text()
        self.problem.setText(text)
        self.problem.setVisible(bool(text))
        self.changed.emit()

    def set_findings(self, findings: list, names: dict | None = None) -> None:
        _clear(self.findings)
        names = names or {}
        if not findings:
            return
        frame = QFrame()
        frame.setObjectName("Warning")
        inner = QVBoxLayout(frame)
        inner.addWidget(_label("This cannot be saved yet", bold=True))
        for f in findings:
            who = names.get(f.key, "") if f.key else ""
            prefix = f"{who}: " if who else ""
            inner.addWidget(_label(f"{prefix}{f.message.title}. {f.message.text}"))
            if f.message.detail:
                inner.addWidget(TechnicalDetails(f.message.detail))
        self.findings.addWidget(frame)


# --------------------------------------------------------------------------
# Step 5: saving, then the result


def saved_words(result: JoinResult) -> str:
    """The result screen's sentence for a recording that was saved and
    checked."""
    kept = format_count(len(result.channels_kept), "channel")
    how = ""
    if result.anonymous:
        how += " It is anonymous: the patient's details were cleared as it was made."
    if result.notes_left_out:
        how += " All notes and events were left out."
    if result.one_file:
        how += " It was saved as one file, which this app opens directly."
    return (
        f"The new recording was saved with {kept} and checked against the originals, "
        f"which were not changed.{how}"
    )


class RunStep(QWidget):
    def __init__(self, page: JoinerPage):
        super().__init__(page)
        self.page = page
        self.heading = _label(bold=True)
        self.heading.setFont(scaled_font(self, 1.15, bold=True))
        self.progress_slot = QVBoxLayout()
        self.result = _label()
        self.details_slot = QVBoxLayout()
        self.open_button = QPushButton("Open it in Explore")
        self.open_button.setProperty("primary", True)
        self.open_button.clicked.connect(page.open_result)
        self.folder_button = QPushButton("Show the folder")
        self.folder_button.clicked.connect(page.show_folder)
        buttons = QHBoxLayout()
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.folder_button)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.heading)
        layout.addLayout(self.progress_slot)
        layout.addWidget(self.result)
        layout.addLayout(self.details_slot)
        layout.addLayout(buttons)
        layout.addStretch(1)
        self.show_buttons(False)

    def show_buttons(self, shown: bool) -> None:
        self.open_button.setVisible(shown)
        self.folder_button.setVisible(shown)

    def start(self, handle, several: bool, anonymous: bool = False) -> None:
        _clear(self.progress_slot)
        _clear(self.details_slot)
        self.result.setText("")
        self.show_buttons(False)
        self.heading.setText(
            "Joining the recordings…" if several else "Saving the part of the recording…"
        )
        if anonymous:
            self.heading.setText("Making the anonymous recording…")
        self.progress = JobProgressWidget(handle, self)
        self.progress_slot.addWidget(self.progress)

    def finish(self, result: JoinResult | None, stopped: bool, failure=None) -> None:
        _clear(self.details_slot)
        left = (
            " The hidden temporary folder (its name starts with .mef3io-studio-parts-) could "
            "NOT be removed: delete it yourself, in the chosen folder."
            if result is not None and result.parts_left
            else ""
        )
        if stopped:
            self.heading.setText("Stopped")
            self.result.setText(
                "Nothing half-made was left behind, and the originals were not changed." + left
            )
        elif result is not None and result.ok:
            self.heading.setText("Saved and checked")
            self.result.setText(saved_words(result) + left)
            self.show_buttons(True)
        else:
            message = result.message if result is not None else failure
            self.heading.setText(message.title if message else "Not saved")
            self.result.setText((message.text if message else "") + left)
            if message is not None and message.detail:
                self.details_slot.addWidget(TechnicalDetails(message.detail))


# --------------------------------------------------------------------------
# The page


class JoinerPage(QWidget):
    """The "Join or trim" module."""

    #: Open the new recording in Explore: (path, password).
    open_recording = Signal(str, str)
    status = Signal(str)
    #: "View these recordings": the chosen Explore entries, to look at in the
    #: signal viewer.
    view_requested = Signal(list)

    def __init__(self, jobs: JobManager, explorer, parent=None):
        super().__init__(parent)
        self.jobs = jobs
        self.explorer = explorer
        self.zone: tzinfo = UTC
        self._zone_name = ""
        self.theme: Theme = LIGHT
        self.selection = ChannelSelection({})
        self._selection_keys: tuple[str, ...] = ()
        self.check_handle = None
        self.handle = None
        self.request: JoinRequest | None = None
        self.result: JoinResult | None = None
        self._stopped = False
        self._failure = None
        #: Bumped by every change of a choice: a check that finishes after
        #: one is stale and never starts a run.
        self._generation = 0
        self._recordings_dirty = True
        self._built = False
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        model = explorer.model
        for signal in (
            model.rowsInserted,
            model.rowsRemoved,
            model.modelReset,
            model.dataChanged,
            model.layoutChanged,
        ):
            signal.connect(self._explorer_changed)

    # Built on first use, like the Anonymize page (widgets made while the
    # user is on another page slow that page's tasks).
    def __getattr__(self, name: str):
        if name.startswith("_") or self.__dict__.get("_built", True):
            raise AttributeError(name)
        self._build()
        return object.__getattribute__(self, name)

    @property
    def built(self) -> bool:
        return self._built

    def _build(self) -> None:
        if self._built:
            return
        self._built = True
        em = self.fontMetrics().height()
        title = _label("Join or trim recordings", wrap=False)
        title.setFont(scaled_font(self, 1.4, bold=True))
        self.step_labels: list[QLabel] = []
        steps_row = FlowLayout()
        for i, name in enumerate(STEPS):
            label = QLabel(f"{i + 1}. {name}")
            self.step_labels.append(label)
            steps_row.addWidget(label)
        self.recordings = JoinRecordingsStep(self)
        self.keep = KeepStep(self)
        self.picker = ChannelPicker(self)
        self.privacy = PrivacyStep(self)
        self.new = NewRecordingStep(self)
        self.run_step = RunStep(self)
        self.stack = QStackedWidget(self)
        for widget in (
            self.recordings,
            self.picker,
            self.keep,
            self.privacy,
            self.new,
            self.run_step,
        ):
            self.stack.addWidget(widget)
        for step in (self.recordings, self.keep, self.picker, self.privacy, self.new):
            step.changed.connect(self._inputs_changed)
        self.keep.set_theme(self.theme)

        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(self.back)
        self.next_button = QPushButton("Next")
        self.next_button.setProperty("primary", True)
        self.next_button.clicked.connect(self.next)
        self.why_not = _label(muted=True)
        self.view_button = QPushButton("View these recordings")
        self.view_button.setToolTip("Look at the chosen recordings in the signal viewer")
        self.view_button.clicked.connect(self.view_chosen)
        nav = QHBoxLayout()
        nav.addWidget(self.why_not, 1)
        nav.addWidget(self.view_button)
        nav.addWidget(self.back_button)
        nav.addWidget(self.next_button)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(int(em), int(em * 0.8), int(em), int(em * 0.6))
        layout.addWidget(title)
        layout.addLayout(steps_row)
        layout.addWidget(self.stack, 1)
        layout.addLayout(nav)
        self._outer.addWidget(_scroll(inner))

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(REFRESH_DELAY_MS)
        self._refresh_timer.timeout.connect(self._refresh_recordings)
        self._recordings_dirty = False
        self.recordings.refresh()
        self.go(STEP_RECORDINGS)

    # -- shell ------------------------------------------------------------------------
    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        if self._built:
            self.keep.set_theme(theme)

    def set_zone_name(self, name: str) -> None:
        self.zone = resolve_zone(name)
        self._zone_name = name
        if self._built:
            self.privacy.dates.set_zone_name(name)
            self.recordings.refresh()
            self.keep.set_zone()

    @property
    def step(self) -> int:
        return self.stack.currentIndex()

    def _explorer_changed(self, *_args) -> None:
        self._recordings_dirty = True
        if self._built and self.isVisible() and self.step == STEP_RECORDINGS:
            self._refresh_timer.start()

    def _refresh_recordings(self) -> None:
        if not self._recordings_dirty:
            return
        self._recordings_dirty = False
        self.recordings.refresh()
        self._update_buttons()

    def showEvent(self, event) -> None:  # noqa: N802
        self._build()
        super().showEvent(event)
        if self.step == STEP_RECORDINGS and self._recordings_dirty:
            self._refresh_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        # Passwords typed but not used yet are not kept while the page is away.
        if self._built and self.step != STEP_RUN:
            self._generation += 1
            self.new.passwords.forget()
        super().hideEvent(event)

    def _still_readable(self) -> bool:
        """Every chosen recording is still in Explore with its details; if
        not, back to the first step, saying why."""
        listed = {e.key: e for e in self.explorer.entries()}
        lost = [
            r.entry.name
            for r in self.keep.all_rows
            if (e := listed.get(r.entry.key)) is None or e.details is None or e.busy
        ]
        if not lost:
            return True
        self.recordings.ticked -= {k for k in self.recordings.ticked if k not in listed}
        self.go(STEP_RECORDINGS)
        self.recordings.lost.setText(
            "These recordings were removed from Explore, or are being read again, so they "
            f"were taken out: {', '.join(lost)}. Check the list and continue."
        )
        self.recordings.lost.setVisible(True)
        return False

    def _inputs_changed(self, *_args) -> None:
        self._generation += 1
        self.keep.set_dates_moved(
            self.privacy.is_anonymous() and self.privacy.dates_move.isChecked()
        )
        self._update_buttons()

    def _checking(self) -> bool:
        return self.check_handle is not None and not self.check_handle.is_finished

    def go(self, step: int) -> None:
        self._generation += 1
        if step > STEP_RECORDINGS and step != STEP_RUN and not self._still_readable():
            return
        if step != STEP_RECORDINGS:
            self.recordings.lost.setVisible(False)
        self.stack.setCurrentIndex(step)
        for i, label in enumerate(self.step_labels):
            label.setObjectName(
                "StepCurrent" if i == step else ("StepDone" if i < step else "StepTodo")
            )
            label.setFont(scaled_font(label, 1.0, bold=i == step))
            label.style().unpolish(label)
            label.style().polish(label)
        if step == STEP_RECORDINGS:
            self._recordings_dirty = False
            self.recordings.refresh()
        elif step == STEP_CHANNELS:
            self._prepare_channels()
        elif step == STEP_KEEP:
            self._prepare_channels()
            self.keep.set_dates_moved(
                self.privacy.is_anonymous() and self.privacy.dates_move.isChecked()
            )
            self.keep.recompute()
        elif step == STEP_PRIVACY:
            self.privacy.set_recordings()
        elif step == STEP_NEW:
            self.new.prepare()
        passwords = self.new.passwords
        passwords.cleared.setVisible(step == STEP_NEW and passwords.cleared_once)
        self._update_buttons()

    def _prepare_channels(self) -> None:
        entries = [r.entry for r in self.keep.all_rows]
        keys = tuple(e.key for e in entries)
        if keys == self._selection_keys:
            return
        previous = self.selection if self._selection_keys else None
        self.selection, notice = channel_selection_for(entries, previous)
        self._selection_keys = keys
        self.picker.set_selection(self.selection, len(entries), notice)

    def has_changes(self) -> bool:
        """A channel is left out, new passwords are chosen, or the new
        recording is made anonymous."""
        return (
            bool(self.selection.excluded)
            or bool(self.keep.left_out)
            or self.new.passwords.both.isChecked()
            or self.privacy.is_anonymous()
        )

    def _blocker_text(self) -> str:
        step = self.step
        if step == STEP_RECORDINGS:
            return "" if self.recordings.ticked else "Tick at least one recording."
        if step == STEP_KEEP:
            return "" if self.keep.ok else "Fix what is listed above first."
        if step == STEP_CHANNELS:
            return "" if self.selection.can_continue else "Keep at least one channel."
        if step == STEP_PRIVACY:
            return self.privacy.problem_text()
        if step == STEP_NEW:
            if self._checking():
                return "Checking everything before starting…"
            return "Fix what is listed above first." if self.new.problem_text() else ""
        return ""

    def _update_buttons(self, *_args) -> None:
        step = self.step
        running = self.handle is not None and not self.handle.is_finished
        why = self._blocker_text()
        self.why_not.setText(why)
        checking = self._checking()
        # Nothing can be changed while the check runs (it checks THESE choices).
        self.stack.setEnabled(not checking)
        self.back_button.setVisible(step != STEP_RUN)
        self.back_button.setEnabled(step > STEP_RECORDINGS and not checking)
        if step == STEP_NEW:
            self.next_button.setText("Check and save")
        elif step == STEP_RUN:
            self.next_button.setText("Join or trim more recordings")
        else:
            self.next_button.setText("Next")
        self.next_button.setEnabled(not running if step == STEP_RUN else not why)
        self.view_button.setVisible(step in (STEP_RECORDINGS, STEP_CHANNELS, STEP_KEEP))
        self.view_button.setEnabled(bool(self.recordings.ticked) and not checking)

    def view_chosen(self) -> None:
        """Open the signal viewer on the recordings chosen so far."""
        entries = [e for e in self.recordings.chosen() if not e.busy]
        if entries:
            self.view_requested.emit(entries)

    def back(self) -> None:
        if STEP_RECORDINGS < self.step < STEP_RUN and not self._checking():
            self.go(self.step - 1)

    def next(self):
        step = self.step
        if step == STEP_RUN:
            self.restart()
            return None
        if self._blocker_text():
            return None
        if step == STEP_RECORDINGS:
            self._refresh_recordings()
            self.keep.set_recordings(self.recordings.chosen())
        if step == STEP_NEW:
            return self.check_and_save()
        self.go(step + 1)
        return None

    # -- checking, then saving ---------------------------------------------------------
    def make_request(self) -> JoinRequest:
        output = self.new.passwords.passwords()
        return join_model.new_request(
            self.keep.sources(),
            self.new.destination(),
            exclude_channels=self.selection.excluded,
            output_password=output,
            anonymous=self.privacy.choice(),
            one_file=self.new.one_file,
            records=self.privacy.records(),
            layouts=self.keep.known_layouts(),
        )

    def _names(self) -> dict[str, str]:
        return {r.entry.key: r.entry.name for r in self.keep.rows}

    def check_and_save(self):
        """mef3io's checks (nothing written), then, if nothing stops it, the
        run itself."""
        if not self._still_readable():
            return None
        try:
            request = self.make_request()
        except ValueError:
            # A choice on the privacy step is incomplete (for instance the
            # dates are to be moved but there is no plan): say so, start
            # nothing. Never let it vanish in the slot.
            self.new.set_findings(
                [
                    join_model.CheckFinding(
                        None,
                        join_model.UserMessage(
                            "The dates are not ready",
                            "The dates are to be moved, but there is no plan for it yet. "
                            "Go back to the privacy step and check the dates. Nothing was "
                            "changed.",
                        ),
                    )
                ]
            )
            return None
        self.new.set_findings([])
        return self._check_then_start(request)

    def _check_then_start(self, request: JoinRequest):
        handle = self.jobs.submit("Checking before joining", check_join, request)
        self.check_handle = handle
        generation = self._generation

        def checked(findings) -> None:
            if (
                generation != self._generation
                or self.step != STEP_NEW
                or not self.isVisible()
                or self.handle is not None
            ):
                # Something changed while it was being checked (or the page
                # was left): this check is not about the choices on screen.
                self.status.emit(
                    "The choices changed during the check, so nothing was started. Choose "
                    "Check and save again."
                )
                return
            if findings:
                self.new.set_findings(findings, self._names())
                return
            if request.layouts and not join_model.facts_still_true(request):
                # A recording was touched since Explore read it: what the
                # check used may be out of date, so check again, opening
                # every recording afresh. (An unchanged check is not repeated.)
                self.check_handle = None
                self._check_then_start(join_model.without_facts(request))
                return
            self.start(request)

        def failed(message) -> None:
            self.new.set_findings([join_model.CheckFinding(None, message)])

        handle.succeeded.connect(checked)
        handle.failed.connect(failed)
        handle.finished.connect(self._update_buttons)
        self._update_buttons()
        return handle

    def start(self, request: JoinRequest):
        self.request = request
        self.result = None
        self._stopped = False
        several = len(request.sources) > 1
        title = "Joining recordings" if several else "Saving part of a recording"
        if request.anonymous is not None:
            title = "Making an anonymous recording"
        handle = self.jobs.submit(title, join_recordings, request)
        self.handle = handle
        handle.succeeded.connect(self._on_result)
        handle.cancelled.connect(self._on_stopped)
        handle.failed.connect(self._on_failed)
        handle.finished.connect(self._on_finished)
        self.go(STEP_RUN)
        self.run_step.start(handle, several, request.anonymous is not None)
        return handle

    def stop(self) -> None:
        if self.handle is not None:
            self.handle.cancel()

    def _on_result(self, result: JoinResult) -> None:
        self.result = result

    def _on_stopped(self) -> None:
        self._stopped = True

    def _on_failed(self, message) -> None:
        self._failure = message

    def _on_finished(self) -> None:
        self.run_step.finish(self.result, self._stopped, self._failure)
        self._failure = None
        self._update_buttons()
        if self._stopped:
            text = "Joining stopped: nothing was saved."
        elif self.result is not None and self.result.ok:
            text = "The new recording was saved and checked."
            if self.result.anonymous:
                text = "The anonymous recording was saved and checked."
        else:
            text = "The new recording could not be made."
        self.status.emit(text)

    def open_result(self) -> None:
        if self.request is None or self.result is None or not self.result.ok:
            return
        output = self.request.output_password
        self.open_recording.emit(self.request.destination, output[1] if output else "")

    def show_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if self.request is not None:
            folder = os.path.dirname(self.request.destination)
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def choose(self, keys) -> bool:
        """Start over with exactly these recordings (Explore's "Join or trim
        these…"), then go on to the next step. Recordings that cannot be
        used are named on the first step instead. False while a check or a
        run is going on (nothing is changed then)."""
        self._build()
        running = self.handle is not None and not self.handle.is_finished
        if running or self._checking():
            self.status.emit("Join or trim is busy: wait until it has finished.")
            return False
        self.restart()
        keys = list(keys)
        self.recordings.set_ticked(keys)
        dropped = [k for k in keys if k not in self.recordings.ticked]
        if dropped:
            names = {e.key: e.name for e in self.recordings.rows}
            self.recordings.lost.setText(
                "These recordings cannot be used here (the list says why): "
                f"{', '.join(names.get(k, '') for k in dropped)}."
            )
            self.recordings.lost.setVisible(True)
            return True
        if self.recordings.ticked:
            self.next()
        return True

    def choose_window(self, keys, start: int, end: int, hidden=()) -> bool:
        """Start over with exactly these recordings and keep ``[start, end)``
        of each (the signal viewer's "Send to Join or trim"), stopping at the
        "Channels" step so the person sees which channels are kept: the ones
        ``hidden`` in the viewer are left out there (and can be changed). The
        part to keep is set on the "What to keep" step behind it. The page
        works in whole seconds: the start is rounded down and the end up, so
        the part asked for is always inside what is kept. False when it
        could not."""
        if not self.choose(keys) or self.step != STEP_CHANNELS:
            return False
        self._leave_out_hidden(hidden)
        end_up = -(-end // SECOND_US) * SECOND_US
        for row in self.keep.all_rows:
            row.set_how(KEEP_BETWEEN)
            row.start.set_us(max(start, row.entry.details.start), self.zone)
            row.end.set_us(min(end_up, row.entry.details.end), self.zone)
        self.keep.recompute()
        # What is kept now (a recording ending mid-second is kept to its end).
        kept = [r.keep() for r in self.keep.all_rows]
        new_start = min(k.start_us for k in kept)
        new_end = max(k.end_us for k in kept)
        asked = f"{format_time(start, self.zone, 3)} to {format_time(end, self.zone, 3)}"
        now = f"{format_time(new_start, self.zone, 3)} to {format_time(new_end, self.zone, 3)}"
        text = (
            f"From the viewer: {asked}. This page works in whole seconds, so it keeps {now}"
            if (new_start, new_end) != (start, end)
            else f"From the viewer: {asked}."
        )
        self.keep.viewer_note.setText(text)
        self.keep.viewer_note.setVisible(True)
        self.status.emit(text)
        return True

    def _leave_out_hidden(self, hidden) -> None:
        """Leave out the channels hidden in the viewer (only those this
        selection has; never all of them), and say so on the picker."""
        selection = self.selection
        names = [n for n in dict.fromkeys(hidden) if n in selection.rows]
        if not names or len(names) >= selection.total:
            return
        selection.set_kept(names, False)
        selection.clear_history()
        self.picker.set_selection(
            selection,
            len(self.keep.all_rows),
            f"From the viewer: the {format_count(len(names), 'channel')} hidden there "
            f"{'is' if len(names) == 1 else 'are'} left out. Change it here if you like; "
            'to keep only a few channels, choose "Exclude all" and tick the ones to keep.',
        )
        self._update_buttons()

    def restart(self) -> None:
        self.request = None
        self.result = None
        self.handle = None
        self.check_handle = None
        self._stopped = False
        self.new.passwords.forget()
        self.new.passwords.cleared_once = False
        self.new.name.clear()
        self.new._auto_name = ""
        self.new.save_folder.setChecked(True)
        self.privacy.reset()
        self._selection_keys = ()
        self.keep.set_recordings([])
        self.keep._layout_keys = None
        self.keep.viewer_note.setVisible(False)
        self.recordings.ticked.clear()
        self.recordings.lost.setVisible(False)
        self.go(STEP_RECORDINGS)
