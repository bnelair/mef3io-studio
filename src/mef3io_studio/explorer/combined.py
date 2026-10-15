"""The combined timeline: several recordings, one row each, on one shared
time line, with what is worth knowing across them.

Drawn from what the Explorer already read (each part's start, end and
sample count): no file is opened here. The layout is worked out in short
slices between other events (``model.coverage`` through
``workers.slices``); painting draws
only the rows on screen, and only one rectangle per run of pixel columns
(``coverage.pixel_runs``), so the cost of a repaint depends on the size of
the widget, not on how many recordings or parts there are.

Privacy: rows are named like the recordings list (the recording's name;
where two listed recordings share a name, e.g. a folder and its
single-file copy, the file name tells them apart).
Patient details and note text are not part of this view, so the "Show"
choice has nothing to reveal here; times are shown like everywhere else in
the Explorer, in the chosen time zone with its offset.
"""

from __future__ import annotations

from datetime import UTC, tzinfo

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..common.flow import FlowLayout
from ..common.theme import LIGHT, Theme
from ..model.coverage import (
    PIXEL_ALL,
    STATUS_LOCKED,
    STATUS_READING,
    STATUS_UNREADABLE,
    STATUS_WRONG_PASSWORD,
    CombinedLayout,
    RecordingCoverage,
    pixel_runs,
    span_runs,
)
from ..model.export import LEVEL_PARTS, LEVEL_RECORDINGS
from ..model.timefmt import (
    axis_ticks,
    format_count,
    format_span,
    format_time,
    tick_label,
    tick_step,
    zone_label,
)

#: What a row without anything to draw says instead.
STATUS_TEXT = {
    STATUS_READING: "Reading the recording's details…",
    STATUS_LOCKED: "Password needed: enter it to see when this recording has data.",
    STATUS_WRONG_PASSWORD: "That password did not open it: enter the right one to see its times.",
    STATUS_UNREADABLE: "Could not be opened, so there is nothing to draw.",
}
NO_PARTS_TEXT = "No part of this recording holds any data."
UNVERIFIED_TAG = "unverified time"

#: The shortest stretch the view zooms in to (10 ms).
MIN_SPAN = 10_000
ZOOM_STEP = 1.25


