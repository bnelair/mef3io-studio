"""The trace layer's fast paths: polylines built from the stream form, and a
one-device-pixel cosmetic pen (Qt's fast line path; wider pens are stroked
as outlines and made each page take seconds)."""

# ruff: noqa: F811
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage, QPolygonF
from test_viewer_ui import make_window, one  # noqa: F401

from mef3io_studio.viewer import canvas


def test_polygon_holds_exactly_the_given_points(qapp):
    rng = np.random.default_rng(3)
    for n in (0, 1, 2, 3001):
        xs = rng.uniform(-50, 2000, n).astype(np.float32)
        ys = rng.normal(0, 1e4, n)
        want = QPolygonF([QPointF(float(x), float(y)) for x, y in zip(xs, ys, strict=True)])
        assert canvas._polygon(xs, ys) == want


def test_polygon_keeps_non_finite_values(qapp):
    poly = canvas._polygon(np.array([0.0, 1.0, 2.0]), np.array([np.nan, np.inf, -0.0]))
    got = [(poly.at(i).x(), poly.at(i).y()) for i in range(poly.size())]
    assert np.isnan(got[0][1]) and got[1][1] == np.inf and got[2] == (2.0, 0.0)


def test_trace_pen_is_one_device_pixel():
    pen = canvas._trace_pen("#123456")
    assert pen.isCosmetic() and pen.widthF() == 1.0


def _ink(win) -> int:
    img = win.canvas.grab_traces().toImage()
    trace = QColor(win.canvas.palette_.trace)
    bg = QColor(win.canvas.palette_.background)

    def inked(px: int) -> bool:  # nearer the trace colour than the page's
        c = QColor(px)
        d = lambda a: sum(abs(u - v) for u, v in zip(c.getRgb()[:3], a.getRgb()[:3], strict=True))  # noqa: E731
        return d(trace) < d(bg)

    plot = win.canvas.plot_rect()
    dpr = img.devicePixelRatio()
    hits = sum(
        inked(img.pixel(int(x * dpr), int(y * dpr)))
        for x in range(int(plot.left()) + 2, int(plot.right()) - 2, 3)
        for y in range(0, int(plot.height()), 1)
    )
    return hits


def _ink_all(win) -> int:
    """Inked pixels over the whole plot, in device pixels."""
    img = win.canvas.grab_traces().toImage().convertToFormat(QImage.Format.Format_RGB32)
    px = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.bytesPerLine())
    bgra = px[:, : 4 * img.width()].reshape(img.height(), img.width(), 4).astype(int)
    trace = QColor(win.canvas.palette_.trace)
    plot = win.canvas.plot_rect()
    dpr = img.devicePixelRatio()
    area = bgra[
        : int(plot.height() * dpr), int(plot.left() * dpr) + 4 : int(plot.right() * dpr) - 4
    ]
    near = np.abs(area[..., 2] - trace.red()) + np.abs(area[..., 1] - trace.green()) < 120
    return int(near.sum())


def test_a_page_puts_trace_ink_on_screen(one):
    assert _ink(one) > 200


def test_a_2x_screen_draws_each_trace_twice(one, monkeypatch):
    """At 2x a one-device-pixel line is drawn again one device pixel over:
    about twice the ink of a single line at that scale."""
    monkeypatch.setattr(one.canvas, "devicePixelRatioF", lambda: 2.0)
    one.canvas.invalidate()
    echoed = _ink_all(one)
    monkeypatch.setattr(canvas.QPolygonF, "translated", lambda self, dx, dy: QPolygonF())
    one.canvas.invalidate()
    single = _ink_all(one)
    assert echoed > 1.6 * single > 0
