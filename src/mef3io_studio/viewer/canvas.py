"""The trace area: traces on a cached pixmap, a separate overlay.

The traces, the gutter with the channel names, the time axis and the grid
are painted into a pixmap only when the view or the data changes. Moving the
pointer repaints just the overlay on top of it (cursor, hover time,
measurement box, spinner), never the traces (docs/design/viewer.md,
"Drawing"). One polyline per row: exact samples joined by straight lines
when there are few, otherwise the minimum and maximum of every pixel column
(``dsp.minmax_columns``). Gaps are shaded bands.

Widgets here never import mef3io: the data arrives as ready-to-draw pieces
(``workers.viewer_pipeline``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, tzinfo

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..model import timefmt
from ..model.viewer_view import Palette, format_measure
from ..workers.viewer_pipeline import EnvelopePiece, ViewPiece, envelope_columns


@dataclass
class DrawRow:
    """One row on screen. ``piece`` is None while it loads."""

    label: str
    channel: str  # the channel the row shows (its plus channel)
    piece: ViewPiece | EnvelopePiece | None
    local: int = 0  # the row's index inside ``piece``
    units: str = ""
    tag: str = ""  # recording letters, only with 2+ recordings
    note: str | None = None


def _polygon(xs: np.ndarray, ys: np.ndarray) -> QPolygonF:
    return QPolygonF([QPointF(float(x), float(y)) for x, y in zip(xs, ys, strict=True)])


class TraceCanvas(QWidget):
    #: Pointer position: (time in µs or None, row index or None).
    pointer_changed = Signal(object, object)
    #: Mouse wheel: whole notches of time, rows, and zoom (with the x fraction).
    wheel_time = Signal(float)
    wheel_rows = Signal(float)
    wheel_zoom = Signal(float, float)
    #: A drag: time moved (µs, to subtract from the page start) and rows moved.
    panned = Signal(float, int)
    pan_finished = Signal()
    centre_requested = Signal(object)
    #: Right-click: (row index or -1, time µs, global position, on_name).
    context_requested = Signal(int, object, object, bool)
    size_changed = Signal()

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self.setObjectName("TraceCanvas")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.palette_ = palette
        self.zone: tzinfo = UTC
        self.rows: list[DrawRow] = []
        self.t0 = 0
        self.t1 = 1
        self.sens = 100.0
        self.spacing_mm = 7.0
        self.row_offset = 0
        self.overview = False
        self.multi = False
        self.overview_text = ""
        self.edges_note = ""
        self.loading = False
        self._dirty = True
        self._pm: QPixmap | None = None
        self._pointer: QPointF | None = None
        self._measure_from: QPointF | None = None
        self._drag: tuple[QPointF, float, int] | None = None
        self._spin = 0
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(80)
        self._spin_timer.timeout.connect(self._tick_spinner)
        #: Last measurement text (for tests and the status line).
        self.measure_text = ""
        #: Painted traces per row, for tests: row -> number of polyline points.
        self.drawn_points: dict[int, int] = {}
        self.setMinimumHeight(self.fontMetrics().height() * 8)

    # -- geometry ---------------------------------------------------------

    @property
    def px_per_mm(self) -> float:
        return self.logicalDpiX() / 25.4

    @property
    def gutter(self) -> int:
        return max(84, int(self.fontMetrics().height() * 6.5))

    @property
    def axis_height(self) -> int:
        return int(self.fontMetrics().height() * 2.3)

    def plot_rect(self) -> QRectF:
        return QRectF(
            self.gutter,
            0,
            max(1, self.width() - self.gutter),
            max(1, self.height() - self.axis_height),
        )

    @property
    def plot_width(self) -> int:
        return max(1, self.width() - self.gutter)

    @property
    def row_height(self) -> float:
        return self.spacing_mm * self.px_per_mm

    def visible_rows(self) -> int:
        return max(1, int(self.plot_rect().height() // self.row_height))

    def row_y(self, i: int) -> float:
        """Vertical centre of row ``i``."""
        return (i - self.row_offset + 0.5) * self.row_height

    def x_of(self, t: float) -> float:
        r = self.plot_rect()
        return r.left() + (t - self.t0) / max(1.0, self.t1 - self.t0) * r.width()

    def t_of(self, x: float) -> float:
        r = self.plot_rect()
        return self.t0 + (x - r.left()) / r.width() * (self.t1 - self.t0)

    def row_at(self, y: float) -> int | None:
        if y < 0 or y >= self.plot_rect().height():
            return None
        i = int(y // self.row_height) + self.row_offset
        return i if 0 <= i < len(self.rows) else None

    # -- state -------------------------------------------------------------

    def set_palette(self, palette: Palette) -> None:
        self.palette_ = palette
        self.invalidate()

    def set_view(self, t0: int, t1: int, sens: float, spacing_mm: float, row_offset: int) -> None:
        self.t0, self.t1 = t0, max(t1, t0 + 1)
        self.sens, self.spacing_mm, self.row_offset = sens, spacing_mm, row_offset
        self.invalidate()

    def set_rows(self, rows: Sequence[DrawRow], overview: bool, multi: bool) -> None:
        self.rows = list(rows)
        self.overview = overview
        self.multi = multi
        self.invalidate()

    def set_loading(self, loading: bool) -> None:
        if loading == self.loading:
            return
        self.loading = loading
        if loading:
            self._spin_timer.start()
        else:
            self._spin_timer.stop()
        self.update()

    def invalidate(self) -> None:
        self._dirty = True
        self.update()

    def _tick_spinner(self) -> None:
        self._spin = (self._spin + 1) % 12
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._dirty = True
        self.size_changed.emit()

    # -- painting ------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        if (
            self._dirty
            or self._pm is None
            or self._pm.size() != self.size() * self.devicePixelRatioF()
        ):
            self._render()
        p = QPainter(self)
        p.drawPixmap(0, 0, self._pm)
        self._paint_overlay(p)
        p.end()

    def grab_traces(self) -> QPixmap:
        """The cached trace layer, rendered now (for tests and screenshots)."""
        if self._dirty or self._pm is None:
            self._render()
        return self._pm

    def _render(self) -> None:
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(self.width() * dpr), int(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pal = self.palette_
        pm.fill(QColor(pal.background))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.drawn_points = {}
        plot = self.plot_rect()
        self._paint_grid(p, plot)
        p.save()
        p.setClipRect(plot)
        self._paint_gaps_and_traces(p, plot)
        p.restore()
        self._paint_gutter(p, plot)
        self._paint_axis(p, plot)
        p.end()
        self._pm = pm
        self._dirty = False

    def _paint_grid(self, p: QPainter, plot: QRectF) -> None:
        pal = self.palette_
        if pal.grid_minor and pal.grid_major:
            mm = self.px_per_mm
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            for step, colour in ((1, pal.grid_minor), (5, pal.grid_major)):
                p.setPen(QPen(QColor(colour), 1))
                n = int(plot.width() / mm) + 1
                for k in range(0, n, step):
                    x = plot.left() + k * mm
                    p.drawLine(QPointF(x, 0), QPointF(x, plot.height()))
                n = int(plot.height() / mm) + 1
                for k in range(0, n, step):
                    y = k * mm
                    p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # vertical time grid at the axis marks
        p.setPen(QPen(QColor(pal.time_grid), 1))
        for t in self._ticks()[0]:
            x = self.x_of(t)
            p.drawLine(QPointF(x, 0), QPointF(x, plot.height()))

    def _ticks(self) -> tuple[list[int], int]:
        span = int(self.t1 - self.t0)
        step = timefmt.tick_step(
            span, max(2, int(self.plot_width / (self.fontMetrics().height() * 7)))
        )
        return timefmt.axis_ticks(int(self.t0), int(self.t1), step, self.zone), step

    def _paint_gaps_and_traces(self, p: QPainter, plot: QRectF) -> None:
        pal = self.palette_
        first = self.row_offset
        last = min(len(self.rows), first + self.visible_rows() + 1)
        w = max(1, int(plot.width()))
        pen = QPen(QColor(pal.trace), 1.2)
        dim = QPen(QColor(pal.trace_dim), 1.2)
        px_uv = self.px_per_mm / max(1e-9, self.sens)
        half = self.row_height / 2
        for i in range(first, last):
            row = self.rows[i]
            yc = self.row_y(i)
            if row.piece is None:
                p.setPen(QPen(QColor(pal.time_grid), 1, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(plot.left(), yc), QPointF(plot.right(), yc))
                continue
            shape = self._row_shape(row, w)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(pal.gap))
            for a, b in shape.gaps:
                p.drawRect(QRectF(plot.left() + a, yc - half, b - a, self.row_height))
            limit = self.row_height * 40
            total = 0
            for xs, ys, seg_dim in shape.segments:
                ys = np.clip(yc - ys * px_uv, yc - limit, yc + limit)
                p.setPen(dim if seg_dim else pen)
                p.drawPolyline(_polygon(plot.left() + xs, ys))
                total += xs.size
            self.drawn_points[i] = total

    def _row_shape(self, row: DrawRow, w: int) -> _RowShape:
        """What to draw for a row: polylines (x in pixels from the plot's
        left edge, y in microvolts) and gap bands (pixel ranges)."""
        piece = row.piece
        if isinstance(piece, EnvelopePiece):
            mn, mx = envelope_columns(piece.rows[row.local], self.t0, self.t1, w)
            return _columns_shape(mn, mx, np.zeros(w, dtype=bool))
        dt = piece.dt_us
        n = piece.values.shape[1]
        j0 = max(0, int(math.floor((self.t0 - piece.t_first_us) / dt)) - 1)
        j1 = min(n, int(math.ceil((self.t1 - piece.t_first_us) / dt)) + 1)
        if j1 <= j0:
            return _RowShape([], [(0, w)])
        y = piece.values[row.local, j0:j1].astype(np.float64)
        um = piece.unfiltered[row.local, j0:j1]
        t = piece.t_first_us + np.arange(j0, j1) * dt
        x = (t - self.t0) / (self.t1 - self.t0) * w
        if len(y) <= 2 * w:  # exact samples joined by straight lines
            return _exact_shape(x, y, um, dt / (self.t1 - self.t0) * w, w)
        keep = (x >= 0) & (x < w)
        y, um, x = y[keep], um[keep], x[keep]
        if y.size == 0:
            return _RowShape([], [(0, w)])
        col = np.clip(np.floor(x).astype(np.int64), 0, w - 1)
        edges = np.searchsorted(col, np.arange(w))
        present = np.zeros(w, dtype=bool)
        present[col] = True
        edges = np.minimum(edges, y.size - 1)
        with np.errstate(all="ignore"):
            lo = np.fmin.reduceat(y, edges)
            hi = np.fmax.reduceat(y, edges)
            du = np.maximum.reduceat(um.astype(np.uint8), edges).astype(bool)
        mn = np.where(present, lo, np.nan)
        mx = np.where(present, hi, np.nan)
        return _columns_shape(mn, mx, du & present)

    def _paint_gutter(self, p: QPainter, plot: QRectF) -> None:
        pal = self.palette_
        p.setClipping(False)
        p.fillRect(QRectF(0, 0, self.gutter, self.height()), QColor(pal.background))
        p.setPen(QPen(QColor(pal.time_grid), 1))
        p.drawLine(QPointF(self.gutter - 0.5, 0), QPointF(self.gutter - 0.5, plot.height()))
        font = QFont(self.font())
        p.setFont(font)
        fm = p.fontMetrics()
        first = self.row_offset
        last = min(len(self.rows), first + self.visible_rows() + 1)
        for i in range(first, last):
            row = self.rows[i]
            yc = self.row_y(i)
            if yc - self.row_height / 2 > plot.height():
                break
            p.setPen(QColor(pal.text))
            label = fm.elidedText(
                row.label,
                Qt.TextElideMode.ElideRight,
                self.gutter - 8 - (fm.horizontalAdvance("AA") if row.tag else 0),
            )
            p.drawText(QPointF(4, yc + fm.ascent() / 2 - 1), label)
            if row.tag:
                p.setPen(QColor(pal.muted))
                tw = fm.horizontalAdvance(row.tag)
                p.drawText(
                    QPointF(self.gutter - tw - 4, yc - self.row_height / 2 + fm.ascent()), row.tag
                )
            if row.note:
                p.setPen(QColor(pal.muted))
                small = QFont(font)
                small.setPointSizeF(font.pointSizeF() * 0.8)
                p.setFont(small)
                p.drawText(QPointF(4, yc + fm.ascent() + 2), "as recorded")
                p.setFont(font)

    def _paint_axis(self, p: QPainter, plot: QRectF) -> None:
        pal = self.palette_
        p.setClipping(False)
        top = plot.height()
        p.fillRect(QRectF(0, top, self.width(), self.axis_height), QColor(pal.background))
        p.setPen(QPen(QColor(pal.muted), 1))
        p.drawLine(QPointF(self.gutter, top + 0.5), QPointF(self.width(), top + 0.5))
        fm = p.fontMetrics()
        ticks, step = self._ticks()
        multi_day = step >= 86_400_000_000
        last_right = -1e9
        for t in ticks:
            x = self.x_of(t)
            p.drawLine(QPointF(x, top), QPointF(x, top + 4))
            label = timefmt.tick_label(t, step, self.zone, with_date=False)
            if (
                step < 86_400_000_000
                and timefmt.to_datetime(t, self.zone).time().replace(microsecond=0).isoformat()
                == "00:00:00"
            ):
                label = timefmt.tick_label(t, step, self.zone, with_date=True)
            tw = fm.horizontalAdvance(label)
            if x - tw / 2 > last_right + 6 and x - tw / 2 >= self.gutter:
                p.setPen(QColor(pal.text))
                p.drawText(QPointF(x - tw / 2, top + 4 + fm.ascent()), label)
                p.setPen(QPen(QColor(pal.muted), 1))
                last_right = x + tw / 2
        p.setPen(QColor(pal.muted))
        zone = timefmt.zone_label(self.zone)
        left_text = (
            f"{timefmt.format_time(int(self.t0), self.zone)[:10]} ({zone})"
            if not multi_day
            else zone
        )
        p.drawText(QPointF(4, top + 4 + fm.ascent()), left_text)
        if self.overview_text:
            p.setPen(QColor(pal.text))
            text = self.overview_text
            tw = fm.horizontalAdvance(text)
            p.drawText(QPointF(self.width() - tw - 6, top + 4 + 2 * fm.ascent() - 2), text)
        if self.edges_note:
            p.setPen(QColor(pal.muted))
            p.drawText(QPointF(self.gutter + 6, top + 4 + 2 * fm.ascent() - 2), self.edges_note)

    # -- the overlay -----------------------------------------------------------------

    def _paint_overlay(self, p: QPainter) -> None:
        pal = self.palette_
        plot = self.plot_rect()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._pointer is not None and plot.contains(self._pointer):
            x = self._pointer.x()
            p.setPen(QPen(QColor(pal.cursor), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, 0), QPointF(x, plot.height()))
            label = timefmt.format_time(int(self.t_of(x)), self.zone, 3)[11:]
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(label) + 8
            bx = min(max(x + 6, plot.left()), plot.right() - tw)
            box = QRectF(bx, 2, tw, fm.height() + 2)
            p.setPen(Qt.PenStyle.NoPen)
            fill = QColor(pal.background)
            fill.setAlpha(225)
            p.setBrush(fill)
            p.drawRoundedRect(box, 3, 3)
            p.setPen(QColor(pal.text))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        if self._measure_from is not None and self._pointer is not None:
            a, b = self._measure_from, self._pointer
            row = self.row_at(a.y())
            top = 0.0 if row is None else self.row_y(row) - self.row_height / 2
            h = plot.height() if row is None else self.row_height
            sel = QColor(pal.selection)
            sel.setAlpha(50)
            rect = QRectF(min(a.x(), b.x()), top, abs(b.x() - a.x()), h)
            p.setPen(QPen(QColor(pal.selection), 1))
            p.setBrush(sel)
            p.drawRect(rect)
            if self.measure_text:
                fm = p.fontMetrics()
                tw = fm.horizontalAdvance(self.measure_text) + 10
                bx = min(max(rect.left(), plot.left()), plot.right() - tw)
                tb = QRectF(bx, max(2, rect.top() - fm.height() - 4), tw, fm.height() + 4)
                fill = QColor(pal.background)
                fill.setAlpha(235)
                p.setBrush(fill)
                p.setPen(QPen(QColor(pal.selection), 1))
                p.drawRoundedRect(tb, 3, 3)
                p.setPen(QColor(pal.text))
                p.drawText(tb, Qt.AlignmentFlag.AlignCenter, self.measure_text)
        if self.loading:
            c = plot.center()
            r = min(plot.width(), plot.height()) * 0.05 + 10
            p.setBrush(Qt.BrushStyle.NoBrush)
            for k in range(12):
                alpha = int(255 * (((k - self._spin) % 12) / 12.0))
                col = QColor(pal.text)
                col.setAlpha(max(25, alpha))
                p.setPen(QPen(col, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                ang = 2 * math.pi * k / 12
                p.drawLine(
                    QPointF(c.x() + r * 0.6 * math.cos(ang), c.y() + r * 0.6 * math.sin(ang)),
                    QPointF(c.x() + r * math.cos(ang), c.y() + r * math.sin(ang)),
                )
            p.setPen(QColor(pal.muted))
            p.drawText(QPointF(c.x() - 30, c.y() + r + p.fontMetrics().height()), "Loading…")

    # -- measurement ------------------------------------------------------------------------

    def measurement(self) -> str:
        """Δt, 1/Δt and the minimum and maximum of the row where the box
        began, from the drawn pieces (so spikes are exact)."""
        if self._measure_from is None or self._pointer is None:
            return ""
        a, b = self._measure_from, self._pointer
        ta, tb = self.t_of(a.x()), self.t_of(b.x())
        row = self.row_at(a.y())
        lo = hi = None
        units = ""
        if row is not None and self.rows[row].piece is not None:
            r = self.rows[row]
            mm = r.piece.minmax(r.local, ta, tb)
            if mm is not None:
                lo, hi = mm
            units = r.units
        return format_measure(tb - ta, lo, hi, units)

    # -- mouse ------------------------------------------------------------------------------------

    def _t_at(self, pos: QPointF) -> int | None:
        return int(self.t_of(pos.x())) if self.plot_rect().contains(pos) else None

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        self._pointer = pos
        if self._measure_from is not None:
            self.measure_text = self.measurement()
        elif self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            start, t_per_px, rows0 = self._drag
            dx = pos.x() - start.x()
            drow = int(round(-(pos.y() - start.y()) / self.row_height))
            self.panned.emit(-dx * t_per_px, rows0 + drow)
        row = self.row_at(pos.y()) if self.plot_rect().contains(pos) else None
        self.pointer_changed.emit(self._t_at(pos), row)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._pointer = None
        self.pointer_changed.emit(None, None)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus()
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton and self.plot_rect().contains(pos):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._measure_from = pos
                self._pointer = pos
                self.measure_text = self.measurement()
            else:
                span = (self.t1 - self.t0) / self.plot_rect().width()
                self._drag = (pos, span, self.row_offset)
        elif event.button() == Qt.MouseButton.RightButton:
            row = self.row_at(pos.y())
            self.context_requested.emit(
                -1 if row is None else row,
                int(self.t_of(pos.x())),
                event.globalPosition().toPoint(),
                pos.x() < self.gutter,
            )

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self._drag is not None:
                self._drag = None
                self.pan_finished.emit()
            if self._measure_from is not None:
                self._measure_from = None
                self.measure_text = ""
                self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton and self.plot_rect().contains(pos):
            self.centre_requested.emit(int(self.t_of(pos.x())))

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta()
        steps = (delta.y() or delta.x()) / 120.0
        if steps == 0:
            return
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            frac = (event.position().x() - self.gutter) / self.plot_rect().width()
            self.wheel_zoom.emit(steps, min(1.0, max(0.0, frac)))
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            self.wheel_rows.emit(steps)
        else:
            self.wheel_time.emit(steps)
        event.accept()

    def clear_selection(self) -> None:
        self._measure_from = None
        self.measure_text = ""
        self.update()


@dataclass
class _RowShape:
    #: (x pixels, y in microvolts, dimmed) per polyline
    segments: list
    #: pixel ranges without data
    gaps: list


def _columns_shape(mn, mx, dimmed) -> _RowShape:
    """Min/max per pixel column as polylines that alternate lo-hi / hi-lo
    so the path does not zigzag across the column."""
    ok = np.isfinite(mn) & np.isfinite(mx)
    segments = []
    for a, b in _runs(ok):
        lo, hi = mn[a:b], mx[a:b]
        flip = (np.arange(b - a) % 2) == 1
        ys = np.empty(2 * (b - a))
        ys[0::2] = np.where(flip, hi, lo)
        ys[1::2] = np.where(flip, lo, hi)
        xs = np.repeat(np.arange(a, b) + 0.5, 2)
        segments.append((xs, ys, bool(dimmed[a:b].mean() > 0.5)))
    return _RowShape(segments, _runs(~ok))


def _exact_shape(x, y, um, dx, w) -> _RowShape:
    """Every sample, joined; a gap is a run of NaN samples (shaded from the
    last sample before it to the first after it)."""
    ok = np.isfinite(y)
    segments = []
    for a, b in _runs(ok):
        # split further where the filter state changes
        flags = um[a:b]
        cuts = np.flatnonzero(np.diff(flags.astype(np.int8))) + 1
        bounds = [0, *cuts.tolist(), b - a]
        for c0, c1 in zip(bounds[:-1], bounds[1:], strict=True):
            lo = max(a + c0 - 1, a) if c0 else a  # overlap by one sample: no visible break
            segments.append((x[lo : a + c1], y[lo : a + c1], bool(flags[c0])))
    gaps = []
    for a, b in _runs(~ok):
        left = x[a - 1] if a > 0 else x[a] - dx
        right = x[b] if b < len(x) else x[b - 1] + dx
        lo, hi = max(0.0, left), min(float(w), right)
        if hi > lo:
            gaps.append((lo, hi))
    return _RowShape(segments, gaps)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Half-open index ranges of the True runs of a boolean array."""
    if not mask.any():
        return []
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.view(np.int8), [0]))))
    return list(zip(edges[0::2].tolist(), edges[1::2].tolist(), strict=True))


__all__ = ["DrawRow", "TraceCanvas"]
