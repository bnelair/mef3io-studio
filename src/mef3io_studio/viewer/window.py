"""The signal viewer window: two toolbar rows, the overview strip, the traces
and a status line (docs/design/viewer.md, "Layout").

Part 1 builds the window, the data flow and the navigation. Places for what
comes next are left as hooks: ``band_slot`` (recording band), ``chips_slot``
(note chips), ``side_panel`` (Recordings / notes tabs), ``features`` (the
feature names whose keys and buttons are live), ``handlers`` (action name ->
function; the key table dispatches through it) and ``context_hooks`` (extra
right-click entries).
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, tzinfo

import numpy as np
from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..common.theme import LIGHT, Theme
from ..model import dsp, montage, timefmt
from ..model import viewer_view as vv
from ..model.viewer_view import ViewState
from ..workers.viewer_pipeline import EnvelopePiece
from . import jump as jumping
from .canvas import DrawRow, TraceCanvas
from .controller import ViewController
from .dialogs import JumpDialog
from .filters import FilterPanel, ResponseDialog
from .keymap import KEYMAP, NAVIGATION_ACTIONS, TIMEBASE_ACTIONS, HelpOverlay, entry_for
from .montage_bar import ChannelChooser, MontageBar
from .strip import OverviewStrip

PROFILE_ORDER = tuple(vv.PROFILE_NAMES)


def default_state(settings, timeline_start: int) -> ViewState:
    """The starting view: the saved defaults, else the clinical ones."""
    sens = vv.nearest_step(vv.SENS_STEPS, settings.get("viewer/sensitivity") if settings else 100)
    tb = vv.nearest_step(vv.TIMEBASE_STEPS, settings.get("viewer/timebase") if settings else 30)
    sp = vv.nearest_step(vv.SPACING_STEPS, settings.get("viewer/spacing") if settings else 7)
    profile = settings.get("viewer/profile") if settings else "auto"
    filters = dsp.FilterSettings()
    if settings:
        try:
            raw = json.loads(settings.get("viewer/filters") or "{}")
            filters = dsp.FilterSettings(**raw) if raw else filters
        except (ValueError, TypeError):
            filters = dsp.FilterSettings()
    return ViewState(
        t0=timeline_start,
        sensitivity=sens,
        timebase=tb,
        spacing=sp,
        profile=profile,
        filters=filters,
    )


def performance_from(settings) -> vv.Performance:
    """The performance settings (0 = automatic), kept inside their ranges."""
    if settings is None:
        return vv.Performance()
    perf = vv.Performance(
        settings.get("viewer/load_workers"),
        settings.get("viewer/decode_threads"),
        settings.get("viewer/filter_workers"),
        settings.get("viewer/lookahead_pages"),
        settings.get("viewer/memory_mb"),
    )
    return vv.clean_performance(perf, os.cpu_count() or 1)


class ViewerWindow(QMainWindow):
    """Shows ``data`` (a ``ViewerData``) in a window of its own."""

    closed = Signal()
    #: Hooks for what comes next: (what, time in µs). what = "start" | "end".
    export_edge = Signal(str, object)
    #: The page changed: (start, end) in µs.
    page_changed = Signal(object, object)

    def __init__(
        self,
        data,
        settings=None,
        theme: Theme = LIGHT,
        zone: tzinfo = UTC,
        reveal: Callable[[int], bool] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.data = data
        self.settings = settings
        self.theme = theme
        self.zone = zone
        self._reveal = reveal or (lambda _i: False)
        self.features: set[str] = set()
        self.handlers: dict[str, Callable[[], None]] = {}
        self.context_hooks: list[Callable[[QMenu, int, int], None]] = []
        self.state = default_state(settings, data.timeline_start)
        self.multi = len(data.recordings) > 1
        self.last_key_text = ""
        self._pointer_t: int | None = None
        self._pointer_row: int | None = None
        self._row_cache: dict[tuple, list[montage.Row]] = {}
        self._direction = 1
        self._ready = False
        self._pan_base: int | None = None
        self._closed = False
        self._rate_of = data.rate
        self._channels = list(data.channels)
        self._rates = sorted({float(data.rate(c)) for c in self._channels}) or [256.0]
        self.perf = performance_from(settings)
        self.controller = ViewController(data, self.perf, parent=self)
        self._chosen: set[str] = set()
        self._last_row: int | None = None
        self.controller.pieces_changed.connect(self._apply_pieces)
        self.controller.loading_changed.connect(lambda on: self.canvas.set_loading(on))
        self.controller.failed.connect(self._failed)
        self.controller.strip_envelope.connect(lambda env: self.strip.set_envelope(env))
        self.setWindowFlag(Qt.WindowType.Window, True)
        em = self.fontMetrics().height()
        self.resize(em * 80, em * 44)
        self.setMinimumSize(em * 36, em * 22)

        palette = vv.resolve_profile(self.state.profile, theme.dark)
        self.canvas = TraceCanvas(palette, self)
        self.canvas.zone = zone
        self.strip = OverviewStrip(palette, self)
        self.strip.zone = zone
        self.band_slot = QWidget(self)  # hook: the recording band
        self.band_slot.setVisible(False)
        self.chips_slot = QWidget(self)  # hook: the note chips row
        self.chips_slot.setVisible(False)
        self.side_panel = QTabWidget(self)  # hook: Recordings / notes tabs
        self.side_panel.setVisible(False)
        self.help = HelpOverlay(self.canvas, frozenset(self.features))

        left = QWidget(self)
        box = QVBoxLayout(left)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        for w in (self.strip, self.band_slot, self.chips_slot):
            box.addWidget(w)
        box.addWidget(self.canvas, 1)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(left)
        self.splitter.addWidget(self.side_panel)
        self.splitter.setStretchFactor(0, 1)
        self.setCentralWidget(self.splitter)

        self.filter_panel = FilterPanel(self.state.filters, self._rates, self)
        self.channel_chooser = ChannelChooser(self)
        self.channel_chooser.set_channels(self._channels, self.state.hidden)
        self.jump_dialog = JumpDialog(self)
        self.response_dialog: ResponseDialog | None = None
        self._build_toolbars()
        self._build_status()
        self._connect()
        self.montage_bar.set_channels(self._channels)
        self.montage_bar.set_state(self.state.montage, self._default_reference())
        self._apply_profile()
        self.strip.set_timeline(data.timeline_start, data.timeline_end, data.segments_coverage())
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(120)
        self._debounce.timeout.connect(self._refresh)
        self.canvas.setFocus()
        self._install_handlers()
        self._update_title()

    # -- construction ----------

    def _build_toolbars(self) -> None:
        def tool(text: str, tip: str, slot, visible: bool = True) -> QToolButton:
            b = QToolButton(self)
            b.setText(text)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            b.clicked.connect(lambda _c=False: (slot(), self.canvas.setFocus()))
            b.setVisible(visible)
            return b

        row1 = QToolBar("Navigation", self)
        row1.setMovable(False)
        self.prev_button = tool(
            "◀ Previous page", "Go back one page (Left arrow)", lambda: self.page(-1)
        )
        self.next_button = tool(
            "Next page ▶", "Go forward one page (Right arrow)", lambda: self.page(1)
        )
        self.day_box = QSpinBox(self)
        self.day_box.setPrefix("Day ")
        self.day_box.setMinimum(1)
        self.day_box.setMaximum(max(1, self._days()))
        self.day_box.setToolTip("The day of the recording")
        self.time_edit = QLineEdit(self)
        self.time_edit.setMaximumWidth(self.fontMetrics().height() * 7)
        self.time_edit.setInputMask("99:99:99")
        self.time_edit.setToolTip("Time of day (hours:minutes:seconds) in the site time zone")
        self.go_button = tool("Go", "Go to this day and time", self._go_clicked)
        self.jump_button = tool(
            "Jump…", "Jump to a time, a move or a recording (Ctrl+K)", self.open_jump
        )
        self.edit_button = tool("Edit mode", "Add notes (not available yet)", lambda: None, False)
        press = self.press
        self.export_start_button = tool(
            "Export start",
            "Set the start of the part to save",
            lambda: press("export_start"),
            False,
        )
        self.export_end_button = tool(
            "Export end", "Set the end of the part to save", lambda: press("export_end"), False
        )
        self.notes_button = tool(
            "Notes and events", "List of notes and events", lambda: press("note_list"), False
        )
        self.help_button = tool("?", "Keys and mouse (?)", self.toggle_help)
        for w in (
            self.prev_button,
            self.next_button,
            self.day_box,
            self.time_edit,
            self.go_button,
            self.jump_button,
        ):
            row1.addWidget(w)
        row1.addSeparator()
        for w in (
            self.notes_button,
            self.edit_button,
            self.export_start_button,
            self.export_end_button,
        ):
            row1.addWidget(w)
        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row1.addWidget(spacer)
        row1.addWidget(self.help_button)
        self.addToolBar(row1)
        self.addToolBarBreak()

        row2 = QToolBar("Display", self)
        row2.setMovable(False)
        self.montage_bar = MontageBar(self)
        self.channels_button = tool(
            "Channels", "Choose the channels to show (C)", self.toggle_channels
        )
        self.spacing_box = self._steps_box(vv.SPACING_STEPS, "{:g} mm", "Distance between rows")
        self.sens_box = self._steps_box(vv.SENS_STEPS, "{:g} µV/mm", "Sensitivity")
        self.auto_button = tool(
            "Auto", "Choose the sensitivity that fits the signals", self.auto_sensitivity
        )
        self.timebase_box = self._steps_box(vv.TIMEBASE_STEPS, "{:g} mm/s", "Timebase")
        self.filters_button = tool("No filters", "Filters (F)", self.toggle_filters)
        self.profile_box = QComboBox(self)
        for key in PROFILE_ORDER:
            self.profile_box.addItem(vv.PROFILE_NAMES[key], key)
        self.profile_box.setToolTip("Colours")
        self.types_button = tool(
            "Types", "Types of notes and events (T)", lambda: press("types"), False
        )
        row2.addWidget(self.montage_bar)
        row2.addWidget(self.channels_button)
        for label, w in (("Spacing", self.spacing_box), ("Sensitivity", self.sens_box)):
            row2.addWidget(QLabel(label, self))
            row2.addWidget(w)
        row2.addWidget(self.auto_button)
        row2.addWidget(QLabel("Timebase", self))
        row2.addWidget(self.timebase_box)
        row2.addWidget(self.filters_button)
        row2.addWidget(self.profile_box)
        row2.addWidget(self.types_button)
        self.addToolBar(row2)
        self._sync_boxes()

    def _steps_box(self, steps, fmt: str, tip: str) -> QComboBox:
        box = QComboBox(self)
        for s in steps:
            box.addItem(fmt.format(s), s)
        box.setToolTip(tip)
        box.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        return box

    def _build_status(self) -> None:
        bar = self.statusBar()
        self.status_time = QLabel("", self)
        self.status_recording = QLabel("", self)
        self.status_mode = QLabel("", self)
        self.status_message = QLabel("", self)
        for w in (self.status_time, self.status_recording):
            bar.addWidget(w)
        bar.addWidget(self.status_message, 1)
        bar.addPermanentWidget(self.status_mode)

    def _connect(self) -> None:
        c = self.canvas
        c.size_changed.connect(self._schedule)
        c.pointer_changed.connect(self._pointer)
        c.wheel_time.connect(lambda n: self.move_by(-n * self.page_us() / 10, direction=-n))
        c.wheel_rows.connect(lambda n: self.scroll_rows(-1 if n > 0 else 1))
        c.wheel_zoom.connect(self.zoom_at)
        c.panned.connect(self._panned)
        c.pan_finished.connect(self._pan_done)
        c.centre_requested.connect(self.centre_at)
        c.context_requested.connect(self._context_menu)
        self.strip.navigate.connect(self.centre_at)
        self.montage_bar.changed.connect(self._montage_chosen)
        self.channel_chooser.hidden_changed.connect(lambda h: self.set_hidden(h))
        self.spacing_box.activated.connect(lambda i: self._box_chose("spacing", self.spacing_box))
        self.sens_box.activated.connect(lambda i: self._box_chose("sensitivity", self.sens_box))
        self.timebase_box.activated.connect(
            lambda i: self._box_chose("timebase", self.timebase_box)
        )
        self.profile_box.activated.connect(self._profile_chosen)
        self.filter_panel.changed.connect(self._filters_chosen)
        self.filter_panel.response_requested.connect(self.show_response)
        self.jump_dialog.jump_requested.connect(self._jump_requested)
        self.time_edit.returnPressed.connect(self._go_clicked)
        self.day_box.lineEdit().returnPressed.connect(self._go_clicked)
        self.time_edit.installEventFilter(self)
        self.day_box.installEventFilter(self)
        self.day_box.lineEdit().installEventFilter(self)

    # -- keys ----------

    def _install_handlers(self) -> None:
        h = self.handlers
        h["page_forward"] = lambda: self.page(1)
        h["page_back"] = lambda: self.page(-1)
        h["half_forward"] = lambda: self.page(0.5)
        h["half_back"] = lambda: self.page(-0.5)
        h["second_forward"] = lambda: self.move_by(1_000_000, 1)
        h["second_back"] = lambda: self.move_by(-1_000_000, -1)
        h["start"] = lambda: self.go_to_edge(False)
        h["end"] = lambda: self.go_to_edge(True)
        h["sens_up"] = lambda: self.step_sensitivity(-1)
        h["sens_down"] = lambda: self.step_sensitivity(1)
        h["spacing_up"] = lambda: self.step_spacing(1)
        h["spacing_down"] = lambda: self.step_spacing(-1)
        h["rows_up"] = lambda: self.scroll_rows(-self.canvas.visible_rows())
        h["rows_down"] = lambda: self.scroll_rows(self.canvas.visible_rows())
        for name, value in TIMEBASE_ACTIONS.items():
            h[name] = lambda v=value: self.set_timebase(v)
        h["boundary_back"] = lambda: self.go_to_boundary(-1)
        h["boundary_forward"] = lambda: self.go_to_boundary(1)
        h["goto"] = lambda: (self.time_edit.setFocus(), self.time_edit.selectAll())
        h["jump"] = self.open_jump
        h["channels"] = self.toggle_channels
        h["montage"] = self.next_montage
        h["filters"] = self.toggle_filters
        h["escape"] = self.escape
        h["help"] = self.toggle_help
        h["export_start"] = lambda: self._export_edge("start")
        h["export_end"] = lambda: self._export_edge("end")

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
            and obj in (self.time_edit, self.day_box, self.day_box.lineEdit())
        ):
            self.canvas.setFocus()
            return True
        return super().eventFilter(obj, event)

    def press(self, action: str) -> bool:
        """Run a key-table action (as its key would). False when it is not
        available in this window."""
        entry = next((e for e in KEYMAP if e.action == action), None)
        if entry is None or (entry.feature and entry.feature not in self.features):
            return False
        fn = self.handlers.get(action)
        if fn is None:
            return False
        fn()
        return True

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        entry = entry_for(event)
        if entry is None:
            super().keyPressEvent(event)
            return
        if event.isAutoRepeat() and entry.action not in NAVIGATION_ACTIONS:
            event.accept()
            return
        self.last_key_text = event.text()
        if self.press(entry.action):
            event.accept()
        else:
            super().keyPressEvent(event)

    # -- geometry and the page ----------

    def page_us(self) -> int:
        return vv.page_us(self.canvas.plot_width, self.canvas.px_per_mm, self.state.timebase)

    def overview(self) -> bool:
        return vv.is_overview(self.page_us())

    def _days(self) -> int:
        a = timefmt.to_datetime(self.data.timeline_start, self.zone).date()
        b = timefmt.to_datetime(self.data.timeline_end, self.zone).date()
        return (b - a).days + 1

    def _schedule(self) -> None:
        self._debounce.start()

    def set_state(self, **changes) -> None:
        self.state = self.state.with_(**changes)
        self._refresh()

    def move_by(self, delta_us: float, direction: int = 1) -> None:
        self._direction = 1 if direction >= 0 else -1
        self.set_state(t0=int(self.state.t0 + delta_us))

    def page(self, fraction: float) -> None:
        self.move_by(fraction * self.page_us(), 1 if fraction > 0 else -1)

    def go_to_edge(self, end: bool) -> None:
        length = self.page_us()
        self._direction = 1 if end else -1
        self.set_state(t0=self.data.timeline_end - length if end else self.data.timeline_start)

    def centre_at(self, t: int) -> None:
        self.set_state(t0=int(t - self.page_us() / 2))

    def go_to(self, t: int) -> None:
        self.set_state(t0=int(t))

    def scroll_rows(self, delta: int) -> None:
        self.set_state(row_offset=self.state.row_offset + delta)

    def _panned(self, dt_us: float, rows: int) -> None:
        """A drag: ``dt_us`` is the time moved since the press, ``rows`` the
        first row wanted."""
        if self._pan_base is None:
            self._pan_base = self.state.t0
        self._direction = 1 if dt_us >= 0 else -1
        self.set_state(t0=int(self._pan_base + dt_us), row_offset=rows)

    def _pan_done(self) -> None:
        self._pan_base = None

    def zoom_at(self, notches: float, frac: float) -> None:
        old = self.page_us()
        anchor = self.state.t0 + frac * old
        tb = vv.step_value(vv.TIMEBASE_STEPS, self.state.timebase, 1 if notches > 0 else -1)
        self.state = self.state.with_(timebase=tb)
        new = self.page_us()
        self.set_state(t0=int(anchor - frac * new))

    def set_timebase(self, value: float) -> None:
        self._chosen.add("timebase")
        self.set_state(timebase=vv.nearest_step(vv.TIMEBASE_STEPS, value))

    def step_sensitivity(self, delta: int) -> None:
        self._chosen.add("sensitivity")
        self.set_state(sensitivity=vv.step_value(vv.SENS_STEPS, self.state.sensitivity, delta))

    def step_spacing(self, delta: int) -> None:
        self._chosen.add("spacing")
        self.set_state(spacing=vv.step_value(vv.SPACING_STEPS, self.state.spacing, delta))

    def go_to_boundary(self, direction: int) -> None:
        centre = self.state.t0 + self.page_us() / 2
        times = sorted({b.time_us for b in self.data.boundaries()})
        eps = max(1.0, self.page_us() * 1e-3)
        pick = [t for t in times if (t > centre + eps if direction > 0 else t < centre - eps)]
        if not pick:
            self.status_message.setText("There is no further boundary between recordings.")
            return
        self._direction = direction
        self.centre_at(pick[0] if direction > 0 else pick[-1])

    # -- montage, channels, filters ----------------------------------------------------------------

    def _default_reference(self) -> str | None:
        for name in self._channels:
            if name.casefold() == "cz":
                return name
        return self._channels[0] if self._channels else None

    def next_montage(self) -> None:
        order = vv.MONTAGES
        nxt = order[(order.index(self.state.montage) + 1) % len(order)]
        ref = (
            self.state.reference or self._default_reference()
            if nxt == vv.MONTAGE_REFERENCE
            else None
        )
        self.montage_bar.set_state(nxt, ref)
        self.set_state(montage=nxt, reference=ref)

    def _montage_chosen(self, key: str, ref) -> None:
        self.set_state(montage=key, reference=ref)
        self.canvas.setFocus()

    def use_as_reference(self, channel: str) -> None:
        self.montage_bar.set_state(vv.MONTAGE_REFERENCE, channel)
        self.set_state(montage=vv.MONTAGE_REFERENCE, reference=channel)

    def set_hidden(self, hidden) -> None:
        hidden = frozenset(hidden)
        self.channel_chooser.set_channels(self._channels, hidden)
        self.set_state(hidden=hidden, row_offset=0)

    def _lead_members(self, channel: str) -> set[str]:
        lead = montage.lead_and_contact(channel)[0]
        return {c for c in self._channels if montage.lead_and_contact(c)[0] == lead}

    def hide_channel(self, channel: str) -> None:
        self.set_hidden(self.state.hidden | {channel})

    def hide_lead(self, channel: str) -> None:
        self.set_hidden(self.state.hidden | self._lead_members(channel))

    def show_only_lead(self, channel: str) -> None:
        keep = self._lead_members(channel)
        self.set_hidden(frozenset(c for c in self._channels if c not in keep))

    def toggle_channels(self) -> None:
        self._toggle_tool(self.channel_chooser, self.channels_button)

    def toggle_filters(self) -> None:
        self._toggle_tool(self.filter_panel, self.filters_button)

    def _toggle_tool(self, panel: QWidget, anchor: QWidget) -> None:
        if panel.isVisible():
            panel.hide()
            self.canvas.setFocus()
            return
        panel.show()
        panel.move(anchor.mapToGlobal(QPoint(0, anchor.height())))
        panel.raise_()

    def _filters_chosen(self, settings: dsp.FilterSettings) -> None:
        self._chosen.add("filters")
        self.set_state(filters=settings)
        if self.response_dialog is not None and self.response_dialog.isVisible():
            self.response_dialog.refresh(settings)

    def show_response(self) -> None:
        rate = None
        row = self._last_row
        if row is not None and row < len(self.canvas.rows):
            rate = float(self._rate_of(self.canvas.rows[row].channel))
        dlg = self.response_dialog
        if dlg is None or not dlg.isVisible():
            self.response_dialog = dlg = ResponseDialog(self.state.filters, self._rates, rate, self)
        else:
            dlg.refresh(self.state.filters)
            if rate is not None:
                dlg.set_rate(rate)
        dlg.show()
        dlg.raise_()

    def _profile_chosen(self, _i: int) -> None:
        self._chosen.add("profile")
        self.set_state(profile=self.profile_box.currentData())
        self.canvas.setFocus()

    def _box_chose(self, field: str, box: QComboBox) -> None:
        self._chosen.add(field)
        self.set_state(**{field: box.currentData()})
        self.canvas.setFocus()

    def auto_sensitivity(self) -> None:
        first = self.state.row_offset
        last = first + self.canvas.visible_rows()
        peaks = []
        for r in self.canvas.rows[first:last]:
            if r.piece is None or isinstance(r.piece, EnvelopePiece):
                continue
            w = r.piece.window(r.local, self.state.t0, self.state.t0 + self.page_us())
            w = w[np.isfinite(w)]
            if w.size:
                peaks.append(float(np.percentile(np.abs(w - np.median(w)), 98)))
        if not peaks:
            return
        wanted = 2 * float(np.median(peaks)) / (0.9 * self.state.spacing)
        larger = [s for s in vv.SENS_STEPS if s >= wanted]
        self.set_state(sensitivity=larger[0] if larger else vv.SENS_STEPS[-1])

    # -- jump ----------

    def _letters(self) -> dict[str, int]:
        return {r.letter: r.start_us for r in self.data.recordings}

    def open_jump(self) -> None:
        self.jump_dialog.show_error("")
        self.jump_dialog.show()
        self.jump_dialog.edit.setFocus()
        self.jump_dialog.edit.selectAll()

    def parse_jump(self, text: str, previous: bool = False) -> int:
        return jumping.parse(
            text,
            current_us=self.state.t0,
            timeline=(self.data.timeline_start, self.data.timeline_end),
            zone=self.zone,
            letters=self._letters(),
            previous=previous,
        )

    def _jump_requested(self, text: str, previous: bool) -> None:
        try:
            t = self.parse_jump(text, previous)
        except jumping.JumpError as exc:
            self.jump_dialog.show_error(str(exc))
            return
        self.jump_dialog.hide()
        self._direction = 1 if t >= self.state.t0 else -1
        self.go_to(t)
        self.canvas.setFocus()

    def _go_clicked(self) -> None:
        text = self.time_edit.text().strip()
        try:
            t = self.parse_jump(f"day {self.day_box.value()} {text}")
        except jumping.JumpError as exc:
            self.status_message.setText(str(exc))
            return
        self.go_to(t)
        self.canvas.setFocus()

    # -- the refresh ----------

    def _rows_for(self, montage_key: str, ref: str | None) -> list[montage.Row]:
        key = (montage_key, ref)
        rows = self._row_cache.get(key)
        if rows is None:
            if montage_key == vv.MONTAGE_REFERENCE and ref in self._channels:
                rows = montage.referenced(self._channels, ref, self._rate_of)
            elif montage_key == vv.MONTAGE_BIPOLAR:
                rows = montage.lead_bipolar(self._channels, self._rate_of)
            else:
                rows = montage.as_recorded(self._channels)
            self._row_cache[key] = rows
        return rows

    def current_rows(self) -> list[montage.Row]:
        overview = self.overview()
        key = vv.MONTAGE_RECORDED if overview else self.state.montage
        rows = self._rows_for(key, self.state.reference)
        return [r for r in rows if r.plus not in self.state.hidden]

    def _refresh(self) -> None:
        if self._closed or self.canvas.width() < 10:
            return
        length = self.page_us()
        t0 = vv.clamp_start(self.state.t0, length, self.data.timeline_start, self.data.timeline_end)
        rows = self.current_rows()
        visible = self.canvas.visible_rows()
        offset = max(0, min(self.state.row_offset, max(0, len(rows) - visible)))
        if (t0, offset) != (self.state.t0, self.state.row_offset):
            self.state = self.state.with_(t0=t0, row_offset=offset)
        overview = vv.is_overview(length)
        s = self.state
        self.canvas.overview_text = (
            "Overview: the lowest and highest value of each stretch" if overview else ""
        )
        self.canvas.set_view(t0, t0 + length, s.sensitivity, s.spacing, offset)
        self.canvas.multi = self.multi
        self.strip.set_page(t0, t0 + length)
        self._set_rows_loading(rows)
        self.controller.show(
            rows,
            (t0, t0 + length),
            s.filters,
            self.canvas.plot_width,
            (offset, min(len(rows), offset + visible + 1)),
            overview=overview,
            direction=self._direction,
        )
        self._apply_pieces()
        self._sync_boxes()
        self._sync_toolbar_time()
        self._update_status_mode()
        self.filter_panel.set_notice(self._notice())
        self._ready = True
        self.page_changed.emit(t0, t0 + length)

    def _set_rows_loading(self, rows) -> None:
        self.canvas.set_rows(self._draw_rows(rows, [None] * len(rows)), self.overview(), self.multi)

    def _apply_pieces(self) -> None:
        if self._closed:
            return
        rows = self.current_rows()
        pieces = self.controller.current_pieces()
        if len(pieces) != len(rows):
            return
        self.canvas.set_rows(self._draw_rows(rows, pieces), self.overview(), self.multi)
        self.canvas.set_loading(self.controller.loading)

    def _draw_rows(self, rows, pieces) -> list[DrawRow]:
        data = self.data
        t0, t1 = self.state.t0, self.state.t0 + self.page_us()
        out = []
        for row, pc in zip(rows, pieces, strict=True):
            tag = ""
            if self.multi:
                letters = sorted(
                    {
                        data.recordings[i].letter
                        for a, b, i in data.ownership(row.plus)
                        if a < t1 and b > t0
                    }
                )
                tag = ",".join(letters)
            out.append(
                DrawRow(
                    row.label,
                    row.plus,
                    None if pc is None else pc[0],
                    0 if pc is None else pc[1],
                    data.units(row.plus),
                    tag,
                    row.note,
                )
            )
        return out

    def _notice(self) -> str:
        s = self.state.filters
        if s.is_off:
            return ""
        capped = any(dsp.settling_samples(s, r)[1] for r in self._rates)
        return (
            "The edges of the page may not be fully settled with these filters." if capped else ""
        )

    def _failed(self, text: str) -> None:
        self.status_message.setText(text)

    # -- toolbar sync ----------

    def _sync_boxes(self) -> None:
        s = self.state
        for box, value in (
            (self.spacing_box, s.spacing),
            (self.sens_box, s.sensitivity),
            (self.timebase_box, s.timebase),
        ):
            i = box.findData(value)
            if i >= 0 and box.currentIndex() != i:
                box.setCurrentIndex(i)
        self.profile_box.setCurrentIndex(PROFILE_ORDER.index(s.profile))
        self._apply_profile()
        overview = self.overview()
        self.filters_button.setText(
            "Overview: as recorded, no filters" if overview else s.filters.summary()
        )
        self.montage_bar.setEnabled(not overview)

    def _sync_toolbar_time(self) -> None:
        t = self.state.t0
        first = timefmt.to_datetime(self.data.timeline_start, self.zone).date()
        here = timefmt.to_datetime(t, self.zone)
        self.day_box.blockSignals(True)
        self.day_box.setMaximum(max(1, self._days()))
        self.day_box.setValue((here.date() - first).days + 1)
        self.day_box.blockSignals(False)
        if not self.time_edit.hasFocus():
            self.time_edit.setText(here.strftime("%H:%M:%S"))

    def _update_status_mode(self) -> None:
        s = self.state
        if self.overview():
            text = "Overview: as recorded, no filters"
        else:
            m = {
                vv.MONTAGE_RECORDED: "As recorded",
                vv.MONTAGE_REFERENCE: f"Referenced to {s.reference}",
                vv.MONTAGE_BIPOLAR: "Lead bipolar",
            }[s.montage]
            text = f"{m}; {s.filters.summary()}"
        self.status_mode.setText(text)

    def _apply_profile(self) -> None:
        palette = vv.resolve_profile(self.state.profile, self.theme.dark)
        if palette is not self.canvas.palette_:
            self.canvas.set_palette(palette)
            self.strip.set_palette(palette)

    # -- pointer and status ----------

    def recording_label(self, index: int) -> str:
        r = self.data.recordings[index]
        if self._reveal(index) and r.name:
            return f"Recording {r.letter}: {r.name}"
        return f"Recording {r.letter}"

    def _pointer(self, t, row) -> None:
        self._pointer_t, self._pointer_row = t, row
        if row is not None:
            self._last_row = row  # remembered for "Show response" after the pointer left
        self.status_time.setText("" if t is None else timefmt.format_time(t, self.zone, 3))
        text = ""
        if self.multi and t is not None and row is not None and row < len(self.canvas.rows):
            owner = [
                i for a, b, i in self.data.ownership(self.canvas.rows[row].channel) if a <= t < b
            ]
            if owner:
                text = " and ".join(self.recording_label(i) for i in owner)
        self.status_recording.setText(text)

    def _export_edge(self, which: str) -> None:
        if self._pointer_t is not None:
            self.export_edge.emit(which, int(self._pointer_t))

    # -- context menu ----------

    def _context_menu(self, row: int, t: int, pos, on_name: bool) -> None:
        menu = QMenu(self)
        if 0 <= row < len(self.canvas.rows):
            ch = self.canvas.rows[row].channel
            menu.addAction("Use as reference", lambda: self.use_as_reference(ch))
            menu.addAction("Hide channel", lambda: self.hide_channel(ch))
            menu.addAction("Hide lead", lambda: self.hide_lead(ch))
            menu.addAction("Show only this lead", lambda: self.show_only_lead(ch))
        if self.state.hidden:
            menu.addAction("Show all channels", lambda: self.set_hidden(frozenset()))
        for hook in self.context_hooks:
            hook(menu, row, t)
        if menu.isEmpty():
            return
        self._menu = menu
        menu.popup(pos)

    # -- misc ----------

    def toggle_help(self) -> None:
        self.help.features = frozenset(self.features)
        if self.help.isVisible():
            self.help.hide()
            return
        self.help.setGeometry(self.canvas.rect().adjusted(12, 12, -12, -12))
        self.help.show()
        self.help.raise_()

    def escape(self) -> None:
        self.canvas.clear_selection()
        for panel in (self.help, self.filter_panel, self.channel_chooser, self.jump_dialog):
            panel.hide()

    def _update_title(self) -> None:
        recs = self.data.recordings
        revealed = [i for i in range(len(recs)) if self._reveal(i)]
        if len(revealed) == len(recs):
            names = ", ".join(r.name for r in recs if r.name)
            title = f"Signal viewer: {names}" if names else "Signal viewer"
        elif len(recs) > 1:
            title = "Signal viewer: recordings " + ", ".join(r.letter for r in recs)
        else:
            title = "Signal viewer"
        self.setWindowTitle(title)

    def refresh_privacy(self) -> None:
        """Call when a recording's details are shown or hidden in Explore."""
        self._update_title()

    def set_zone(self, zone: tzinfo) -> None:
        self.zone = zone
        self.canvas.zone = zone
        self.strip.zone = zone
        self.canvas.invalidate()
        self.strip.update()
        self._sync_toolbar_time()

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self._apply_profile()

    def row_labels(self) -> list[str]:
        return [r.label for r in self.canvas.rows]

    def request_strip_envelope(self) -> None:
        """Draw a faint envelope of the first channel in the strip (index only)."""
        if self._channels:
            self.controller.request_strip_envelope(self._channels[0])

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.refresh_privacy()  # details may have been shown or hidden in Explore

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        QTimer.singleShot(0, self._refresh)

    def save_preferences(self) -> None:
        """Save only what the user chose through the controls (not the
        automatic sensitivity, not a zoom)."""
        s = self.settings
        if s is None:
            return
        st = self.state
        if "sensitivity" in self._chosen:
            s.set("viewer/sensitivity", float(st.sensitivity))
        if "spacing" in self._chosen:
            s.set("viewer/spacing", float(st.spacing))
        if "timebase" in self._chosen and st.timebase in vv.TIMEBASE_CLINICAL:
            s.set("viewer/timebase", float(st.timebase))
        if "profile" in self._chosen:
            s.set("viewer/profile", st.profile)
        if "filters" in self._chosen:
            f = st.filters
            s.set(
                "viewer/filters",
                json.dumps(
                    {
                        "highpass_hz": f.highpass_hz,
                        "lowpass_hz": f.lowpass_hz,
                        "mains_hz": f.mains_hz,
                        "mains_width": f.mains_width,
                        "mains_harmonics": f.mains_harmonics,
                        "order": f.order,
                        "zero_phase": f.zero_phase,
                    }
                ),
            )

    def _shut_down(self) -> None:
        """Stop the scheduler, then the pools, then close the data (in that order)."""
        try:
            self.controller.finish_close()
        finally:
            self.data.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._closed:
            self._closed = True
            self._debounce.stop()
            self.save_preferences()
            self.controller.close()  # cancels at once; the stopping happens off this thread
            threading.Thread(target=self._shut_down, daemon=True).start()
            self.closed.emit()
        super().closeEvent(event)
