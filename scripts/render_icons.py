"""Render the app icon (src/mef3io_studio/resources/icons/app.svg) into the
formats the installers need. Run in CI before PyInstaller; outputs are build
artefacts and are not committed.

    python scripts/render_icons.py --png out/app.png      # Linux AppImage
    python scripts/render_icons.py --ico out/app.ico      # Windows exe/installer
    python scripts/render_icons.py --iconset out/app.iconset   # macOS: iconutil -c icns
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QByteArray, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

SVG = Path(__file__).resolve().parents[1] / "src/mef3io_studio/resources/icons/app.svg"


def render(size: int) -> QImage:
    renderer = QSvgRenderer(QByteArray(SVG.read_bytes()))
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--png")
    parser.add_argument("--ico")
    parser.add_argument("--iconset")
    args = parser.parse_args(argv)
    QGuiApplication(sys.argv[:1])
    if args.png:
        Path(args.png).parent.mkdir(parents=True, exist_ok=True)
        assert render(256).save(args.png, "PNG")
    if args.ico:
        Path(args.ico).parent.mkdir(parents=True, exist_ok=True)
        # Qt's ico writer stores one image; 256 px is what Windows scales from.
        assert render(256).save(args.ico, "ICO"), "Qt ico image plugin missing"
    if args.iconset:
        out = Path(args.iconset)
        out.mkdir(parents=True, exist_ok=True)
        for base in (16, 32, 128, 256, 512):
            assert render(base).save(str(out / f"icon_{base}x{base}.png"), "PNG")
            assert render(base * 2).save(str(out / f"icon_{base}x{base}@2x.png"), "PNG")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
