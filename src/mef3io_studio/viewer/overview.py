"""The overview band: the highest and lowest value of the whole recording for
the chosen channels, drawn right above the overview strip, to find one's way
in time before the slow reading of a page. Index only (``block_envelope``).

Each channel is drawn in its own lane, scaled to its own range. Stretches
without data stay empty (gaps). The page marker continues from the strip up
over the band; a click or drag goes there, exactly as on the strip
(:class:`~.strip.TimelineNav`).
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy

from ..model.viewer_view import Palette
from ..workers.viewer_pipeline import envelope_columns
from .strip import TimelineNav

#: Columns kept per channel for the whole timeline (drawing reduces them to
#: the band's width).
OVERVIEW_BINS = 4096
LANE_HEIGHT = 22
#: The most channels in the band (more would not stay readable).
MAX_LANES = 4


def summarise(e: dict, timeline: tuple[int, int], bins: int = OVERVIEW_BINS):
    """One channel's ``block_envelope`` result as ``(min, max, scale)``:
    ``bins`` columns over the timeline (NaN = no data) and the channel's own
    range, the 99th percentile of the magnitudes (never 0)."""
    row = np.vstack(
        [
            np.asarray(e["start_uutc"], dtype=np.float64),
            np.asarray(e["end_uutc"], dtype=np.float64),
            np.asarray(e["min"], dtype=np.float64),
            np.asarray(e["max"], dtype=np.float64),
        ]
    )
    mn, mx = envelope_columns(row, timeline[0], timeline[1], bins)
    mags = np.abs(np.concatenate([mn[np.isfinite(mn)], mx[np.isfinite(mx)]]))
    scale = float(np.percentile(mags, 99)) if mags.size else 1.0
    return mn, mx, (scale or 1.0)


def reduce_columns(mn: np.ndarray, mx: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """``n`` drawing columns from the stored ones: the lowest of the lows and
    the highest of the highs that fall in each column."""
    m = len(mn)
    if n <= 0:
        return mn[:0], mx[:0]
    if n >= m:
        idx = np.arange(n) * m // n
        return mn[idx], mx[idx]
    col = np.arange(m) * n // m
    lo = np.full(n, np.nan)
    hi = np.full(n, np.nan)
    ok = np.isfinite(mn) & np.isfinite(mx)
    np.fmin.at(lo, col[ok], mn[ok])
    np.fmax.at(hi, col[ok], mx[ok])
    return lo, hi


class OverviewBand(TimelineNav):
    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self.setObjectName("OverviewBand")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.palette_ = palette
        self.page = (0, 1)
        self.channels: list[str] = []
        #: channel -> (min, max, scale) over the timeline
        self.data: dict[str, tuple[np.ndarray, np.ndarray, float]] = {}
        self.failed = False
        self.setToolTip(
            "The whole recording at a glance: the highest and lowest value of each\n"
            "stretch, read without loading the signals. Click or drag to go there."
        )
        self.set_channels([])

    def set_palette(self, palette: Palette) -> None:
        self.palette_ = palette
        self.update()

    def set_timeline(self, start: int, end: int) -> None:
        self.timeline = (start, max(end, start + 1))
        self.update()

    def set_page(self, t0: int, t1: int) -> None:
        self.page = (t0, t1)
        self.update()

    def set_channels(self, channels: list[str]) -> None:
        self.channels = list(channels)[:MAX_LANES]
        self.setFixedHeight(max(1, len(self.channels)) * LANE_HEIGHT + 2)
        self.update()

    def set_data(self, data: dict, failed: bool = False) -> None:
        self.data = data
        self.failed = failed
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = self.palette_
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(pal.background))
        small = QFont(self.font())
        small.setPointSizeF(max(6.0, small.pointSizeF() * 0.78))
        p.setFont(small)
        w = self.width()
        for i, ch in enumerate(self.channels):
            lane = QRectF(0, 1 + i * LANE_HEIGHT, w, LANE_HEIGHT)
            p.fillRect(lane.adjusted(0, 1, 0, -1), QColor(pal.gap))
            entry = self.data.get(ch)
            if entry is None:
                p.setPen(QColor(pal.muted))
                p.drawText(
                    lane.adjusted(6, 0, 0, 0),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    "Overview not available" if self.failed else f"Loading the overview of {ch}…",
                )
                continue
            mn, mx, scale = entry
            lo, hi = reduce_columns(mn, mx, w)
            mid, half = lane.center().y(), LANE_HEIGHT / 2 - 2
            p.setPen(QPen(QColor(pal.trace), 1))
            for x in np.nonzero(np.isfinite(lo) & np.isfinite(hi))[0]:
                y0 = mid - float(np.clip(hi[x] / scale, -1, 1)) * half
                y1 = mid - float(np.clip(lo[x] / scale, -1, 1)) * half
                p.drawLine(QPointF(x + 0.5, y0), QPointF(x + 0.5, max(y1, y0 + 1)))
            p.setPen(QColor(pal.muted))
            p.drawText(
                lane.adjusted(4, 1, 0, 0),
                int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft),
                ch,
            )
        # the page marker, continued from the strip below (same three lines)
        x0, x1 = self.x_of(self.page[0]), self.x_of(self.page[1])
        x1 = max(x1, x0 + 4.0)
        h = self.height()
        path = QPainterPath(QPointF(x0, h))
        path.lineTo(x0, 1.5)
        path.lineTo(x1, 1.5)
        path.lineTo(x1, h)
        for colour, width in (("#000000", 4), ("#ffffff", 2.5), (pal.cursor, 1)):
            p.setPen(QPen(QColor(colour), width))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
        p.end()
