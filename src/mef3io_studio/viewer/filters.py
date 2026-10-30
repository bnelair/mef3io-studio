"""The filter panel and the "Show response" plot.

The panel builds a :class:`~..model.dsp.FilterSettings` from its controls
and emits it; a ValueError from the settings (for example a high-pass at or
above the low-pass) is shown as plain text and nothing is emitted. The plot
draws exactly ``dsp.applied_response``, the function the filter multiplies
the spectrum by, so plot and filter cannot disagree.
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSlider,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from ..common.dialogs import TechnicalDetails
from ..model import dsp
from ..model.timefmt import format_rate

HP_PRESETS = (0.1, 0.5, 1, 5)
LP_PRESETS = (30, 50, 70, 100, 300, 1000)
F_MIN = 0.01
ORDERS = {"Gentle": 2, "Standard": 4, "Sharp": 8}
WIDTHS = {"Narrow": "narrow", "Normal": "normal", "Wide": "wide"}
SLIDER_STEPS = 1000
MINUS = "−"


def plain_filter_error(exc: ValueError) -> str:
    """The settings' own sentence when it is one, else a generic one."""
    text = str(exc)
    if text[:1].isupper() and text.endswith("."):
        return text
    return "These filter settings cannot be used. Check the numbers."


def technical_line(settings: dsp.FilterSettings) -> str:
    n = settings.order
    if settings.zero_phase:
        return f"Butterworth, order {n}, applied twice (as steep as order {2 * n})."
    return f"Butterworth, order {n}, applied once."


class _CutoffRow(QWidget):
    """On/off, a number field, a logarithmic slider and presets."""

    changed = Signal()

    def __init__(self, title: str, presets, default: float, parent=None):
        super().__init__(parent)
        self.on = QCheckBox(title, self)
        self.value = QDoubleSpinBox(self)
        self.value.setDecimals(3)
        self.value.setRange(F_MIN, 100000.0)
        self.value.setKeyboardTracking(False)
        self.value.setSingleStep(0.1)
        self.value.setSuffix(" Hz")
        self.value.setValue(default)
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, SLIDER_STEPS)
        self.f_max = 500.0
        self.preset_buttons: list[QPushButton] = []
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(self.on, 0, 0)
        grid.addWidget(self.value, 0, 1)
        grid.addWidget(self.slider, 0, 2)
        grid.setColumnStretch(2, 1)
        row = QHBoxLayout()
        for f in presets:
            b = QPushButton(f"{f:g}", self)
            b.setToolTip(f"Use {f:g} Hz")
            b.clicked.connect(lambda _c=False, f=f: self.choose(f))
            row.addWidget(b)
            self.preset_buttons.append(b)
        row.addStretch(1)
        grid.addLayout(row, 1, 1, 1, 2)
        self._busy = False
        self.on.toggled.connect(lambda _c: self._emit())
        self.value.valueChanged.connect(self._value_changed)
        self.slider.valueChanged.connect(self._slider_changed)
        self._sync_slider()

    def choose(self, f: float) -> None:
        self._busy = True
        self.on.setChecked(True)
        self.value.setValue(f)
        self._busy = False
        self._sync_slider()
        self._emit()

    def set_max(self, f_max: float) -> None:
        self.f_max = max(F_MIN * 10, f_max)
        self._sync_slider()

    def hz(self) -> float | None:
        return self.value.value() if self.on.isChecked() else None

    def set_hz(self, f: float | None) -> None:
        self._busy = True
        self.on.setChecked(f is not None)
        if f is not None:
            self.value.setValue(f)
        self._busy = False
        self._sync_slider()

    def _to_slider(self, f: float) -> int:
        f = min(max(f, F_MIN), self.f_max)
        return round(SLIDER_STEPS * math.log(f / F_MIN) / math.log(self.f_max / F_MIN))

    def _from_slider(self, v: int) -> float:
        return F_MIN * (self.f_max / F_MIN) ** (v / SLIDER_STEPS)

    def _sync_slider(self) -> None:
        self._busy = True
        self.slider.setValue(self._to_slider(self.value.value()))
        self._busy = False

    def _value_changed(self, _v: float) -> None:
        if self._busy:
            return
        self._sync_slider()
        self._emit()

    def _slider_changed(self, v: int) -> None:
        if self._busy:
            return
        f = self._from_slider(v)
        f = float(f"{f:.3g}")
        self._busy = True
        self.on.setChecked(True)
        self.value.setValue(f)
        self._busy = False
        self._emit()

    def _emit(self) -> None:
        if not self._busy:
            self.changed.emit()


