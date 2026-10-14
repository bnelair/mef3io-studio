"""Bundled SVG icons, tinted to the current theme.

Each icon is rendered from its SVG at many pixel sizes (up to 256 px), so Qt
picks a crisp one at any display scale — no blurry icons on high-DPI screens.
The SVGs use ``currentColor``; we substitute the theme's text colour.
"""

from __future__ import annotations

from functools import cache, lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

ICON_DIR = Path(__file__).resolve().parent.parent / "resources" / "icons"
_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def icon_names() -> list[str]:
    return sorted(p.stem for p in ICON_DIR.glob("*.svg"))


@cache
def _svg(name: str) -> str:
    return (ICON_DIR / f"{name}.svg").read_text(encoding="utf-8")


def render(name: str, size: int, color: str | None = None) -> QPixmap:
    svg = _svg(name)
    if color:
        svg = svg.replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


@lru_cache(maxsize=256)
def icon(name: str, color: str | None = None, disabled_color: str | None = None) -> QIcon:
    result = QIcon()
    for size in _SIZES:
        result.addPixmap(render(name, size, color), QIcon.Mode.Normal)
        if disabled_color:
            result.addPixmap(render(name, size, disabled_color), QIcon.Mode.Disabled)
    return result


def app_icon() -> QIcon:
    return icon("app")
