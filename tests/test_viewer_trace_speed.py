"""The trace layer's fast paths: polylines built from the stream form, and a
one-device-pixel cosmetic pen (Qt's fast line path; wider pens are stroked
as outlines and made each page take seconds)."""

# ruff: noqa: F811
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QPolygonF
from test_viewer_ui import make_window, one  # noqa: F401

from mef3io_studio.viewer import canvas


def test_polygon_holds_exactly_the_given_points(qapp):
    rng = np.random.default_rng(3)
    for n in (0, 1, 2, 3001):
        xs = rng.uniform(-50, 2000, n).astype(np.float32)
        ys = rng.normal(0, 1e4, n)
        want = QPolygonF([QPointF(float(x), float(y)) for x, y in zip(xs, ys, strict=True)])
        assert canvas._polygon(xs, ys) == want


def test_trace_pen_is_one_device_pixel():
    pen = canvas._trace_pen("#123456")
    assert pen.isCosmetic() and pen.widthF() == 1.0


def test_a_page_puts_trace_ink_on_screen(one):
    img = one.canvas.grab_traces().toImage()
    trace = QColor(one.canvas.palette_.trace)
    bg = QColor(one.canvas.palette_.background)

    def inked(px: int) -> bool:  # nearer the trace colour than the page's
        c = QColor(px)
        d = lambda a: sum(abs(u - v) for u, v in zip(c.getRgb()[:3], a.getRgb()[:3], strict=True))  # noqa: E731
        return d(trace) < d(bg)

    plot = one.canvas.plot_rect()
    dpr = img.devicePixelRatio()
    hits = sum(
        inked(img.pixel(int(x * dpr), int(y * dpr)))
        for x in range(int(plot.left()) + 2, int(plot.right()) - 2, 3)
        for y in range(0, int(plot.height()), 1)
    )
    assert hits > 200
