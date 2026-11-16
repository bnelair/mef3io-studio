"""The trace layer's fast paths: polylines built from the stream form, and a
one-device-pixel cosmetic pen (Qt's fast line path; wider pens are stroked
as outlines and made each page take seconds)."""

# ruff: noqa: F811
from __future__ import annotations

import numpy as np
import pytest
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


def test_a_fractional_scale_does_not_re_render_on_each_paint(one, qtbot, monkeypatch):
    """At 1.25 a canvas width of 4k + 2 gives a x.5 device width, where
    QSize * dpr rounds up and the pixmap size truncates."""
    for _ in range(4):
        if one.canvas.width() % 4 == 2:
            break
        one.resize(one.width() + 1, one.height())
        qtbot.wait(20)
    assert one.canvas.width() % 4 == 2
    monkeypatch.setattr(one.canvas, "devicePixelRatioF", lambda: 1.25)
    renders = []
    real = one.canvas._render
    monkeypatch.setattr(one.canvas, "_render", lambda: (renders.append(1), real())[1])
    one.canvas.invalidate()
    for _ in range(3):
        one.canvas.repaint()
    assert len(renders) == 1


# --- building a polygon by writing into its memory ----------


def test_the_direct_polygon_is_the_stream_polygon_point_for_point(qapp):
    from mef3io_studio.viewer import canvas as cv

    rng = np.random.default_rng(3)
    for n in (0, 1, 2, 3, 100, 5000):
        xs, ys = rng.uniform(-3e3, 3e3, n), rng.uniform(-3e3, 3e3, n)
        a, b = cv._polygon_stream(xs, ys), cv._polygon_direct(xs, ys)
        assert a.size() == b.size() == n
        assert all(a.at(i) == b.at(i) for i in range(n))
    assert cv._direct_polygon_is_sound() is True


def test_a_direct_polygon_that_differs_or_fails_is_dropped_for_good(qapp, monkeypatch):
    from mef3io_studio.viewer import canvas as cv

    real = cv._polygon_direct
    monkeypatch.setattr(cv, "_use_direct", None)
    monkeypatch.setattr(cv, "_polygon_direct", lambda xs, ys: real(xs + 1.0, ys))  # a wrong one
    assert cv._direct_polygon_is_sound() is False
    poly = cv._polygon(np.array([1.0, 2.0]), np.array([3.0, 4.0]))  # decides, uses the stream
    assert cv._use_direct is False and poly.at(1).x() == 2.0

    def broken(xs, ys):
        raise OSError("no such layout")

    monkeypatch.setattr(cv, "_use_direct", None)
    monkeypatch.setattr(cv, "_polygon_direct", broken)
    assert cv._direct_polygon_is_sound() is False
    assert cv._polygon(np.array([5.0]), np.array([6.0])).at(0).y() == 6.0


def test_the_level_of_a_shape_is_its_median_from_a_sample():
    from mef3io_studio.viewer.canvas import _RowShape

    rng = np.random.default_rng(5)
    ys = rng.normal(40, 10, 20_000)
    shape = _RowShape([(np.arange(ys.size), ys, False)], [])
    assert shape.centre == pytest.approx(np.median(ys), abs=1.5)
    nan = _RowShape([(np.arange(3.0), np.full(3, np.nan), False)], [])
    assert np.isnan(nan.centre) and np.isnan(_RowShape([], []).centre)
