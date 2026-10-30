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

The rules (what each choice keeps, overlaps, the temporary parts and their
password protection) live in ``model.join``; the widgets never import
mef3io. Recordings come from the Explore list (opened with their
passwords there): no recording is opened twice.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, tzinfo

from PySide6.QtCore import QDate, QDateTime, QRectF, QSize, Qt, QTime, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ..anonymizer.page import (
    BLOCKER_PLAN_WORDS,
    PasswordsStep,
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
from ..common.theme import LIGHT, Theme
from ..explorer.state import RecordingEntry
from ..model import join as join_model
from ..model.anonymize import (
    blocker_words,
    destination_for,
    inside_any_recording,
    name_problem,
)
from ..model.channels import ChannelSelection
from ..model.join import (
    KEEP_BETWEEN,
    KEEP_FIRST,
    KEEP_LAST,
    KEEP_WHOLE,
    SECOND_US,
    JoinPlan,
    JoinRequest,
    JoinResult,
    JoinSource,
    Keep,
    Layout,
    plan_join,
    problem_text,
)
from ..model.recording import ACCESS_BASIC
from ..model.shiftplan import Blocker
from ..model.timefmt import (
    format_count,
    format_span,
    format_time,
    from_local,
    resolve_zone,
    to_datetime,
)
from ..workers.jobs import JobManager
from ..workers.tasks import check_join, join_recordings

#: Wait this long after the last change of the Explore list before listing
#: the recordings again (a scan delivers them in quick bursts).
REFRESH_DELAY_MS = 150

# Channels come before "What to keep": two parts that overlap in time are
# fine when they share no kept channel, so the overlap rule needs the
# channel choice.
STEPS = ("Recordings", "Channels", "What to keep", "New recording", "Save")
STEP_RECORDINGS, STEP_CHANNELS, STEP_KEEP, STEP_NEW, STEP_RUN = range(len(STEPS))

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
    "Several recordings: the part of each that you keep is first saved, password-protected, "
    "in a hidden temporary folder next to the new recording, then the parts are joined into "
    "the new recording, which is checked against them. The temporary folder is always "
    "removed at the end, also when you stop or something goes wrong. The originals are "
    "never changed."
)

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


