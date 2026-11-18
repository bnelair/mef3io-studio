"""The manual and video tours (docs/manual/tours, scripts/manual) run against
the CURRENT app, headless, on made-up recordings. A UI change that moves or
renames something a tour clicks makes a tour fail here, so the manual and
the videos never drift from the app. No video encoding, no real data.
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "manual"))

import tourlib  # noqa: E402
from run_tour import ShotSink  # noqa: E402
from test_ui_language import BANNED_RE  # noqa: E402

TOURS = tourlib.load_tours()
KEYS = [t.key for t in TOURS]


def test_every_tour_is_found_and_well_formed():
    assert KEYS == ["t1", "t2", "t3", "t4"]
    for tour in TOURS:
        assert tour.title and tour.summary and tour.steps
        assert len({s.title for s in tour.steps}) == len(tour.steps), tour.key
        for step in tour.steps:
            assert step.title and step.caption and step.hold > 0


def test_the_manual_text_uses_plain_words():
    """docs/ui-language.md: the same words as the app."""
    for tour in TOURS:
        texts = [tour.title, tour.summary, *tour.before]
        for step in tour.steps:
            texts += [step.title, step.caption]
        for text in texts:
            assert not BANNED_RE.search(text), (tour.key, text)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Each tour is run once; the folders are shared with the PDF test."""
    return {}


def run_once(runs, tmp_factory, tour):
    if tour.key not in runs:
        base = tmp_factory.mktemp(tour.key)
        (base / "work").mkdir()
        sink = ShotSink(base / "shots")
        tourlib.run_tour(tour, sink, base / "work", fast=True)
        runs[tour.key] = base
    return runs[tour.key]


@pytest.mark.parametrize("tour", TOURS, ids=KEYS)
def test_tour_runs_against_the_real_app(tour, runs, tmp_path_factory):
    base = run_once(runs, tmp_path_factory, tour)
    records = json.loads((base / "shots" / f"{tour.key}.json").read_text("utf-8"))
    assert [r["title"] for r in records] == [s.title for s in tour.steps]
    for rec in records:
        assert (base / "shots" / rec["image"]).stat().st_size > 5_000
    if tour.key in ("t1", "t2", "t3"):
        # The tours that save a recording really did: and left the originals alone.
        names = sorted(p.name for p in (base / "work" / "My recordings").iterdir())
        assert len(names) == 1 and (names[0].endswith((".mefd.tar", ".mefd"))), names
        for name in tourlib.NIGHTS:
            assert (base / "work" / "recordings" / f"{name}.mefd").is_dir()


def test_apng_writer_makes_a_valid_animation(qapp, tmp_path):
    from build_videos import write_apng
    from PySide6.QtGui import QColor, QImage, QPainter

    def frame(x):
        img = QImage(64, 40, QImage.Format.Format_RGB32)
        img.fill(QColor("white"))
        p = QPainter(img)
        p.fillRect(x, 10, 8, 8, QColor("black"))
        p.end()
        return img

    frames = [(frame(0), 100), (frame(0), 100), (frame(20), 100), (frame(30), 250)]
    out = tmp_path / "t.png"
    write_apng(frames, 20, out)
    data = out.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, kinds, delays = 8, [], []
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + n]
        (crc,) = struct.unpack(">I", data[pos + 8 + n : pos + 12 + n])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        kinds.append(kind)
        if kind == b"fcTL":
            delays.append(struct.unpack(">IIIIIHH", body[:24])[5])
        pos += 12 + n
    assert kinds[0] == b"IHDR" and kinds[-1] == b"IEND" and b"acTL" in kinds
    assert delays == [200, 100, 250]  # the identical second frame was merged


def test_the_manual_builds_a_pdf(qapp, tmp_path, runs, tmp_path_factory):
    """One tour is enough to check the layout and the PDF writer."""
    import build_manual

    t4 = next(t for t in TOURS if t.key == "t4")
    shots = run_once(runs, tmp_path_factory, t4) / "shots"
    code = build_manual.main(
        ["--tour", "t4", "--out", str(tmp_path / "out"), "--shots", str(shots)]
    )
    assert code == 0
    pdfs = list((tmp_path / "out").glob("mef3io-studio-manual-*.pdf"))
    assert len(pdfs) == 1
    data = pdfs[0].read_bytes()
    assert data.startswith(b"%PDF-") and data.count(b"/Type /Page\n") >= 8
