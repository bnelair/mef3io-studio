"""Which recording is which: the band above the traces and the banner that
says when the recordings on the page change (docs/design/viewer.md,
"Knowing which recording is which").

Both show letters, never names, unless the caller's label function adds the
name of a recording that is shown (the privacy mask is the caller's).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QSizePolicy, QWidget

from ..model.viewer_view import Palette

BAND_HEIGHT = 18
#: How long the banner stays, and how long it takes to fade (milliseconds).
BANNER_SHOW_MS = 4000
BANNER_FADE_MS = 800


def readable_on(colour: str) -> QColor:
    """Black or white text, whichever reads better on ``colour``."""
    c = QColor(colour)
    luminance = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
    return QColor("#111111" if luminance > 150 else "#ffffff")


class RecordingBand(QWidget):
    """A thin bar above the traces: for each stretch of the page, the
    recordings that hold data there, lettered and coloured (side by side
    when several do). Stretches with no data are hatched."""

    def __init__(
        self,
        palette: Palette,
        colour_of: Callable[[int], str],
        letter_of: Callable[[int], str],
        label_of: Callable[[int], str],
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("RecordingBand")
        self.setFixedHeight(BAND_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.palette_ = palette
        self._colour_of, self._letter_of, self._label_of = colour_of, letter_of, label_of
        self.t0, self.t1, self.gutter = 0, 1, 84
        #: ``(a, b, recordings)`` pieces of the page, for drawing and tests.
        self.spans: list[tuple[int, int, tuple[int, ...]]] = []

    def set_palette(self, palette: Palette) -> None:
        self.palette_ = palette
        self.update()

    def set_view(self, t0: int, t1: int, gutter: int, spans) -> None:
        self.t0, self.t1, self.gutter = t0, max(t1, t0 + 1), gutter
        self.spans = list(spans)
        self.update()

    def letters(self) -> list[list[str]]:
        """The letters of each piece (empty = no data), for tests."""
        return [[self._letter_of(i) for i in who] for _, _, who in self.spans]

    def x_of(self, t: float) -> float:
        return self.gutter + (t - self.t0) / (self.t1 - self.t0) * max(
            1, self.width() - self.gutter
        )

    def _who_at(self, x: float) -> tuple[int, ...]:
        for a, b, who in self.spans:
            x0, x1 = self.x_of(a), self.x_of(b)
            if x0 <= x < x1 and who:
                k = min(len(who) - 1, int((x - x0) / max(1e-9, x1 - x0) * len(who)))
                return (who[k],)
        return ()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        who = self._who_at(event.position().x())
        self.setToolTip(self._label_of(who[0]) if who else "")

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = self.palette_
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(pal.background))
        small = QFont(self.font())
        small.setPointSizeF(max(6.0, small.pointSizeF() * 0.85))
        p.setFont(small)
        fm = p.fontMetrics()
        p.setPen(QColor(pal.muted))
        p.drawText(QPointF(4, (self.height() + fm.ascent() - fm.descent()) / 2), "Recording")
        bar = QRectF(self.gutter, 2, max(1, self.width() - self.gutter), self.height() - 4)
        p.fillRect(bar, QColor(pal.gap))
        for a, b, who in self.spans:
            x0, x1 = self.x_of(a), self.x_of(b)
            rect = QRectF(x0, bar.top(), max(1.0, x1 - x0), bar.height())
            if not who:
                p.setPen(QPen(QColor(pal.muted), 1))
                for x in range(int(x0) - 14, int(x1), 6):
                    p.drawLine(
                        QPointF(max(x0, x), bar.bottom()), QPointF(min(x1, x + 14), bar.top())
                    )
                continue
            part = rect.width() / len(who)
            for k, i in enumerate(who):
                cell = QRectF(rect.left() + k * part, rect.top(), part, rect.height())
                colour = self._colour_of(i)
                p.fillRect(cell, QColor(colour))
                letter = self._letter_of(i)
                if cell.width() >= fm.horizontalAdvance(letter) + 6:
                    p.setPen(readable_on(colour))
                    p.drawText(cell, Qt.AlignmentFlag.AlignCenter, letter)
        p.end()


class Banner(QLabel):
    """ "Now showing recording B": over the top of the traces, fades after a
    few seconds, and is announced to screen readers."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("ViewerBanner")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Recording change")
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(BANNER_FADE_MS)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self.hide)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade.start)
        #: Every text announced, newest last (for tests).
        self.history: list[str] = []
        self.hide()

    def set_palette(self, palette: Palette) -> None:
        bg = QColor(palette.background)
        self.setStyleSheet(
            "QLabel#ViewerBanner {"
            f"background: rgba({bg.red()}, {bg.green()}, {bg.blue()}, 235);"
            f"color: {palette.text}; border: 1px solid {palette.cursor};"
            "border-radius: 6px; padding: 6px 14px; font-weight: 600; }"
        )

    def announce(self, text: str, show_ms: int = BANNER_SHOW_MS) -> None:
        self.history.append(text)
        self._timer.stop()
        self._fade.stop()
        self._effect.setOpacity(1.0)
        self.setText(text)
        self.setAccessibleDescription(text)
        self.adjustSize()
        self.place()
        self.show()
        self.raise_()
        self._timer.start(show_ms)
        if QAccessible.isActive():
            QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(self, text))

    def place(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        x = max(0, (parent.width() - self.width()) // 2)
        self.move(x, int(parent.height() * 0.04))

    def current_text(self) -> str:
        return self.text() if self.isVisible() else ""