class KeptPartsPreview(QWidget):
    """One lane per recording on a shared time axis: the whole recording as
    a light track, the kept part filled in, overlapping parts in red. Gaps
    between recordings are the empty space between the lanes' bars."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[tuple[str, int, int, object, bool]] = []
        self.zone: tzinfo = UTC
        self.theme: Theme = LIGHT
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def _metrics(self):
        em = self.fontMetrics().height()
        row = round(em * 1.5)
        names = [self.fontMetrics().horizontalAdvance(r[0]) for r in self.rows] or [0]
        gutter = min(max(names) + em, round(em * 12))
        return em, row, gutter, round(em * 2.0)

    def sizeHint(self) -> QSize:  # noqa: N802
        em, row, _gutter, axis = self._metrics()
        return QSize(em * 30, axis + max(1, len(self.rows)) * row + em)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def set_rows(self, rows, zone: tzinfo) -> None:
        """``rows``: (name, recording start, recording end, kept Span or
        None, overlaps another kept part)."""
        self.rows = list(rows)
        self.zone = zone
        self.updateGeometry()
        self.setMinimumHeight(self.sizeHint().height())
        self.update()

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.rows:
            painter.end()
            return
        theme = self.theme
        em, row, gutter, axis = self._metrics()
        left, right = gutter, self.width() - em
        width = max(1.0, right - left)
        t0 = min(r[1] for r in self.rows)
        t1 = max(max(r[2] for r in self.rows), t0 + 1)

        def x_of(t: int) -> float:
            return left + width * (t - t0) / (t1 - t0)

        painter.setPen(QColor(theme.muted))
        painter.drawText(
            QRectF(left, 0, width / 2, em * 1.2),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            format_time(t0, self.zone),
        )
        painter.drawText(
            QRectF(left + width / 2, 0, width / 2, em * 1.2),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            format_time(t1, self.zone),
        )
        pen = QPen(QColor(theme.border))
        painter.setPen(pen)
        base = em * 1.5
        painter.drawLine(round(left), round(base), round(right), round(base))
        for i, (name, start, end, span, overlap) in enumerate(self.rows):
            y = axis + i * row
            painter.setPen(QColor(theme.text))
            painter.drawText(
                QRectF(0, y, gutter - em * 0.5, row),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                self.fontMetrics().elidedText(
                    name, Qt.TextElideMode.ElideRight, int(gutter - em * 0.5)
                ),
            )
            painter.setPen(Qt.PenStyle.NoPen)
            x0, x1 = x_of(start), x_of(end)
            painter.setBrush(QColor(theme.divider))
            painter.drawRoundedRect(QRectF(x0, y + row * 0.3, max(1.5, x1 - x0), row * 0.4), 3, 3)
            if span is not None:
                k0, k1 = x_of(span.start), x_of(span.end)
                painter.setBrush(QColor(theme.danger if overlap else theme.accent))
                painter.drawRect(QRectF(k0, y + row * 0.15, max(2.0, k1 - k0), row * 0.7))
        painter.end()


class KeepStep(QWidget):
    changed = Signal()

    COLUMNS = ("Recording", "Start", "End", "Keep", "Kept")

    def __init__(self, page: JoinerPage):
        super().__init__(page)
        self.page = page
        self.rows: list[KeepRow] = []
        self.plan = JoinPlan()
        self.zone_note = _label(muted=True)
        #: What the signal viewer asked for and what this page holds instead.
        self.viewer_note = _label(muted=True)
        self.viewer_note.setVisible(False)
        self.table = _table(self.COLUMNS)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.preview = KeptPartsPreview()
        self.summary = _label(bold=True)
        self.problems = _label()
        self.problems.setObjectName("Bad")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(
            _label(
                "Choose what to keep of each recording. They are listed in time order, and "
                "the new recording holds the kept parts in that order, with the gaps between "
                "them as they really were. Parts may run at the same time (side by side) only "
                "when they hold different channels: two parts that overlap in time must not "
                "share a channel you keep.",
                muted=True,
            )
        )
        layout.addWidget(self.zone_note)
        layout.addWidget(self.viewer_note)
        layout.addWidget(self.table, 1)
        layout.addWidget(_label("Preview of the kept parts", bold=True))
        layout.addWidget(self.preview)
        layout.addWidget(self.summary)
        layout.addWidget(self.problems)

    def set_recordings(self, entries: list[RecordingEntry]) -> None:
        """List ``entries`` in time order, keeping each one's choice."""
        states = {r.entry.key: r.state() for r in self.rows}
        entries = sorted(entries, key=lambda e: (e.details.start, e.details.end, e.key))
        self.table.setRowCount(0)  # deletes the old rows' widgets
        zone = self.page.zone
        self.rows = []
        for e in entries:
            row = KeepRow(e, zone, states.get(e.key))
            row.changed.connect(self.recompute)
            self.rows.append(row)
        rows = self.rows
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
        self.recompute()

    def set_zone(self) -> None:
        """Show the same choices in the page's (new) time zone."""
        if self.rows:
            self.set_recordings([r.entry for r in self.rows])

    def sources(self) -> list[JoinSource]:
        return [
            JoinSource(r.entry.key, r.entry.location, r.keep(), r.entry.password) for r in self.rows
        ]

    def layouts(self) -> dict:
        return {
            r.entry.key: Layout(
                r.entry.details.start,
                r.entry.details.end,
                tuple(c.name for c in r.entry.details.channels),
            )
            for r in self.rows
        }

    def recompute(self, *_args) -> None:
        self.plan = plan_join(
            self.sources(), self.layouts(), excluded=self.page.selection.excluded, changes=True
        )
        names = {r.entry.key: r.entry.name for r in self.rows}
        overlapping = self.plan.overlapping
        preview = []
        total = 0.0
        for i, row in enumerate(self.rows):
            key = row.entry.key
            span = self.plan.spans.get(key)
            problem = self.plan.problem_of(key)
            if problem is not None:
                text = problem_text(problem, names)
            elif span is not None:
                total += span.seconds
                text = ("All of it" if span.whole else format_span(span.seconds)) + (
                    " (overlaps)" if key in overlapping else ""
                )
            else:
                text = ""
            item = self.table.item(i, 4)
            if item is not None:
                item.setText(text)
            details = row.entry.details
            preview.append((row.entry.name, details.start, details.end, span, key in overlapping))
        self.preview.set_rows(preview, self.page.zone)
        self.table.resizeColumnToContents(4)
        self.summary.setText(
            f"The new recording will hold about {format_span(total)} of signal."
            if self.rows and total
            else ""
        )
        words = list(dict.fromkeys(problem_text(p, names) for p in self.plan.problems))
        self.problems.setText("\n".join(words))
        self.problems.setVisible(bool(words))
        self.changed.emit()

    @property
    def ok(self) -> bool:
        return bool(self.rows) and self.plan.ok


def zone_words(zone: tzinfo) -> str:
    return "UTC" if zone is UTC else f"the site time zone ({getattr(zone, 'key', zone)})"


