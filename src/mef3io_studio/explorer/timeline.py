"""The timeline: each channel as a bar over time, showing its parts and the
gaps between them.

Drawn from each part's start and end time and sample count only (what
``Reader.segments()`` returns from the recording's details): no signal and
no block table is read. A part whose samples cover less time than it spans
has gaps inside it; those are drawn hatched, because where exactly they are
is only in the block table (the detailed coverage view is a next direction).
"""

from __future__ import annotations

from datetime import UTC, tzinfo

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ..common.theme import LIGHT, Theme
from ..model.recording import RecordingDetails
from ..model.timefmt import format_span, format_time


class TimelineWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.details: RecordingDetails | None = None
        self.zone: tzinfo = UTC
        self.theme: Theme = LIGHT
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        #: (area, channel index, part index) of every drawn part, for tooltips.
        self._hits: list[tuple[QRectF, int, int]] = []

    # -- geometry -----------------------------------------------------------
    def _metrics(self):
        em = self.fontMetrics().height()
        row = round(em * 1.5)
        gutter = (
            max(
                [self.fontMetrics().horizontalAdvance(c.name) for c in self.details.channels] or [0]
            )
            + em
            if self.details
            else em
        )
        gutter = min(gutter, round(em * 12))
        axis = round(em * 2.6)
        return em, row, gutter, axis

    def sizeHint(self) -> QSize:  # noqa: N802
        em, row, _gutter, axis = self._metrics()
        rows = len(self.details.channels) if self.details else 0
        return QSize(em * 30, axis + rows * row + em)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def set_details(self, details: RecordingDetails | None, zone: tzinfo) -> None:
        self.details = details
        self.zone = zone
        self.updateGeometry()
        self.setMinimumHeight(self.sizeHint().height())
        self.update()

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.update()

    # -- painting -----------------------------------------------------------
    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = self.theme
        self._hits = []
        details = self.details
        if details is None or details.start is None or not details.channels:
            painter.end()
            return
        em, row, gutter, axis = self._metrics()
        left, right = gutter, self.width() - em
        width = max(1.0, right - left)
        t0, t1 = details.start, max(details.end, details.start + 1)

        def x_of(t: int) -> float:
            return left + width * (t - t0) / (t1 - t0)

        text = QColor(theme.text)
        muted = QColor(theme.muted)
        fill = QColor(theme.accent)
        track = QColor(theme.divider)
        painter.setPen(muted)
        # The axis: start and end, plus a few ticks between.
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
        tick_pen = QPen(QColor(theme.border))
        tick_pen.setWidthF(1.0)
        painter.setPen(tick_pen)
        base = em * 1.6
        painter.drawLine(round(left), round(base), round(right), round(base))
        for i in range(5):
            x = left + width * i / 4
            painter.drawLine(round(x), round(base - em * 0.3), round(x), round(base + em * 0.3))
        painter.setPen(muted)
        span = format_span((t1 - t0) / 1e6)
        painter.drawText(
            QRectF(left, base, width, em * 1.0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            f"{span} in total",
        )

        hatch = QBrush(fill, Qt.BrushStyle.BDiagPattern)
        for i, channel in enumerate(details.channels):
            y = axis + i * row
            bar = QRectF(left, y + row * 0.2, width, row * 0.6)
            painter.setPen(text)
            painter.drawText(
                QRectF(0, y, gutter - em * 0.5, row),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                self.fontMetrics().elidedText(
                    channel.name, Qt.TextElideMode.ElideRight, int(gutter - em * 0.5)
                ),
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(track)
            painter.drawRoundedRect(bar, 3, 3)
            for j, part in enumerate(channel.parts):
                x0, x1 = x_of(part.start), x_of(part.end)
                rect = QRectF(x0, bar.top(), max(1.5, x1 - x0), bar.height())
                missing = part.missing_seconds(channel.sampling_frequency)
                painter.setBrush(hatch if missing else fill)
                if missing:
                    painter.setPen(QPen(fill, 1))
                else:
                    painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRect(rect)
                self._hits.append((rect.adjusted(-2, -2, 2, 2), i, j))
        painter.end()

    def tooltip_at(self, channel_index: int, part_index: int) -> str:
        channel = self.details.channels[channel_index]
        part = channel.parts[part_index]
        tip = (
            f"{channel.name}, part {part.number + 1}\n"
            f"{format_time(part.start, self.zone)} to {format_time(part.end, self.zone)}\n"
            f"{format_span(part.seconds)}"
        )
        missing = part.missing_seconds(channel.sampling_frequency)
        if missing:
            tip += f", with about {format_span(missing)} of gaps inside"
        return tip

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        for rect, i, j in reversed(self._hits):
            if rect.contains(pos):
                QToolTip.showText(event.globalPosition().toPoint(), self.tooltip_at(i, j), self)
                return
        QToolTip.hideText()
