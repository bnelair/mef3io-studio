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
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ..model import timefmt
from ..model.viewer_colours import legible_on
from ..model.viewer_view import STRIP_DATA, STRIP_GAP, Palette, format_length, recording_colour

STRIP_HEIGHT = 32
_DAY_US = 86_400_000_000


def pixel_runs(
    coverage: Sequence[tuple[int, int, int]], timeline: tuple[int, int], width: int
) -> list[tuple[int, int, int]]:
    """The data of the timeline as ``(x0, x1, recording)`` pixel runs: one run
    for each stretch of columns that hold data of one recording, at most
    ``width`` of them however many stretches there are (a gappy recording of
    days has thousands). A stretch narrower than a pixel still takes its column.
    The work is a few array operations per recording, not a loop over stretches."""
    a, b = timeline
    width = max(1, int(width))
    if not len(coverage) or b <= a:
        return []
    cov = np.asarray(coverage, dtype=np.int64).reshape(-1, 3)
    scale = width / float(b - a)
    c0 = np.clip(np.floor((cov[:, 0] - a) * scale).astype(np.int64), 0, width - 1)
    c1 = np.clip(np.ceil((cov[:, 1] - a) * scale).astype(np.int64), c0 + 1, width)
    owner = np.full(width, -1, dtype=np.int64)
    for rec in np.unique(cov[:, 2]):
        mine = cov[:, 2] == rec
        diff = np.zeros(width + 1, dtype=np.int64)
        np.add.at(diff, c0[mine], 1)
        np.add.at(diff, c1[mine], -1)
        held = np.cumsum(diff[:-1]) > 0
        owner[held & (owner < 0)] = rec
    runs = []
    edges = np.flatnonzero(np.diff(np.concatenate(([-2], owner))) != 0)
    ends = np.append(edges[1:], width)
    for x0, x1 in zip(edges.tolist(), ends.tolist(), strict=True):
        if owner[x0] >= 0:
            runs.append((x0, x1, int(owner[x0])))
    return runs


class OverviewStrip(QWidget):
    #: The user clicked or dragged: centre the page on this time (µs).
    navigate = Signal(object)

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self.setObjectName("OverviewStrip")
        self.setFixedHeight(STRIP_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
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
        #: Where the recordings' colours come from: ``(index, dark) -> colour``.
        #: The window sets the one the recording band uses.
        self.colour_of = None
        self.setToolTip("The whole timeline. Click or drag to go there.")
        self._runs: list[tuple[int, int, int]] = []
        self._runs_key: tuple | None = None
        #: The text of the readout under the pointer (also shown as a tooltip).
        self.hover_text = ""

    def _pixel_runs(self) -> list[tuple[int, int, int]]:
        key = (self.timeline, self.width(), id(self.coverage), len(self.coverage))
        if key != self._runs_key:
            self._runs = pixel_runs(self.coverage, self.timeline, self.width())
            self._runs_key = key
        return self._runs

    def readout(self, x: float) -> str:
        """What is under the pointer: the time (with its day) and whether there
        is data, or how long the gap is. Looked up in the sorted stretches."""
        import bisect

        t = self.t_of(x)
        a, _b = self.timeline
        day = (
            timefmt.to_datetime(t, self.zone).date() - timefmt.to_datetime(a, self.zone).date()
        ).days + 1
        head = f"{timefmt.format_time(t, self.zone)}  (day {day})"
        spans = sorted(self.coverage)
        starts = [s[0] for s in spans]
        i = bisect.bisect_right(starts, t)
        inside = [s for s in spans[:i] if s[1] > t]
        if inside:
            return head + "\nThere is data here."
        before = max((s[1] for s in spans[:i]), default=None)
        after = starts[i] if i < len(starts) else None
        if before is not None and after is not None:
            return head + f"\nNo data: a gap of {format_length(after - before)}."
        return head + "\nNo data here."

    def _colour_of(self, rec: int, dark: bool) -> str:
        return self.colour_of(rec) if self.colour_of else recording_colour(rec, dark)

    def set_palette(self, palette: Palette) -> None:
        self.palette_ = palette
        self.update()

    def set_timeline(self, start: int, end: int, coverage: Sequence[tuple[int, int, int]]) -> None:
        self.timeline = (start, max(end, start + 1))
        self.coverage = list(coverage)
        self.multi = len({c[2] for c in self.coverage}) > 1
        self._runs_key = None
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
        hatch = QColor(legible_on(pal, STRIP_GAP[shade]))
        p.fillRect(bar, QColor(pal.gap))
        p.setPen(QPen(hatch, 1))
        for x in range(-14, self.width(), 5):
            p.drawLine(QPointF(x, bar.bottom()), QPointF(x + 14, bar.top()))
        dark = pal.dark
        for px0, px1, rec in self._pixel_runs():
            r = QRectF(px0, bar.top(), max(1.5, px1 - px0), bar.height())
            colour = (
                self._colour_of(rec, dark) if self.multi else legible_on(pal, STRIP_DATA[shade])
            )
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
        midnights = set()
        if b - a > 2 * 3600 * 1_000_000:
            midnights = set(timefmt.axis_ticks(a, b, _DAY_US, self.zone))
        for t in sorted(set(timefmt.axis_ticks(a, b, step, self.zone)) | midnights):
            x = self.x_of(t)
            if t in midnights:  # a day begins: a line through the bar, and its date
                p.setPen(QPen(QColor(pal.text), 1))
                p.drawLine(QPointF(x, bar.top() - 1), QPointF(x, bar.bottom() + 4))
                label = timefmt.to_datetime(t, self.zone).strftime("%Y-%m-%d")
                tw = fm.horizontalAdvance(label)
                lx = min(max(x + 2, 0.0), self.width() - tw)
                if lx > last + 4:
                    p.setPen(QColor(pal.text))
                    p.drawText(QPointF(lx, self.height() - 2), label)
                    last = lx + tw
                continue
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
        fill.setAlpha(0)  # outline only: a fill would tint the recording colours underneath
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
            shade.setAlpha(40)
            p.fillRect(QRectF(x0, top, max(2.0, x1 - x0), bottom - top), shade)
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
        self.hover_text = self.readout(event.position().x())
        QToolTip.showText(event.globalPosition().toPoint(), self.hover_text, self)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.hover_text = ""
        super().leaveEvent(event)


def block_stretches(starts, ends, min_gap_us: int = 1_000_000) -> list[tuple[int, int]]:
    """The stretches with data from the blocks' own times: blocks that follow each
    other closer than the larger of ``min_gap_us`` and twice a typical block are
    one stretch. A gap inside one segment of a recording (a pause) shows here,
    which the segment times alone do not tell."""
    st = np.asarray(starts, dtype=np.int64)
    en = np.asarray(ends, dtype=np.int64)
    if st.size == 0:
        return []
    order = np.argsort(st, kind="stable")
    st, en = st[order], en[order]
    tol = max(int(min_gap_us), 2 * int(np.median(en - st)))
    latest = np.maximum.accumulate(en)
    breaks = np.flatnonzero(st[1:] - latest[:-1] > tol) + 1
    first = np.concatenate(([0], breaks))
    last = np.concatenate((breaks - 1, [st.size - 1]))
    return [
        (int(st[f]), int(latest[k])) for f, k in zip(first.tolist(), last.tolist(), strict=True)
    ]


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