# --------------------------------------------------------------------------
# Step 4: the new recording


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
        form = QFormLayout()
        form.addRow("Name of the new recording:", self.name)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_label, 1)
        folder_row.addWidget(self.folder_button)
        form.addRow("Saved in:", folder_row)
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
        layout.addWidget(_label("Password protection", bold=True))
        layout.addWidget(self.protected_note)
        layout.addWidget(self.passwords)
        layout.addWidget(self.how)
        layout.addWidget(self.problem)
        layout.addLayout(self.findings)
        layout.addStretch(1)

    def prepare(self) -> None:
        """Fill in a name and folder the first time (from the first
        recording), and say what applies to these recordings."""
        rows = self.page.keep.rows
        if not rows:
            return
        first = rows[0].entry
        if not self.folder:
            self.folder = os.path.dirname(first.location)
        if not self.name.text():
            suffix = "joined" if len(rows) > 1 else "part"
            self.name.setText(f"{first.name}-{suffix}")
        self.protected_note.setVisible(
            any(r.entry.details.password_protected for r in rows)
            and not self.passwords.both.isChecked()
        )
        self.how.setVisible(len(rows) > 1)
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
        return destination_for(self.folder, self.name.text().strip())

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
        if os.path.lexists(dst + ".partial"):
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

    def start(self, handle, several: bool) -> None:
        _clear(self.progress_slot)
        _clear(self.details_slot)
        self.result.setText("")
        self.show_buttons(False)
        self.heading.setText(
            "Joining the recordings…" if several else "Saving the part of the recording…"
        )
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
            kept = format_count(len(result.channels_kept), "channel")
            self.result.setText(
                f"The new recording was saved with {kept} and checked against the originals, "
                "which were not changed." + left
            )
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
        self.new = NewRecordingStep(self)
        self.run_step = RunStep(self)
        self.stack = QStackedWidget(self)
        for widget in (self.recordings, self.picker, self.keep, self.new, self.run_step):
            self.stack.addWidget(widget)
        for step in (self.recordings, self.keep, self.picker, self.new):
            step.changed.connect(self._inputs_changed)
        self.keep.preview.set_theme(self.theme)

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
            self.keep.preview.set_theme(theme)

    def set_zone_name(self, name: str) -> None:
        self.zone = resolve_zone(name)
        self._zone_name = name
        if self._built:
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
            for r in self.keep.rows
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
            self.keep.recompute()
        elif step == STEP_NEW:
            self.new.prepare()
        passwords = self.new.passwords
        passwords.cleared.setVisible(step == STEP_NEW and passwords.cleared_once)
        self._update_buttons()

    def _prepare_channels(self) -> None:
        entries = [r.entry for r in self.keep.rows]
        keys = tuple(e.key for e in entries)
        if keys == self._selection_keys:
            return
        previous = self.selection if self._selection_keys else None
        self.selection, notice = channel_selection_for(entries, previous)
        self._selection_keys = keys
        self.picker.set_selection(self.selection, len(entries), notice)

    def has_changes(self) -> bool:
        """A channel is left out, or new passwords are chosen."""
        return bool(self.selection.excluded) or self.new.passwords.both.isChecked()

    def _blocker_text(self) -> str:
        step = self.step
        if step == STEP_RECORDINGS:
            return "" if self.recordings.ticked else "Tick at least one recording."
        if step == STEP_KEEP:
            return "" if self.keep.ok else "Fix what is listed above first."
        if step == STEP_CHANNELS:
            return "" if self.selection.can_continue else "Keep at least one channel."
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
        )

    def _names(self) -> dict[str, str]:
        return {r.entry.key: r.entry.name for r in self.keep.rows}

    def check_and_save(self):
        """mef3io's checks (nothing written), then, if nothing stops it, the
        run itself."""
        if not self._still_readable():
            return None
        request = self.make_request()
        self.new.set_findings([])
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
        handle = self.jobs.submit(title, join_recordings, request)
        self.handle = handle
        handle.succeeded.connect(self._on_result)
        handle.cancelled.connect(self._on_stopped)
        handle.failed.connect(self._on_failed)
        handle.finished.connect(self._on_finished)
        self.go(STEP_RUN)
        self.run_step.start(handle, several)
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

    def choose_window(self, keys, start: int, end: int) -> bool:
        """Start over with exactly these recordings and keep ``[start, end)``
        of each (the signal viewer's "Send to Join or trim"), stopping at the
        "What to keep" step so the window can be checked. The page works in
        whole seconds: the start is rounded down and the end up, so the part
        asked for is always inside what is kept. False when it could not."""
        if not self.choose(keys) or self.step != STEP_CHANNELS:
            return False
        self.next()  # the channels step has nothing to decide here
        if self.step != STEP_KEEP:
            return False
        end_up = -(-end // SECOND_US) * SECOND_US
        for row in self.keep.rows:
            row.set_how(KEEP_BETWEEN)
            row.start.set_us(max(start, row.entry.details.start), self.zone)
            row.end.set_us(min(end_up, row.entry.details.end), self.zone)
        self.keep.recompute()
        # What is kept now (a recording ending mid-second is kept to its end).
        kept = [r.keep() for r in self.keep.rows]
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

    def restart(self) -> None:
        self.request = None
        self.result = None
        self.handle = None
        self.check_handle = None
        self._stopped = False
        self.new.passwords.forget()
        self.new.passwords.cleared_once = False
        self.new.name.clear()
        self._selection_keys = ()
        self.keep.set_recordings([])
        self.keep.viewer_note.setVisible(False)
        self.recordings.ticked.clear()
        self.recordings.lost.setVisible(False)
        self.go(STEP_RECORDINGS)
