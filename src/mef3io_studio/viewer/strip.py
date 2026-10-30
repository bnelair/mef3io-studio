"""The overview strip (32 px): the whole timeline at a glance.

Each recording's data is drawn in that recording's colour (from the segment
coverage, metadata only), gaps hatched, the current page as a box, day and
time marks. Click or drag to go there. Optionally a faint envelope of one
channel on top (``set_envelope``, from the block index).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, tzinfo

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..model import timefmt
from ..model.viewer_view import STRIP_DATA, STRIP_GAP, Palette, recording_colour

STRIP_HEIGHT = 32


class OverviewStrip(QWidget):
    #: The user clicked or dragged: centre the page on this time (µs).
    navigate = Signal(object)

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self.setObjectName("OverviewStrip")
        self.setFixedHeight(STRIP_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(False)
        self.palette_ = palette
        self.zone: tzinfo = UTC
        self.timeline = (0, 1)
        self.coverage: list[tuple[int, int, int]] = []
        self.page = (0, 1)
        self.multi = False
        self.envelope: np.ndarray | None = None  # [2, bins]: min, max (normalised -1..1)
        #: The export range (µs, either end may be unset) and its label.
        self.export: tuple[int | None, int | None] = (None, None)
        self.export_text = ""
        self.setToolTip("The whole timeline. Click or drag to go there.")

    def set_palette(self, palette: Palette) -> None:
        self.palette_ = palette
        self.update()

    def set_timeline(self, start: int, end: int, coverage: Sequence[tuple[int, int, int]]) -> None:
        self.timeline = (start, max(end, start + 1))
        self.coverage = list(coverage)
        self.multi = len({c[2] for c in self.coverage}) > 1
        self.update()

    def set_page(self, t0: int, t1: int) -> None:
        self.page = (t0, t1)
        self.update()

    def set_export(self, start: int | None, end: int | None, text: str = "") -> None:
        self.export = (start, end)
        self.export_text = text
        self.setToolTip(
            "The whole timeline. Click or drag to go there."
            + (f"\nExport range: {text}" if text else "")
        )
        self.update()

    def set_envelope(self, env: np.ndarray | None) -> None:
        self.envelope = env
        self.update()

    def x_of(self, t: float) -> float:
        a, b = self.timeline
        return (t - a) / (b - a) * self.width()

    def t_of(self, x: float) -> int:
        a, b = self.timeline
        return int(a + min(1.0, max(0.0, x / max(1, self.width()))) * (b - a))

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = self.palette_
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(pal.background))
        bar = QRectF(0, 3, self.width(), 14)
        # gaps: hatched background over the whole timeline
        shade = 1 if pal.dark else 0
        hatch = QColor(STRIP_GAP[shade])
        p.fillRect(bar, QColor(pal.gap))
        p.setPen(QPen(hatch, 1))
        for x in range(-14, self.width(), 5):
            p.drawLine(QPointF(x, bar.bottom()), QPointF(x + 14, bar.top()))
        dark = pal.dark
        for t0, t1, rec in self.coverage:
            x0, x1 = self.x_of(t0), self.x_of(t1)
            r = QRectF(x0, bar.top(), max(1.5, x1 - x0), bar.height())
            colour = recording_colour(rec, dark) if self.multi else STRIP_DATA[shade]
            p.fillRect(r, QColor(colour))
        if self.envelope is not None and self.envelope.shape[1] > 1:
            n = self.envelope.shape[1]
            line = QColor(pal.background)
            line.setAlpha(150)
            p.setPen(QPen(line, 1))
            mid, half = bar.center().y(), bar.height() / 2 - 1
            xs = (np.arange(n) + 0.5) / n * self.width()
            for x, lo, hi in zip(xs, self.envelope[0], self.envelope[1], strict=True):
                if np.isfinite(lo) and np.isfinite(hi):
                    p.drawLine(QPointF(x, mid - hi * half), QPointF(x, mid - lo * half))
        # day / time marks
        a, b = self.timeline
        fm = p.fontMetrics()
        small = QFont(self.font())
        small.setPointSizeF(max(6.0, small.pointSizeF() * 0.78))
        p.setFont(small)
        fm = p.fontMetrics()
        step = timefmt.tick_step(
            b - a, max(2, self.width() // (fm.horizontalAdvance("0000-00-00") + 14))
        )
        last = -1e9
        for t in timefmt.axis_ticks(a, b, step, self.zone):
            x = self.x_of(t)
            p.setPen(QPen(QColor(pal.muted), 1))
            p.drawLine(QPointF(x, bar.bottom()), QPointF(x, bar.bottom() + 3))
            label = timefmt.tick_label(t, step, self.zone)
            tw = fm.horizontalAdvance(label)
            if x - tw / 2 > last + 4 and x - tw / 2 >= 0 and x + tw / 2 <= self.width():
                p.setPen(QColor(pal.muted))
                p.drawText(QPointF(x - tw / 2, self.height() - 2), label)
                last = x + tw / 2
        self._paint_export(p, bar)
        # the page
        x0, x1 = self.x_of(self.page[0]), self.x_of(self.page[1])
        box = QRectF(x0, 1.5, max(4.0, x1 - x0), bar.bottom() + 1)  # taller than the bar
        fill = QColor(pal.cursor)
        fill.setAlpha(60)
        p.setBrush(fill)
        p.setPen(QPen(QColor("#000000"), 4))  # dark outside, white inside, then the cursor colour
        p.drawRect(box)
        p.setPen(QPen(QColor("#ffffff"), 2.5))
        p.drawRect(box)
        p.setPen(QPen(QColor(pal.cursor), 1))
        p.drawRect(box)
        p.end()

    def _paint_export(self, p: QPainter, bar: QRectF) -> None:
        a, b = self.export
        if a is None and b is None:
            return
        pal = self.palette_
        ink = QColor("#ffffff" if pal.dark else "#111827")
        top, bottom = bar.top() - 2, bar.bottom() + 2
        if a is not None and b is not None:
            x0, x1 = self.x_of(a), self.x_of(b)
            shade = QColor(ink)
            shade.setAlpha(60)
            p.fillRect(QRectF(x0, top, max(2.0, x1 - x0), bottom - top), shade)
            if self.export_text:
                fm = p.fontMetrics()
                small = QFont(self.font())
                small.setPointSizeF(max(6.0, small.pointSizeF() * 0.78))
                p.setFont(small)
                fm = p.fontMetrics()
                tw = fm.horizontalAdvance(self.export_text) + 8
                bx = x1 + 3 if x1 + 3 + tw < self.width() else max(0.0, x0 - 3 - tw)
                box = QRectF(bx, top, tw, bottom - top)
                fill = QColor(pal.background)
                fill.setAlpha(235)
                p.fillRect(box, fill)
                p.setPen(QColor(pal.text))
                p.drawText(box, Qt.AlignmentFlag.AlignCenter, self.export_text)
        p.setPen(QPen(ink, 2))
        for t in (a, b):
            if t is not None:
                x = self.x_of(t)
                p.drawLine(QPointF(x, top), QPointF(x, bottom))

    def _go(self, event) -> None:
        self.navigate.emit(self.t_of(event.position().x()))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._go(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._go(event)


def envelope_bins(starts, ends, mins, maxs, timeline: tuple[int, int], bins: int) -> np.ndarray:
    """Reduce block extremes to ``bins`` columns over the timeline and
    normalise to -1..1 by the 99th percentile of the magnitudes."""
    a, b = timeline
    mn = np.full(bins, np.nan)
    mx = np.full(bins, np.nan)
    if len(starts):
        starts = np.asarray(starts, dtype=np.float64)
        c = np.clip(((starts - a) / (b - a) * bins).astype(np.int64), 0, bins - 1)
        ok = np.isfinite(mins) & np.isfinite(maxs)
        np.fmin.at(mn, c[ok], np.asarray(mins)[ok])
        np.fmax.at(mx, c[ok], np.asarray(maxs)[ok])
    mags = np.abs(np.concatenate([mn[np.isfinite(mn)], mx[np.isfinite(mx)]]))
    scale = float(np.percentile(mags, 99)) if mags.size else 1.0
    scale = scale or 1.0
    return np.clip(np.vstack([mn, mx]) / scale, -1.0, 1.0)
