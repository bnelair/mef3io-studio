"""The map of "Join or trim -> What to keep": Explore's combined timeline (one
row per recording, when each has data, zoom and pan), with two lines on it,
**Start** and **End**, that the user drags (or moves with the keyboard) to
choose the window to keep, and with what each recording keeps shaded.

It IS the Explore widget (:class:`~..explorer.combined.CombinedTimelineWidget`)
with an overlay painted on top, so rows, gaps, the axis, zoom, pan and the
tooltips are drawn by the same code. Qt only: the window's rules (what a
recording keeps inside it, which ones are left out) are ``model.join``'s.

Keys (the widget must have the focus): **S** / **E** pick the Start / End
line, **Left** / **Right** then move it by one pixel (**Shift**: ten pixels,
**Ctrl**: one second), **Home** / **End** put it at the first / last moment,
**Esc** lets go (the arrow keys then move the view again, as in Explore).
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen

from ..explorer.combined import CombinedTimelineWidget
from ..model.timefmt import format_time

START, END = "start", "end"
LABELS = {START: "Start", END: "End"}

#: How close (pixels) the pointer must be to a line to take hold of it, and
#: how close to the first or last moment a dragged line sticks to it.
GRAB_PIXELS = 7
SNAP_PIXELS = 4
ONE_SECOND = 1_000_000

HELP = (
    "Map of when each recording has data. Press S or E to pick the Start or End line, "
    "then the left and right arrow keys to move it."
)


class TrimMapWidget(CombinedTimelineWidget):
    """The combined timeline with a draggable Start and End line.

    ``window_moved(start, end)`` is emitted when the USER moves a line
    (mouse or keys); :meth:`set_window` (the fields, the model) is silent."""

    window_moved = Signal(object, object)
    active_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bounds: tuple[int, int] = (0, 1)
        self.start = 0
        self.end = 1
        #: Whether the two lines are shown and can be moved.
        self.lines_enabled = False
        #: key -> (start, end) kept of that recording, or None to dim it all.
        self.kept: dict[str, tuple[int, int] | None] = {}
        #: Rows whose recording is left out (drawn greyed, with the reason).
        self.left_out: dict[str, str] = {}
        self.active: str | None = None
        self._line_drag: str | None = None
        self.setAccessibleName(HELP)
        self.setToolTip("")

    # -- geometry: more room above the rows for the line labels ----------------
    def _metrics(self):
        em, row, gutter, _axis = super()._metrics()
        return em, row, gutter, round(em * 4.1)

    # -- state -------------------------------------------------------------------
    def set_bounds(self, lo: int, hi: int) -> None:
        self.bounds = (int(lo), max(int(hi), int(lo) + 1))

    def set_window(self, start: int, end: int) -> None:
        self.start, self.end = int(start), int(end)
        self.update()

    def set_lines_enabled(self, enabled: bool) -> None:
        self.lines_enabled = enabled
        if not enabled:
            self._set_active(None)
            self._line_drag = None
        self.update()

    def set_kept(self, kept: dict, left_out: dict[str, str]) -> None:
        self.kept = dict(kept)
        self.left_out = dict(left_out)
        self.update()

    def _set_active(self, which: str | None) -> None:
        if which != self.active:
            self.active = which
            self.active_changed.emit(which or "")
            self.update()

    # -- moving a line ---------------------------------------------------------------
    def x_of(self, t: int) -> float:
        plot = self.plot_rect()
        return plot.left() + (t - self.v0) * plot.width() / max(1, self.v1 - self.v0)

    def move_line(self, which: str, t: int) -> bool:
        """Put a line at ``t``, kept inside the recordings' span and on its
        own side of the other line (Start < End). True if it moved."""
        lo, hi = self.bounds
        t = int(t)
        if which == START:
            new = min(max(t, lo), self.end - 1)
            if new == self.start:
                return False
            self.start = new
        else:
            new = max(min(t, hi), self.start + 1)
            if new == self.end:
                return False
            self.end = new
        self.update()
        self.window_moved.emit(self.start, self.end)
        return True

    def _snapped(self, which: str, x: float) -> int:
        """The time under pixel ``x``; at the first or last moment of the
        recordings when within a few pixels of it."""
        t = self.time_at(x)
        lo, hi = self.bounds
        for edge in (lo, hi):
            if abs(self.x_of(edge) - x) <= SNAP_PIXELS:
                return edge
        return t

    def line_at(self, x: float) -> str | None:
        """The line within reach of pixel ``x`` (the nearer one if both)."""
        if not self.lines_enabled:
            return None
        near = [
            (abs(self.x_of(t) - x), which)
            for which, t in ((START, self.start), (END, self.end))
            if abs(self.x_of(t) - x) <= GRAB_PIXELS
        ]
        if not near:
            return None
        near.sort()
        if len(near) == 2 and near[0][0] == near[1][0]:
            return START if x <= self.x_of(self.start) else END
        return near[0][1]

    # -- mouse -----------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            which = self.line_at(event.position().x())
            if which is not None:
                self._line_drag = which
                self._set_active(which)
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        if self._line_drag is not None:
            if event.buttons() & Qt.MouseButton.LeftButton:
                self.move_line(self._line_drag, self._snapped(self._line_drag, pos.x()))
            event.accept()
            return
        if self._drag is None:
            if self.line_at(pos.x()) is not None:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._line_drag is not None:
            self._line_drag = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # -- keys ------------------------------------------------------------------------
    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if self.lines_enabled:
            if key == Qt.Key.Key_S:
                self._set_active(START)
                event.accept()
                return
            if key == Qt.Key.Key_E:
                self._set_active(END)
                event.accept()
                return
            if self.active is not None:
                if key == Qt.Key.Key_Escape:
                    self._set_active(None)
                    event.accept()
                    return
                if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                    mods = event.modifiers()
                    if mods & Qt.KeyboardModifier.ControlModifier:
                        step = ONE_SECOND
                    else:
                        per_pixel = max(
                            1, (self.v1 - self.v0) // max(1, int(self.plot_rect().width()))
                        )
                        step = per_pixel * (10 if mods & Qt.KeyboardModifier.ShiftModifier else 1)
                    now = self.start if self.active == START else self.end
                    self.move_line(self.active, now + (step if key == Qt.Key.Key_Right else -step))
                    event.accept()
                    return
                if key in (Qt.Key.Key_Home, Qt.Key.Key_End):
                    self.move_line(
                        self.active, self.bounds[0] if key == Qt.Key.Key_Home else self.bounds[1]
                    )
                    event.accept()
                    return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.update()

    # -- painting --------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        layout = self.layout_data
        if layout is None or not layout.rows:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = self.theme
        em, row_h, gutter, axis = self._metrics()
        plot = self.plot_rect()
        first = self.scrollbar.value()
        shade = QColor(theme.surface)
        shade.setAlphaF(0.72)
        painter.setClipRect(QRectF(0, axis, self.width(), self.height() - axis))
        for n, row in enumerate(layout.rows[first : first + self.visible_rows() + 1]):
            y = axis + n * row_h
            band = QRectF(0, y, plot.right(), row_h)
            if row.key in self.left_out:
                painter.fillRect(band, shade)
                painter.setPen(QColor(theme.text))
                painter.drawText(
                    QRectF(plot.left() + em * 0.3, y, plot.width() - em * 0.6, row_h),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    self.fontMetrics().elidedText(
                        self.left_out[row.key],
                        Qt.TextElideMode.ElideRight,
                        int(plot.width() - em * 0.6),
                    ),
                )
                continue
            if row.key not in self.kept:
                continue
            kept = self.kept[row.key]
            bar_top, bar_h = y + row_h * 0.12, row_h * 0.76
            if kept is None:
                continue
            x0 = min(max(self.x_of(kept[0]), plot.left()), plot.right())
            x1 = min(max(self.x_of(kept[1]), plot.left()), plot.right())
            # What is not kept is dimmed; what is kept is framed.
            painter.fillRect(QRectF(plot.left(), y, max(0.0, x0 - plot.left()), row_h), shade)
            painter.fillRect(QRectF(x1, y, max(0.0, plot.right() - x1), row_h), shade)
            if x1 > x0:
                painter.setPen(QPen(QColor(theme.text), 1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(QRectF(x0, bar_top, x1 - x0, bar_h))
        painter.setClipping(False)
        if self.lines_enabled:
            for which, t in ((START, self.start), (END, self.end)):
                self._paint_line(painter, which, t, em, axis, plot)
        painter.end()

    def _paint_line(self, painter, which, t, em, axis, plot) -> None:
        theme = self.theme
        active = which == self.active
        x = self.x_of(t)
        inside = plot.left() - 0.5 <= x <= plot.right() + 0.5
        color = QColor(theme.accent if active else theme.text)
        text = LABELS[which]
        fm = self.fontMetrics()
        if not inside:
            text = ("◀ " + text) if x < plot.left() else (text + " ▶")
            x = plot.left() if x < plot.left() else plot.right()
        flag_w = fm.horizontalAdvance(text) + em * 0.8
        flag_h = em * 1.15
        top = axis - flag_h - em * 0.1
        # The Start flag hangs to the right of its line, the End flag to the left.
        left = x if which == START else x - flag_w
        left = min(max(left, plot.left() - em * 0.2), self.width() - flag_w - 1)
        pen = QPen(color, 3 if active else 2)
        if inside:
            painter.setPen(pen)
            painter.drawLine(QPointF(x, top), QPointF(x, self.height()))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(left, top, flag_w, flag_h), 3, 3)
        painter.setPen(QColor(theme.accent_text if active else theme.surface))
        painter.drawText(QRectF(left, top, flag_w, flag_h), Qt.AlignmentFlag.AlignCenter, text)
        if active and self.hasFocus():
            painter.setPen(QPen(QColor(theme.text), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(left - 2, top - 2, flag_w + 4, flag_h + 4), 4, 4)

    # -- text for the tooltip over a line ------------------------------------------------
    def tooltip_at(self, pos: QPointF) -> str:
        which = self.line_at(pos.x())
        if which is not None:
            t = self.start if which == START else self.end
            return f"{LABELS[which]}: {format_time(t, self.zone, 6)}\nDrag to move it."
        return super().tooltip_at(pos)