def _alpha(color: str, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(alpha)
    return c


class CombinedTimelineWidget(QWidget):
    """One row per recording on a shared time line. Mouse wheel: zoom in
    or out around the pointer (over the names: scroll the rows). Drag:
    move along the time line. Click a row: select that recording;
    double-click: open its details."""

    recording_clicked = Signal(str)
    recording_activated = Signal(str)
    view_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout_data: CombinedLayout | None = None
        self.zone: tzinfo = UTC
        self.theme: Theme = LIGHT
        self.current_key: str | None = None
        self.v0 = 0
        self.v1 = 1
        self._drag: tuple[float, int, int] | None = None
        self._wheel_rows = 0.0
        self._gutter_cache: tuple = (None, 0)
        self._dragged = False
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.scrollbar = QScrollBar(Qt.Orientation.Vertical, self)
        self.scrollbar.valueChanged.connect(self.update)
        self.scrollbar.hide()

    # -- geometry ------------------------------------------------------------
    def _metrics(self):
        fm = self.fontMetrics()
        em = fm.height()
        row = round(em * 1.45)
        # The names' width is measured once per layout and font.
        cache_key = (id(self.layout_data), em)
        if self._gutter_cache[0] != cache_key:
            rows = self.layout_data.rows if self.layout_data else ()
            widest = max((fm.horizontalAdvance(r.name) for r in rows[:2000]), default=0)
            gutter = min(widest + em, round(em * 13))
            self._gutter_cache = (cache_key, max(gutter, round(em * 6)))
        gutter = self._gutter_cache[1]
        axis = round(em * 2.85)
        return em, row, gutter, axis

    def plot_rect(self) -> QRectF:
        em, _row, gutter, axis = self._metrics()
        right = self.width() - em * 0.5
        if self.scrollbar.isVisible():
            right -= self.scrollbar.width()
        return QRectF(gutter, axis, max(1.0, right - gutter), max(1.0, self.height() - axis))

    def sizeHint(self) -> QSize:  # noqa: N802
        # Small on purpose: the panel gives it every pixel left over (stretch),
        # and a large preferred height would make the panel scroll instead.
        em, row, _g, axis = self._metrics()
        return QSize(em * 30, axis + 3 * row)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        em, row, _g, axis = self._metrics()
        return QSize(em * 16, axis + 2 * row)

    def visible_rows(self) -> int:
        _em, row, _g, axis = self._metrics()
        return max(1, int((self.height() - axis) // row))

    def _update_scrollbar(self) -> None:
        n = len(self.layout_data.rows) if self.layout_data else 0
        page = self.visible_rows()
        needed = n > page
        self.scrollbar.setVisible(needed)
        self.scrollbar.setRange(0, max(0, n - page))
        self.scrollbar.setPageStep(page)
        _em, _row, _g, axis = self._metrics()
        w = self.scrollbar.sizeHint().width()
        self.scrollbar.setGeometry(self.width() - w, axis, w, max(0, self.height() - axis))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_scrollbar()

    # -- data ------------------------------------------------------------------
    def data_range(self) -> tuple[int, int] | None:
        """First and last time of every row with data (verified or not, so
        nothing is drawn off screen when fitting)."""
        rows = [r for r in (self.layout_data.rows if self.layout_data else ()) if r.has_data]
        if not rows:
            return None
        return min(r.first for r in rows), max(r.last for r in rows)

    def set_layout(self, layout: CombinedLayout | None, refit: bool) -> None:
        old = self.data_range()
        self.layout_data = layout
        self._update_scrollbar()
        new = self.data_range()
        if refit or old is None or new != old and not self._inside(new):
            self.fit_all()
        else:
            self.update()

    def _inside(self, rng) -> bool:
        return rng is not None and self.v0 <= rng[0] and rng[1] <= self.v1

    def set_zone(self, zone: tzinfo) -> None:
        self.zone = zone
        self.update()

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.update()

    def set_current(self, key: str | None) -> None:
        if key != self.current_key:
            self.current_key = key
            self.update()

    # -- zoom and pan --------------------------------------------------------
    def fit_all(self) -> None:
        rng = self.data_range()
        if rng is None:
            self.v0, self.v1 = 0, 1
        else:
            span = max(rng[1] - rng[0], 1_000_000)
            pad = span // 50
            self.v0, self.v1 = rng[0] - pad, rng[0] + span + pad
        self._wheel_rows = 0.0
        self.update()
        self.view_changed.emit()

    def _limits(self) -> tuple[int, int]:
        rng = self.data_range() or (0, 1)
        span = max(rng[1] - rng[0], 1_000_000)
        return MIN_SPAN, span * 4

    def _keep_in_reach(self) -> None:
        """Never move so far that no data is left on screen."""
        rng = self.data_range()
        if rng is None:
            return
        span = self.v1 - self.v0
        margin = span // 2
        if self.v0 > rng[1] - margin:
            self.v0 = rng[1] - margin
        elif self.v1 < rng[0] + margin:
            self.v0 = rng[0] + margin - span
        self.v1 = self.v0 + span

    def zoom(self, factor: float, anchor_x: float | None = None) -> None:
        """``factor`` > 1 zooms in. ``anchor_x``: the pixel that stays put."""
        plot = self.plot_rect()
        if anchor_x is None:
            anchor_x = plot.center().x()
        frac = min(1.0, max(0.0, (anchor_x - plot.left()) / plot.width()))
        span = self.v1 - self.v0
        lo, hi = self._limits()
        new = int(min(hi, max(lo, span / factor)))
        anchor = self.v0 + span * frac
        self.v0 = int(anchor - new * frac)
        self.v1 = self.v0 + new
        self._keep_in_reach()
        self.update()
        self.view_changed.emit()

    def pan_pixels(self, dx: float) -> None:
        plot = self.plot_rect()
        shift = int((self.v1 - self.v0) * dx / plot.width())
        self.v0 -= shift
        self.v1 -= shift
        self._keep_in_reach()
        self.update()
        self.view_changed.emit()

    def wheelEvent(self, event) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120
        if not steps:
            event.ignore()
            return
        pos = event.position()
        if pos.x() < self.plot_rect().left():
            # Over the names: scroll the rows (nothing happens when they all
            # fit). Small trackpad steps add up until they make a whole row.
            self._wheel_rows -= steps * 3
            whole = int(self._wheel_rows)
            if whole:
                self._wheel_rows -= whole
                self.scrollbar.setValue(self.scrollbar.value() + whole)
        else:
            self.zoom(ZOOM_STEP**steps, pos.x())
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom(ZOOM_STEP * ZOOM_STEP)
        elif key == Qt.Key.Key_Minus:
            self.zoom(1 / (ZOOM_STEP * ZOOM_STEP))
        elif key == Qt.Key.Key_Left:
            self.pan_pixels(self.plot_rect().width() / 8)
        elif key == Qt.Key.Key_Right:
            self.pan_pixels(-self.plot_rect().width() / 8)
        elif key == Qt.Key.Key_Home:
            self.fit_all()
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.scrollbar.setValue(self.scrollbar.value() + (-1 if key == Qt.Key.Key_Up else 1))
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = (event.position().x(), self.v0, self.v1)
            self._dragged = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._dragged:
                row = self.row_at(event.position())
                if row is not None:
                    self.recording_clicked.emit(row.key)
            self._drag = None
            self.unsetCursor()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        row = self.row_at(event.position())
        if row is not None:
            self.recording_activated.emit(row.key)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            x0, v0, v1 = self._drag
            if abs(pos.x() - x0) > 3:
                self._dragged = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._dragged:
                shift = int((v1 - v0) * (pos.x() - x0) / self.plot_rect().width())
                self.v0, self.v1 = v0 - shift, v1 - shift
                self._keep_in_reach()
                self.update()
                self.view_changed.emit()
            return
        tip = self.tooltip_at(pos)
        if tip:
            QToolTip.showText(event.globalPosition().toPoint(), tip, self)
        else:
            QToolTip.hideText()

    # -- what is where ------------------------------------------------------
    def row_at(self, pos: QPointF) -> RecordingCoverage | None:
        if self.layout_data is None:
            return None
        _em, row_h, _g, axis = self._metrics()
        if pos.y() < axis:
            return None
        index = int((pos.y() - axis) // row_h) + self.scrollbar.value()
        rows = self.layout_data.rows
        return rows[index] if 0 <= index < len(rows) else None

    def time_at(self, x: float) -> int:
        plot = self.plot_rect()
        return int(self.v0 + (self.v1 - self.v0) * (x - plot.left()) / plot.width())

    def tooltip_at(self, pos: QPointF) -> str:
        row = self.row_at(pos)
        if row is None:
            return ""
        lines = [row.name]
        if not row.has_data:
            lines.append(STATUS_TEXT.get(row.status, NO_PARTS_TEXT))
            return "\n".join(lines)
        if not row.verified:
            lines.append(
                "Unverified time: these times may not be the real ones, so they are "
                "not counted with the others."
            )
        if pos.x() < self.plot_rect().left():
            lines.append(
                f"{format_time(row.first, self.zone)} to {format_time(row.last, self.zone)}"
            )
            lines.append(f"{format_span(row.data_seconds)} with data")
            return "\n".join(lines)
        # Half a pixel either side, so a short stretch can still be pointed at.
        t = self.time_at(pos.x())
        slack = int((self.v1 - self.v0) / max(1.0, self.plot_rect().width()))
        run = row.run_at(t) or row.run_at(t - slack) or row.run_at(t + slack)
        if run is not None:
            lines.append(
                f"Data from {format_time(run[0], self.zone)} to {format_time(run[1], self.zone)}"
            )
            lines.append(format_span((run[1] - run[0]) / 1e6))
            step = row.step_at(min(max(t, run[0]), run[1] - 1)) if run[1] > run[0] else None
            if step is not None:
                count, inside = step
                if count >= row.channel_count:
                    lines.append(f"All {row.channel_count} channels have data here")
                else:
                    lines.append(f"{count} of {row.channel_count} channels have data here")
                if inside:
                    lines.append("Some of these parts have gaps inside them")
        else:
            gap = row.gap_at(t)
            if gap is not None:
                lines.append(
                    f"No data from {format_time(gap[0], self.zone)} "
                    f"to {format_time(gap[1], self.zone)}"
                )
                lines.append(f"A gap of {format_span((gap[1] - gap[0]) / 1e6)}")
            else:
                return ""
        layout = self.layout_data
        others = set(layout.overlapping.get(row.key, ())) if layout else set()
        if others:
            names = [r.name for r in layout.rows if r.key in others]
            shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
            lines.append(f"Covers the same time as: {shown}")
        elif layout is not None and row.key in layout.overlap_spans_of:
            lines.append("Covers the same time as other recordings")
        return "\n".join(lines)

    # -- painting ------------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        theme = self.theme
        em, row_h, gutter, axis = self._metrics()
        plot = self.plot_rect()
        width = int(plot.width())
        painter.fillRect(self.rect(), QColor(theme.surface))
        layout = self.layout_data
        if layout is None or not layout.rows:
            painter.end()
            return
        v0, v1 = self.v0, self.v1
        scale = plot.width() / max(1, v1 - v0)

        def x_of(t: int) -> float:
            return plot.left() + (t - v0) * scale

        fm = self.fontMetrics()
        muted, text = QColor(theme.muted), QColor(theme.text)
        # The axis: the times at both ends (with the offset), then marks.
        painter.setPen(muted)
        painter.drawText(
            QRectF(plot.left(), 0, plot.width() / 2, em * 1.0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            format_time(v0, self.zone),
        )
        painter.drawText(
            QRectF(plot.center().x(), 0, plot.width() / 2, em * 1.0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            format_time(v1, self.zone),
        )
        painter.drawText(
            QRectF(0, 0, gutter - em * 0.5, em * 1.0),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            zone_label(self.zone),
        )
        base = em * 2.05
        label_w = fm.horizontalAdvance("2000-01-01 00:00 ") + em
        step = tick_step(v1 - v0, max(1, int(plot.width() // label_w)))
        grid = QPen(_alpha(theme.divider, 1.0))
        tick_pen = QPen(QColor(theme.border))
        previous_day = None
        for t in axis_ticks(v0, v1, step, self.zone):
            x = x_of(t)
            painter.setPen(grid)
            painter.drawLine(QPointF(x, axis), QPointF(x, self.height()))
            painter.setPen(tick_pen)
            painter.drawLine(QPointF(x, base - em * 0.25), QPointF(x, base))
            day = format_time(t, self.zone)[:10]
            label = tick_label(t, step, self.zone, with_date=day != previous_day)
            previous_day = day
            painter.setPen(muted)
            if x + fm.horizontalAdvance(label) > plot.right() + em * 0.5:
                continue  # would run off the edge
            painter.drawText(
                QRectF(x + 2, base - em * 1.0, label_w, em),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
        painter.setPen(tick_pen)
        painter.drawLine(QPointF(plot.left(), base), QPointF(plot.right(), base))
        # Time covered by two or more recordings: a band under the axis.
        band = QRectF(plot.left(), base + em * 0.25, plot.width(), em * 0.4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.danger))
        for a, b in span_runs(layout.overlap_spans, v0, v1, width):
            painter.drawRect(QRectF(plot.left() + a, band.top(), b - a, band.height()))

        # The rows on screen only.
        first = self.scrollbar.value()
        count = self.visible_rows() + 1
        accent = theme.accent
        painter.setClipRect(QRectF(0, axis, self.width(), self.height() - axis))
        for n, row in enumerate(layout.rows[first : first + count]):
            y = axis + n * row_h
            band_rect = QRectF(0, y, plot.right(), row_h)
            if row.key == self.current_key:
                painter.fillRect(band_rect, QColor(theme.accent_soft))
            elif (first + n) % 2:
                painter.fillRect(band_rect, QColor(theme.surface_alt))
            painter.setPen(text if row.has_data and row.verified else muted)
            painter.drawText(
                QRectF(em * 0.3, y, gutter - em * 0.8, row_h),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                fm.elidedText(row.name, Qt.TextElideMode.ElideRight, int(gutter - em * 0.8)),
            )
            bar = QRectF(plot.left(), y + row_h * 0.22, plot.width(), row_h * 0.56)
            if not row.has_data:
                painter.setPen(muted)
                painter.drawText(
                    bar.adjusted(em * 0.3, 0, 0, 0),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    fm.elidedText(
                        STATUS_TEXT.get(row.status, NO_PARTS_TEXT),
                        Qt.TextElideMode.ElideRight,
                        int(bar.width() - em * 0.3),
                    ),
                )
                continue
            # From first to last: a thin line, so gaps inside it read as gaps.
            x0, x1 = max(plot.left(), x_of(row.first)), min(plot.right(), x_of(row.last))
            if x1 >= x0:
                painter.setPen(QPen(QColor(theme.border), 1))
                painter.drawLine(QPointF(x0, bar.center().y()), QPointF(x1, bar.center().y()))
            color = accent if row.verified else theme.muted
            full = QColor(color)
            some = _alpha(color, 0.45)
            hatch = QBrush(QColor(theme.text), Qt.BrushStyle.BDiagPattern)
            painter.setPen(Qt.PenStyle.NoPen)
            for a, b, cls, gappy in pixel_runs(row, v0, v1, width):
                rect = QRectF(plot.left() + a, bar.top(), b - a, bar.height())
                painter.setBrush(full if cls == PIXEL_ALL else some)
                painter.drawRect(rect)
                if gappy:
                    painter.setBrush(hatch)
                    painter.drawRect(rect)
                if not row.verified:
                    painter.setBrush(QBrush(QColor(theme.surface), Qt.BrushStyle.Dense6Pattern))
                    painter.drawRect(rect)
            # Time this recording shares with another one: a line under it.
            spans = layout.overlap_spans_of.get(row.key)
            if spans:
                painter.setBrush(QColor(theme.danger))
                for a, b in span_runs(spans, v0, v1, width):
                    painter.drawRect(
                        QRectF(plot.left() + a, bar.bottom() + 1, max(1, b - a), row_h * 0.1)
                    )
            if not row.verified:
                tag_w = fm.horizontalAdvance(UNVERIFIED_TAG) + em * 0.6
                tx = max(plot.left(), x1 + em * 0.4)
                if tx + tag_w > plot.right():
                    tx = max(plot.left(), x0 - tag_w - em * 0.4)
                painter.setPen(muted)
                painter.drawText(
                    QRectF(tx, y, tag_w, row_h),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    UNVERIFIED_TAG,
                )
        painter.end()


class LegendSwatch(QWidget):
    """A small sample of how something is drawn, for the legend."""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.theme: Theme = LIGHT
        em = self.fontMetrics().height()
        self.setFixedSize(round(em * 1.6), round(em * 0.9))

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        t = self.theme
        r = QRectF(self.rect()).adjusted(0, 1, 0, -1)
        p.setPen(Qt.PenStyle.NoPen)
        if self.kind == "all":
            p.setBrush(QColor(t.accent))
        elif self.kind == "some":
            p.setBrush(_alpha(t.accent, 0.45))
        elif self.kind == "inside":
            p.setBrush(QColor(t.accent))
            p.drawRect(r)
            p.setBrush(QBrush(QColor(t.text), Qt.BrushStyle.BDiagPattern))
        elif self.kind == "unverified":
            p.setBrush(QColor(t.muted))
            p.drawRect(r)
            p.setBrush(QBrush(QColor(t.surface), Qt.BrushStyle.Dense6Pattern))
        elif self.kind == "overlap":
            r = QRectF(r.left(), r.center().y() - 2, r.width(), 4)
            p.setBrush(QColor(t.danger))
        p.drawRect(r)
        p.end()


LEGEND = (
    ("all", "All channels have data"),
    ("some", "Only some channels"),
    ("inside", "Gaps inside a part"),
    ("overlap", "In more than one recording"),
    ("unverified", "Unverified time"),
)


class CombinedTimelinePanel(QWidget):
    """The combined timeline with its summary, legend and buttons."""

    recording_clicked = Signal(str)
    recording_activated = Signal(str)
    export_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.zone: tzinfo = UTC
        self.layout_data: CombinedLayout | None = None
        self.names: dict[str, str] = {}
        em = self.fontMetrics().height()
        self.caption = QLabel("", self)
        self.caption.setObjectName("Muted")
        self.fit_button = QToolButton(self)
        self.fit_button.setText("Fit all")
        self.fit_button.setToolTip("Show every recording's times (Home key)")
        self.zoom_in_button = QToolButton(self)
        self.zoom_in_button.setText("Zoom in")
        self.zoom_in_button.setToolTip("Or turn the mouse wheel over the timeline")
        self.zoom_out_button = QToolButton(self)
        self.zoom_out_button.setText("Zoom out")
        self.export_button = QToolButton(self)
        self.export_button.setText("Save times as a table")
        self.export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.export_button)
        self.export_recordings_action = menu.addAction("One row per recording (CSV)…")
        self.export_recordings_action.triggered.connect(
            lambda: self.export_requested.emit(LEVEL_RECORDINGS)
        )
        self.export_parts_action = menu.addAction("One row per part of each channel (CSV)…")
        self.export_parts_action.triggered.connect(lambda: self.export_requested.emit(LEVEL_PARTS))
        self.export_button.setMenu(menu)
        # One row that wraps: the buttons, then which recordings are shown.
        buttons = FlowLayout()
        for b in (self.fit_button, self.zoom_in_button, self.zoom_out_button, self.export_button):
            buttons.addWidget(b)
        buttons.addWidget(self.caption)

        self.summary = QLabel("", self)
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.overlap_box = QFrame(self)
        self.overlap_box.setObjectName("Warning")
        box_layout = QVBoxLayout(self.overlap_box)
        box_layout.setContentsMargins(int(em * 0.6), int(em * 0.4), int(em * 0.6), int(em * 0.4))
        self.overlap_label = QLabel("", self.overlap_box)
        self.overlap_label.setWordWrap(True)
        box_layout.addWidget(self.overlap_label)
        self.overlap_box.hide()
        self.busy = QLabel("Laying out the timeline…", self)
        self.busy.setObjectName("Muted")
        self.busy.hide()

        legend = QVBoxLayout()
        legend.setSpacing(int(em * 0.2))
        self.swatches = []
        for kind, words in LEGEND:
            item = QWidget(self)
            row = QHBoxLayout(item)
            row.setContentsMargins(0, 0, 0, 0)
            swatch = LegendSwatch(kind, item)
            self.swatches.append(swatch)
            label = QLabel(words, item)
            label.setObjectName("Muted")
            row.addWidget(swatch)
            label.setWordWrap(True)
            row.addWidget(label, 1)
            legend.addWidget(item)

        self.timeline = CombinedTimelineWidget(self)
        self.timeline.recording_clicked.connect(self.recording_clicked)
        self.timeline.recording_activated.connect(self.recording_activated)
        self.fit_button.clicked.connect(self.timeline.fit_all)
        self.zoom_in_button.clicked.connect(lambda: self.timeline.zoom(ZOOM_STEP * ZOOM_STEP))
        self.zoom_out_button.clicked.connect(
            lambda: self.timeline.zoom(1 / (ZOOM_STEP * ZOOM_STEP))
        )

        # Beside the timeline: the figures, any warning, and the legend, in
        # a column that scrolls by itself, so they never take height away
        # from the timeline (or make the page taller than the window).
        side_inner = QWidget(self)
        side_layout = QVBoxLayout(side_inner)
        side_layout.setContentsMargins(int(em * 0.6), 0, 0, 0)
        side_layout.addWidget(self.summary)
        side_layout.addWidget(self.overlap_box)
        side_layout.addLayout(legend)
        side_layout.addWidget(self.busy)
        side_layout.addStretch(1)
        self.side = QScrollArea(self)
        self.side.setObjectName("Panel")
        self.side.setWidgetResizable(True)
        self.side.setFrameShape(QFrame.Shape.NoFrame)
        self.side.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.side.setWidget(side_inner)
        self.side.setFixedWidth(round(em * 17))
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.addWidget(self.timeline, 1)
        body.addWidget(self.side)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, int(em * 0.3), 0, 0)
        layout.addLayout(buttons)
        layout.addLayout(body, 1)
        self._busy_timer = QTimer(self)
        self._busy_timer.setSingleShot(True)
        self._busy_timer.setInterval(250)  # only say "laying out" when it takes a while
        self._busy_timer.timeout.connect(self.busy.show)

    # -- input -----------------------------------------------------------------
    def set_caption(self, text: str) -> None:
        self.caption.setText(text)

    def set_computing(self, computing: bool) -> None:
        if computing:
            self._busy_timer.start()
        else:
            self._busy_timer.stop()
            self.busy.hide()

    def set_layout(self, layout: CombinedLayout | None, refit: bool = False) -> None:
        self.layout_data = layout
        self.set_computing(False)
        self.timeline.set_layout(layout, refit)
        self._describe()

    def set_zone(self, zone: tzinfo) -> None:
        self.zone = zone
        self.timeline.set_zone(zone)
        self._describe()

    def set_theme(self, theme: Theme) -> None:
        self.timeline.set_theme(theme)
        for swatch in self.swatches:
            swatch.theme = theme
            swatch.update()

    def set_current(self, key: str | None) -> None:
        self.timeline.set_current(key)

    # -- the summary -----------------------------------------------------------
    def _describe(self) -> None:
        layout = self.layout_data
        if layout is None:
            self.summary.setText("")
            self.overlap_box.hide()
            return
        self.summary.setText(
            " ".join(t for t in (summary_text(layout, self.zone), note_text(layout)) if t)
        )
        warning = overlap_text(layout)
        self.overlap_label.setText(warning)
        self.overlap_box.setVisible(bool(warning))


def summary_text(layout: CombinedLayout, zone: tzinfo) -> str:
    """The figures across the recordings, in one or two lines."""
    counted = layout.counted
    if not counted:
        return "No times to show yet."
    parts = [
        f"From {format_time(layout.first, zone)} to {format_time(layout.last, zone)}",
        f"{format_span(layout.span_seconds)} from first to last",
    ]
    span = layout.span_seconds or 0
    share = ""
    if span > 0:
        ratio = layout.data_seconds / span
        share = " (under 1%)" if 0 < ratio < 0.01 else f" ({ratio:.0%})"
    parts.append(f"{format_span(layout.data_seconds)} with data{share}")
    gaps = layout.gaps_between
    if len(counted) > 1:
        if gaps:
            longest = max(e - s for s, e in gaps) / 1e6
            parts.append(
                f"{format_count(len(gaps), 'gap')} between recordings, "
                f"{format_span(layout.gaps_between_seconds)} in total "
                f"(longest {format_span(longest)})"
            )
        else:
            parts.append("no gaps between recordings")
    return " · ".join(parts) + "."


def note_text(layout: CombinedLayout) -> str:
    """What the figures leave out, and why."""
    notes = []
    unverified = layout.unverified
    if unverified:
        notes.append(
            f"Not counted: {format_count(len(unverified), 'recording')} with unverified "
            "times, which may not be the real ones."
        )
    missing = layout.without_data
    if missing:
        notes.append(
            f"{format_count(len(missing), 'recording')} with nothing to draw (its row says why)."
            if len(missing) == 1
            else f"{format_count(len(missing), 'recording')} with nothing to draw "
            "(each row says why)."
        )
    return " ".join(notes)


def overlap_text(layout: CombinedLayout, limit: int = 2) -> str:
    """A plain warning when recordings cover the same time."""
    sharing = layout.sharing
    if not sharing:
        return ""
    if not layout.pairs_complete:
        return (
            f"{format_count(len(sharing), 'recording')} cover the same time as others, "
            "too many to list in pairs (for example several beds recorded at once). "
            "Point at a row to see its times."
        )
    # Pairs that may be copies first: they are the ones worth acting on.
    ordered = sorted(layout.overlaps, key=lambda o: (not o.may_be_copies, -o.microseconds))
    pairs = []
    for o in ordered[:limit]:
        tail = ", may be copies" if o.may_be_copies else ""
        pairs.append(f"{o.first_name} and {o.second_name} ({format_span(o.seconds)}{tail})")
    more = len(ordered) - limit
    if more > 0:
        pairs.append(format_count(more, "more pair"))
    head = "Some of these recordings cover the same time: " + "; ".join(pairs) + "."
    if any(o.may_be_copies for o in ordered):
        return head + (
            " Recordings that share nearly all their time and the same channels may be "
            "copies of each other; others were made at the same time."
        )
    return head + " This happens with recordings made at the same time, or with copies."
