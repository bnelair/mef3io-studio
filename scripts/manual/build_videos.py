"""Build the tutorial videos from the tours: silent screen recordings of the
real app on made-up recordings, with a caption bar, a moving mouse pointer
and a ring on every click.

    QT_QPA_PLATFORM=offscreen PYTHONPATH=src python scripts/manual/build_videos.py
        [--out dist/manual/videos] [--tour t1 ...] [--fps 20] [--format auto|mp4|apng]

``--format auto`` (default) writes ``<tour>.mp4`` through QtMultimedia's
FFmpeg backend: H.264 when this Qt can encode it, else MPEG-4 Part 2 in the
same .mp4 (the PySide6 FFmpeg is an LGPL build without x264; the script says
so). Only when QtMultimedia is missing it says why and writes ``<tour>.png``
instead: an animated PNG
(APNG; open it in any browser), written by this script itself, so it needs
no extra program. For MP4, install the full PySide6 (``pip install
PySide6-Addons==<the PySide6 version>``): the lean ``PySide6-Essentials``
has no QtMultimedia.
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tourlib  # noqa: E402
from tourlib import Sink  # noqa: E402

ROOT = tourlib.ROOT
SIZE = (1280, 720)
BAR = 100


# --------------------------------------------------------------------------
# Frames


class FrameSink(Sink):
    """Collects (image, milliseconds) pairs. Frames are 1280 x 820 (the
    window plus a caption bar)."""

    def __init__(self, fps: int = 20):
        self.fps = fps
        self.frames: list[tuple] = []
        self.step = None
        self.label = ""
        self.focus = None
        self.title = ""

    # -- building blocks ------------------------------------------------------
    def _frame_ms(self, n: float = 1.0) -> int:
        return round(1000 * n / self.fps)

    def _add(self, image, ms: int) -> None:
        self.frames.append((image, ms))

    def _compose(self, ctx, base=None, pointer=None, ring=0.0, focus=None, caption=True):
        image = base if base is not None else ctx.grab()
        if focus is not None:
            image = tourlib.draw_highlight(image, focus)
        else:
            image = image.copy()
        tourlib.draw_pointer(image, pointer, ring) if pointer is not None else None
        if caption and self.step is not None:
            return tourlib.draw_caption(image, self.step.caption, self.label, BAR)
        return tourlib.draw_caption(image, "", "", BAR)

    # -- Sink -------------------------------------------------------------------
    def begin(self, tour, ctx):
        self.title = tour.title
        self.total = len(tour.steps)
        self._title_card(ctx, tour)

    def _title_card(self, ctx, tour):
        from PySide6.QtCore import QRect, Qt
        from PySide6.QtGui import QColor, QFont, QImage, QPainter

        from mef3io_studio._version import app_version

        w, h = SIZE[0], SIZE[1] + BAR
        img = QImage(w, h, QImage.Format.Format_RGB32)
        img.fill(QColor("#0b3d91"))
        p = QPainter(img)
        p.setPen(QColor("white"))
        font = QFont(p.font())
        font.setPixelSize(54)
        font.setBold(True)
        p.setFont(font)
        p.drawText(
            QRect(80, 220, w - 160, 260),
            int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
            tour.title,
        )
        font.setPixelSize(26)
        font.setBold(False)
        p.setFont(font)
        p.drawText(
            QRect(80, 500, w - 160, 50),
            int(Qt.AlignmentFlag.AlignCenter),
            f"mef3io-studio {app_version()}  -  made-up recordings, no patient data",
        )
        p.end()
        self._add(img, 2600)

    def step_begin(self, ctx, index, step):
        self.step = step
        self.label = f"{index + 1}/{self.total}"

    def pointer(self, ctx, start, end):
        if start is None or end is None:
            return
        base = ctx.grab()
        n = max(2, round(0.6 * self.fps))
        for i in range(1, n + 1):
            t = i / n
            t = t * t * (3 - 2 * t)  # smooth start and stop
            at = start + (end - start) * t
            self._add(
                self._compose(ctx, base, at.toPoint() if hasattr(at, "toPoint") else at),
                self._frame_ms(),
            )

    def clicked(self, ctx, at):
        base = ctx.grab()
        for i in range(1, 6):
            self._add(self._compose(ctx, base, at, ring=i / 5), self._frame_ms())
        self.last = at

    def tick(self, ctx, at):
        base = ctx.grab()
        self._add(self._compose(ctx, base, at), self._frame_ms(2))

    def shot(self, ctx, index, step, focus):
        pointer = ctx.pointer
        self._add(self._compose(ctx, None, pointer, focus=focus), round(1000 * step.hold))

    def end(self, ctx):
        # Hold the last scene a little longer.
        if self.frames:
            image, ms = self.frames[-1]
            self.frames[-1] = (image, ms + 1500)

    @property
    def seconds(self) -> float:
        return sum(ms for _i, ms in self.frames) / 1000


# --------------------------------------------------------------------------
# Writers


def _can_mp4() -> tuple[str | None, str]:
    """(codec name to use or None, note). H.264 when this Qt can encode it;
    otherwise MPEG-4 Part 2 in the same .mp4 file if the backend offers it
    (the FFmpeg that ships with PySide6 is an LGPL build without x264).
    Needs a QApplication."""
    try:
        from PySide6 import QtMultimedia as M
    except ImportError as exc:
        import PySide6

        return None, (
            f"QtMultimedia is not installed ({exc}). Install the full PySide6, e.g. "
            f"'pip install PySide6-Addons=={PySide6.__version__}', for MP4 output"
        )
    try:
        fmt = M.QMediaFormat()
        encode = M.QMediaFormat.ConversionMode.Encode
        if M.QMediaFormat.FileFormat.MPEG4 not in fmt.supportedFileFormats(encode):
            return (
                None,
                "this Qt's multimedia backend cannot write MPEG-4 (is the FFmpeg backend missing?)",
            )
        fmt.setFileFormat(M.QMediaFormat.FileFormat.MPEG4)
        codecs = fmt.supportedVideoCodecs(encode)
        if M.QMediaFormat.VideoCodec.H264 in codecs:
            return "H264", ""
        if M.QMediaFormat.VideoCodec.MPEG4 in codecs:
            return "MPEG4", (
                "this Qt's FFmpeg has no H.264 encoder (the PySide6 build is LGPL, no x264, "
                "and no hardware encoder was found); using MPEG-4 Part 2 inside the .mp4"
            )
        return None, "this Qt's multimedia backend has no H.264 or MPEG-4 video encoder"
    except Exception as exc:  # a broken backend must not stop the APNG fallback
        return None, f"QtMultimedia could not be asked what it can encode: {exc}"


def write_mp4(frames, fps: int, path: Path, codec: str = "H264") -> None:
    """An .mp4 through QtMultimedia (QVideoFrameInput + QMediaRecorder)."""
    from PySide6.QtCore import QEventLoop, QSize, QTimer, QUrl
    from PySide6.QtGui import QImage
    from PySide6.QtMultimedia import (
        QMediaCaptureSession,
        QMediaFormat,
        QMediaRecorder,
        QVideoFrame,
        QVideoFrameFormat,
        QVideoFrameInput,
    )

    first = frames[0][0].convertToFormat(QImage.Format.Format_ARGB32)
    size = QSize(first.width() - first.width() % 2, first.height() - first.height() % 2)
    pixel = QVideoFrameFormat.pixelFormatFromImageFormat(QImage.Format.Format_ARGB32)
    frame_input = QVideoFrameInput(QVideoFrameFormat(size, pixel))
    session = QMediaCaptureSession()
    session.setVideoFrameInput(frame_input)
    recorder = QMediaRecorder()
    session.setRecorder(recorder)
    media = QMediaFormat(QMediaFormat.FileFormat.MPEG4)
    media.setVideoCodec(getattr(QMediaFormat.VideoCodec, codec))
    recorder.setMediaFormat(media)
    recorder.setVideoResolution(size)
    recorder.setVideoFrameRate(fps)
    recorder.setQuality(QMediaRecorder.Quality.HighQuality)
    recorder.setOutputLocation(QUrl.fromLocalFile(str(path)))
    errors: list[str] = []
    recorder.errorOccurred.connect(lambda _e, text: errors.append(text))
    loop = QEventLoop()

    def spin(ms: int) -> None:
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    recorder.record()
    spin(200)
    step_us = round(1_000_000 / fps)
    clock = 0
    for image, ms in frames:
        count = max(1, round(ms * fps / 1000))
        rgba = image.convertToFormat(QImage.Format.Format_ARGB32).copy(
            0, 0, size.width(), size.height()
        )
        for _ in range(count):
            frame = QVideoFrame(rgba)
            frame.setStartTime(clock)
            frame.setEndTime(clock + step_us)
            clock += step_us
            waited = 0
            while not frame_input.sendVideoFrame(frame):
                spin(5)  # the encoder is busy: wait for it to take frames again
                waited += 5
                if waited > 20_000 or errors:
                    raise RuntimeError(
                        "The video encoder stopped taking frames: " + "; ".join(errors)
                    )
    recorder.stop()
    waited = 0
    while recorder.recorderState() != QMediaRecorder.RecorderState.StoppedState and waited < 30_000:
        spin(50)
        waited += 50
    spin(300)
    if errors:
        raise RuntimeError("; ".join(errors))


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _rgba(image):
    import numpy as np
    from PySide6.QtGui import QImage

    img = image.convertToFormat(QImage.Format.Format_RGBA8888)
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=img.sizeInBytes())
    return (
        buf.reshape(img.height(), img.bytesPerLine())[:, : img.width() * 4]
        .reshape(img.height(), img.width(), 4)
        .copy()
    )


def write_apng(frames, fps: int, path: Path) -> None:
    """An animated PNG. Only the part of the picture that changed is stored
    for each frame, and identical neighbours are merged into one longer
    frame, so a held scene costs almost nothing."""
    import numpy as np

    pictures: list[tuple] = []  # (x, y, array, ms)
    previous = None
    for image, ms in frames:
        cur = _rgba(image)
        if previous is None:
            pictures.append((0, 0, cur, ms))
        else:
            diff = np.any(cur != previous, axis=2)
            if not diff.any():
                x, y, a, old = pictures[-1]
                pictures[-1] = (x, y, a, old + ms)
            else:
                rows = np.flatnonzero(diff.any(axis=1))
                cols = np.flatnonzero(diff.any(axis=0))
                y0, y1, x0, x1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
                pictures.append((int(x0), int(y0), cur[y0:y1, x0:x1], ms))
        previous = cur
    height, width = pictures[0][2].shape[:2]
    out = [
        b"\x89PNG\r\n\x1a\n",
        _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
    ]
    out.append(_chunk(b"acTL", struct.pack(">II", len(pictures), 0)))  # 0 = loop forever
    seq = 0
    for i, (x, y, arr, ms) in enumerate(pictures):
        h, w = arr.shape[:2]
        out.append(
            _chunk(
                b"fcTL",
                struct.pack(">IIIIIHHBB", seq, w, h, x, y, min(ms, 65535), 1000, 0, 0),
            )
        )
        seq += 1
        raw = np.concatenate([np.zeros((h, 1), np.uint8), arr.reshape(h, w * 4)], axis=1).tobytes()
        data = zlib.compress(raw, 6)
        if i == 0:
            out.append(_chunk(b"IDAT", data))
        else:
            out.append(_chunk(b"fdAT", struct.pack(">I", seq) + data))
            seq += 1
    out.append(_chunk(b"IEND", b""))
    path.write_bytes(b"".join(out))


# --------------------------------------------------------------------------


def record_tour(tour, fps: int) -> FrameSink:
    work = tourlib.scratch()
    sink = FrameSink(fps)
    try:
        tourlib.run_tour(tour, sink, work, size=SIZE)
    finally:
        tourlib.cleanup(work)
    return sink


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(ROOT / "dist" / "manual" / "videos"))
    ap.add_argument("--tour", action="append", help="only these tours (default: all)")
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--format", choices=("auto", "mp4", "apng"), default="auto")
    args = ap.parse_args(argv)

    tourlib.prepare_environment(tourlib.scratch("video-env-"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    from mef3io_studio.app import create_application

    create_application()
    codec, note = _can_mp4()
    want_mp4 = args.format == "mp4" or (args.format == "auto" and codec is not None)
    if want_mp4 and codec is None:
        print(f"Cannot write MP4: {note}.")
        return 1
    if want_mp4 and note:
        print(f"Note: {note}.")
    if not want_mp4 and args.format == "auto":
        print(
            f"MP4 is not possible here: {note}.\n"
            "Writing animated PNG files (.png, open in a browser) instead."
        )
    status = 0
    for tour in tourlib.load_tours(args.tour):
        print(f"recording {tour.key}: {tour.title}", flush=True)
        sink = record_tour(tour, args.fps)
        name = f"{tour.key}-{tour.video_title or 'tour'}"
        path = out / (name + (".mp4" if want_mp4 else ".png"))
        try:
            if want_mp4:
                write_mp4(sink.frames, args.fps, path, codec)
            else:
                write_apng(sink.frames, args.fps, path)
        except Exception as exc:
            print(f"  failed: {exc}")
            status = 1
            continue
        print(
            f"  {path}  {path.stat().st_size / 1e6:.1f} MB, {sink.seconds:.0f} s, "
            f"{len(sink.frames)} frames"
        )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