class FilterPanel(QWidget):
    """The filter panel (key F). Emits ``changed(FilterSettings)`` for valid
    settings; invalid ones show a sentence and emit nothing."""

    changed = Signal(object)
    response_requested = Signal()

    def __init__(self, settings: dsp.FilterSettings | None = None, rates=(256.0,), parent=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle("Filters")
        self.rates = sorted({float(r) for r in rates}) or [256.0]
        self.hp = _CutoffRow("Slow-drift filter (high-pass)", HP_PRESETS, 0.5, self)
        self.lp = _CutoffRow("Fast-activity filter (low-pass)", LP_PRESETS, 70.0, self)
        self.mains = QComboBox(self)
        self.mains.addItems(["Off", "50 Hz", "60 Hz"])
        self.mains_width = QComboBox(self)
        self.mains_width.addItems(list(WIDTHS))
        self.mains_width.setCurrentText("Normal")
        self.harmonics = QCheckBox("Also remove harmonics (2×, 3×, …)", self)
        self.steepness = QButtonGroup(self)
        steep = QHBoxLayout()
        self.steepness_buttons: dict[str, QRadioButton] = {}
        for name in ORDERS:
            b = QRadioButton(name, self)
            self.steepness.addButton(b)
            steep.addWidget(b)
            self.steepness_buttons[name] = b
        self.steepness_buttons["Standard"].setChecked(True)
        steep.addStretch(1)
        self.timing = QButtonGroup(self)
        self.no_shift = QRadioButton("No time shift", self)
        self.as_recorder = QRadioButton("As the recorder shows it", self)
        self.no_shift.setChecked(True)
        self.timing.addButton(self.no_shift)
        self.timing.addButton(self.as_recorder)
        self.message = QLabel("", self)
        self.message.setObjectName("Danger")
        self.message.setWordWrap(True)
        self.notice = QLabel("", self)
        self.notice.setObjectName("Muted")
        self.notice.setWordWrap(True)
        self.summary = QLabel("", self)
        self.summary.setObjectName("Caption")
        self.show_response = QPushButton("Show response…", self)
        self.show_response.clicked.connect(self.response_requested)
        self.details = TechnicalDetails(technical_line(dsp.FilterSettings()), self)
        self.reset = QPushButton("Turn all filters off", self)
        self.reset.clicked.connect(lambda: self.set_settings(dsp.FilterSettings(), emit=True))

        box = QVBoxLayout(self)
        box.addWidget(self.hp)
        box.addWidget(self.lp)
        mains = QHBoxLayout()
        mains.addWidget(QLabel("Mains filter", self))
        mains.addWidget(self.mains)
        mains.addWidget(QLabel("Width", self))
        mains.addWidget(self.mains_width)
        mains.addStretch(1)
        box.addLayout(mains)
        box.addWidget(self.harmonics)
        box.addWidget(QLabel("Steepness", self))
        box.addLayout(steep)
        box.addWidget(QLabel("Timing", self))
        box.addWidget(self.no_shift)
        box.addWidget(self.as_recorder)
        box.addWidget(self.message)
        box.addWidget(self.notice)
        box.addWidget(self.summary)
        buttons = QHBoxLayout()
        buttons.addWidget(self.show_response)
        buttons.addWidget(self.reset)
        buttons.addStretch(1)
        box.addLayout(buttons)
        box.addWidget(self.details)
        self.setMinimumWidth(self.fontMetrics().height() * 26)
        self._busy = False
        self._update_ranges()
        for w in (self.hp, self.lp):
            w.changed.connect(self._changed)
        self.mains.currentIndexChanged.connect(self._changed)
        self.mains_width.currentIndexChanged.connect(self._changed)
        self.harmonics.toggled.connect(self._changed)
        self.steepness.buttonToggled.connect(lambda *_a: self._changed())
        self.timing.buttonToggled.connect(lambda *_a: self._changed())
        self.current: dsp.FilterSettings = dsp.FilterSettings()
        if settings is not None:
            self.set_settings(settings)

    def _update_ranges(self) -> None:
        top = dsp.NYQUIST_FRACTION * max(self.rates) / 2.0
        self.hp.set_max(top)
        self.lp.set_max(top)

    def set_rates(self, rates) -> None:
        self.rates = sorted({float(r) for r in rates}) or [256.0]
        self._update_ranges()

    def build(self) -> dsp.FilterSettings:
        """The settings the controls describe (ValueError when they are not valid)."""
        mains_text = self.mains.currentText()
        mains = None if mains_text == "Off" else float(mains_text.split()[0])
        order = next(ORDERS[n] for n, b in self.steepness_buttons.items() if b.isChecked())
        return dsp.FilterSettings(
            highpass_hz=self.hp.hz(),
            lowpass_hz=self.lp.hz(),
            mains_hz=mains,
            mains_width=WIDTHS[self.mains_width.currentText()],
            mains_harmonics=self.harmonics.isChecked(),
            order=order,
            zero_phase=self.no_shift.isChecked(),
        )

    def set_settings(self, s: dsp.FilterSettings, emit: bool = False) -> None:
        self._busy = True
        self.hp.set_hz(s.highpass_hz)
        self.lp.set_hz(s.lowpass_hz)
        self.mains.setCurrentText("Off" if s.mains_hz is None else f"{s.mains_hz:g} Hz")
        self.mains_width.setCurrentText(next(k for k, v in WIDTHS.items() if v == s.mains_width))
        self.harmonics.setChecked(s.mains_harmonics)
        for name, order in ORDERS.items():
            if order == s.order:
                self.steepness_buttons[name].setChecked(True)
        (self.no_shift if s.zero_phase else self.as_recorder).setChecked(True)
        self._busy = False
        self.current = s
        self.message.setText("")
        self.details.box.setPlainText(technical_line(s))
        self.summary.setText(s.summary())
        if emit:
            self.changed.emit(s)

    def _changed(self, *_args) -> None:
        if self._busy:
            return
        try:
            s = self.build()
        except ValueError as exc:
            self.message.setText(plain_filter_error(exc))
            return
        self.message.setText("")
        self.current = s
        self.details.box.setPlainText(technical_line(s))
        self.summary.setText(s.summary())
        self.changed.emit(s)

    def set_notice(self, text: str) -> None:
        self.notice.setText(text)


# --- the response plot -------------------------------------------------------


class ResponsePlot(QWidget):
    """Log-frequency plot of what the filters keep (or their delay)."""

    hovered = Signal(str)

    KEPT, DB, DELAY = "kept", "db", "delay"

    def __init__(self, settings: dsp.FilterSettings, fs: float, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.settings = settings
        self.fs = fs
        self.mode = self.KEPT
        self.hover_hz: float | None = None
        self.setMinimumSize(self.fontMetrics().height() * 24, self.fontMetrics().height() * 12)

    @property
    def f_max(self) -> float:
        return dsp.NYQUIST_FRACTION * self.fs / 2.0

    def curve(self, n: int = 500) -> tuple[np.ndarray, np.ndarray]:
        freqs = np.geomspace(F_MIN, self.f_max, n)
        return freqs, self._y(freqs)

    def _y(self, freqs: np.ndarray) -> np.ndarray:
        if self.mode == self.DELAY:
            return dsp.group_delay_s(self.settings, self.fs, freqs) * 1000.0
        mag = np.abs(dsp.applied_response(self.settings, self.fs, freqs))
        if self.mode == self.DB:
            return 20.0 * np.log10(np.maximum(mag, 1e-6))
        return mag * 100.0

    def readout(self, f: float) -> str:
        """ "At 2.0 Hz: 98% kept (−0.2 dB)" from the applied response."""
        f = float(min(max(f, F_MIN), self.f_max))
        h = abs(dsp.applied_response(self.settings, self.fs, [f])[0])
        db = 20.0 * math.log10(max(h, 1e-6))
        shown = f"{f:.1f}" if f >= 1 else f"{f:.2g}"
        pct = h * 100
        pct_text = "less than 0.1" if pct < 0.05 else (f"{pct:.1f}" if pct < 1 else f"{round(pct)}")
        text = f"At {shown} Hz: {pct_text}% kept ({db:+.1f} dB)".replace("-", MINUS)
        if self.mode == self.DELAY:
            d = dsp.group_delay_s(self.settings, self.fs, [f])[0] * 1000
            text = f"At {shown} Hz: shifted by {d:.1f} ms"
        return text

    def _frame(self) -> QRectF:
        fm = self.fontMetrics()
        return QRectF(
            fm.height() * 3.5,
            8,
            self.width() - fm.height() * 4.5,
            self.height() - fm.height() * 2.6,
        )

    def _x(self, f: float, r: QRectF) -> float:
        return r.left() + math.log(f / F_MIN) / math.log(self.f_max / F_MIN) * r.width()

    def _f_at(self, x: float, r: QRectF) -> float:
        return F_MIN * (self.f_max / F_MIN) ** ((x - r.left()) / r.width())

    def _y_range(self) -> tuple[float, float]:
        if self.mode == self.KEPT:
            return 0.0, 100.0
        if self.mode == self.DB:
            return -60.0, 3.0
        _, y = self.curve()
        y = y[np.isfinite(y)]
        hi = float(max(1.0, y.max() * 1.1)) if y.size else 1.0
        lo = float(min(0.0, y.min() * 1.1)) if y.size else 0.0
        return lo, hi

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = self.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        text, muted = pal.windowText().color(), QColor(pal.windowText().color())
        muted.setAlpha(110)
        accent = pal.highlight().color()
        r = self._frame()
        p.fillRect(self.rect(), pal.base())
        fm = p.fontMetrics()
        lo, hi = self._y_range()

        def yp(v: float) -> float:
            return r.bottom() - (min(max(v, lo), hi) - lo) / (hi - lo) * r.height()

        p.setPen(QPen(muted, 1))
        # decade and 1-2-5 gridlines
        decade = 10.0 ** math.floor(math.log10(F_MIN))
        while decade <= self.f_max:
            for m in (1, 2, 5):
                f = m * decade
                if F_MIN <= f <= self.f_max:
                    x = self._x(f, r)
                    p.setPen(QPen(muted, 1 if m == 1 else 0.5))
                    p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
                    if m in (1, 5) or self.width() > fm.height() * 40:
                        label = f"{f:g}"
                        p.setPen(text)
                        p.drawText(
                            QPointF(x - fm.horizontalAdvance(label) / 2, r.bottom() + fm.height()),
                            label,
                        )
            decade *= 10
        p.setPen(text)
        p.drawText(
            QPointF(
                r.center().x() - fm.horizontalAdvance("Frequency (Hz)") / 2,
                r.bottom() + 2 * fm.height() - 2,
            ),
            "Frequency (Hz)",
        )
        ticks = {self.KEPT: (0, 25, 50, 75, 100), self.DB: (-60, -40, -20, -3, 0)}.get(self.mode)
        if ticks is None:
            ticks = tuple(np.linspace(lo, hi, 5))
        for v in ticks:
            y = yp(v)
            p.setPen(QPen(muted, 0.5))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(text)
            unit = "%" if self.mode == self.KEPT else ""
            label = f"{v:g}{unit}" if self.mode != self.DELAY else f"{v:.1f}"
            p.drawText(
                QPointF(r.left() - fm.horizontalAdvance(label) - 4, y + fm.ascent() / 2 - 1), label
            )
        if self.mode == self.DB:
            p.setPen(QPen(accent, 1, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(r.left(), yp(-3)), QPointF(r.right(), yp(-3)))
        # markers
        eff = dsp.effective(self.settings, self.fs)
        marks = []
        if eff.highpass_hz:
            marks.append(eff.highpass_hz)
        if eff.lowpass_hz:
            marks.append(eff.lowpass_hz)
        marks += dsp.notch_frequencies(self.settings, self.fs)
        p.setPen(QPen(accent, 1, Qt.PenStyle.DashLine))
        for f in marks:
            if F_MIN <= f <= self.f_max:
                p.drawLine(QPointF(self._x(f, r), r.top()), QPointF(self._x(f, r), r.bottom()))
        freqs, y = self.curve()
        p.setPen(QPen(text, 2))
        pts = [QPointF(self._x(f, r), yp(v)) for f, v in zip(freqs, y, strict=True)]
        for a, b in zip(pts[:-1], pts[1:], strict=True):
            p.drawLine(a, b)
        if self.hover_hz is not None:
            x = self._x(self.hover_hz, r)
            p.setPen(QPen(text, 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        p.end()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        r = self._frame()
        x = event.position().x()
        if r.left() <= x <= r.right():
            self.hover_hz = self._f_at(x, r)
            self.hovered.emit(self.readout(self.hover_hz))
        else:
            self.hover_hz = None
            self.hovered.emit("")
        self.update()


class ResponseDialog(QDialog):
    """How much of each frequency the current filters keep."""

    def __init__(
        self, settings: dsp.FilterSettings, rates, default_rate: float | None = None, parent=None
    ):
        super().__init__(parent)
        self.setWindowTitle("What the filters do")
        self.rates = sorted({float(r) for r in rates}) or [256.0]
        fs = default_rate if default_rate in self.rates else self.rates[-1]
        self.settings = settings
        self.rate_box = QComboBox(self)
        for r in self.rates:
            self.rate_box.addItem(format_rate(r), r)
        self.rate_box.setCurrentIndex(self.rates.index(fs))
        self.rate_box.setVisible(len(self.rates) > 1)
        self.plot = ResponsePlot(settings, fs, self)
        self.tabs = QTabBar(self)
        self.tabs.addTab("How much is kept")
        self.tabs.addTab("Decibels")
        self.tab_delay = self.tabs.addTab("Time shift")
        self.readout = QLabel("Move the pointer over the plot to read it.", self)
        self.words = QLabel("", self)
        self.words.setWordWrap(True)
        self.shift_note = QLabel("No time shift: the waves stay where they were recorded.", self)
        self.shift_note.setWordWrap(True)
        box = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(self.tabs, 1)
        top.addWidget(QLabel("Sampling rate", self))
        top.addWidget(self.rate_box)
        self.rate_box.setVisible(len(self.rates) > 1)
        box.addLayout(top)
        box.addWidget(self.plot, 1)
        box.addWidget(self.shift_note)
        box.addWidget(self.readout)
        box.addWidget(self.words)
        self.tabs.currentChanged.connect(self._tab)
        self.rate_box.currentIndexChanged.connect(self._rate)
        self.plot.hovered.connect(
            lambda t: self.readout.setText(t or "Move the pointer over the plot to read it.")
        )
        self.refresh(settings)

    def set_rate(self, rate: float) -> None:
        if rate in self.rates:
            self.rate_box.setCurrentIndex(self.rates.index(rate))

    def refresh(self, settings: dsp.FilterSettings) -> None:
        self.settings = settings
        self.plot.settings = settings
        self.tabs.setTabVisible(self.tab_delay, not settings.zero_phase)
        if settings.zero_phase and self.tabs.currentIndex() == self.tab_delay:
            self.tabs.setCurrentIndex(0)
        self._words()
        self.plot.update()

    def _words(self) -> None:
        self.words.setText("\n".join(dsp.describe(self.settings, self.plot.fs)))
        self._tab(self.tabs.currentIndex())

    def _tab(self, index: int) -> None:
        self.plot.mode = (ResponsePlot.KEPT, ResponsePlot.DB, ResponsePlot.DELAY)[index]
        self.shift_note.setVisible(self.settings.zero_phase)
        self.plot.update()

    def _rate(self, _i: int) -> None:
        self.plot.fs = float(self.rate_box.currentData())
        self._words()
        self.plot.update()
