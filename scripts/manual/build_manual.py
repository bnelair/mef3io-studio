"""Build the user manual (PDF) from the tours.

    QT_QPA_PLATFORM=offscreen PYTHONPATH=src python scripts/manual/build_manual.py
        [--out dist/manual] [--shots DIR] [--preview DIR] [--tour t1 ...]

Runs every tour in docs/manual/tours against made-up recordings, takes one
screenshot per step, and lays out an A4 PDF with Qt's own text engine (no
other tools): title page, contents, "Getting started", one chapter per tour
(numbered steps, each with its picture and caption), and a few short
reference sections taken from README.md and docs/user-guide.md. Light theme.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_tour  # noqa: E402
import tourlib  # noqa: E402

ROOT = tourlib.ROOT
DPI = 150
MM = DPI / 25.4  # device pixels per millimetre
MARGIN_MM = 20
FOOTER_MM = 10

INK, BLUE, MUTED = "#1f2933", "#0b3d91", "#5f6b7a"
USER_ID = 1  # QTextFormat.UserProperty offset for our block tags

#: (title, file, heading) of the reference sections taken from the docs.
REFERENCE = [
    ("Times and time zones", "docs/user-guide.md", "Times and time zones"),
    ("Patient details stay hidden", "docs/user-guide.md", "Patient details stay hidden"),
    ("Privacy check", "docs/user-guide.md", "Privacy check"),
    ("Getting help and reporting problems", "docs/user-guide.md", "Help and reporting problems"),
]


# --------------------------------------------------------------------------
# Pulling text out of the docs


def markdown_section(path: Path, heading: str) -> str:
    """The body of the section called ``heading`` (without its heading line,
    pictures, or sub-headings' own level changes), up to the next heading of
    the same or a higher level."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = level = None
    for i, line in enumerate(lines):
        m = re.match(r"^(#+)\s+(.*?)\s*$", line)
        if m and m.group(2) == heading:
            start, level = i + 1, len(m.group(1))
            break
    if start is None:
        raise LookupError(f"No section {heading!r} in {path}")
    body = []
    for line in lines[start:]:
        m = re.match(r"^(#+)\s", line)
        if m and len(m.group(1)) <= level:
            break
        if line.lstrip().startswith("!["):
            continue  # pictures stay in the guide
        body.append(line)
    return "\n".join(body).strip()


# --------------------------------------------------------------------------
# The document


def pick_font() -> str:
    from PySide6.QtGui import QFontDatabase

    have = set(QFontDatabase.families())
    for name in (
        "Segoe UI",
        "Noto Sans",
        "DejaVu Sans",
        "Arial",
        "Helvetica Neue",
        "Liberation Sans",
    ):
        if name in have:
            return name
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family()


def measure_image_scale(device) -> float:
    """Qt draws inline pictures scaled by (device dpi / screen dpi); find the
    factor by laying out a 100-unit-wide picture on ``device``."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QImage, QTextCursor, QTextDocument, QTextImageFormat

    doc = QTextDocument()
    doc.documentLayout().setPaintDevice(device)
    doc.setDocumentMargin(0)
    doc.addResource(
        QTextDocument.ResourceType.ImageResource,
        QUrl("probe://x"),
        QImage(10, 10, QImage.Format.Format_RGB32),
    )
    fmt = QTextImageFormat()
    fmt.setName("probe://x")
    fmt.setWidth(100)
    fmt.setHeight(100)
    QTextCursor(doc).insertImage(fmt)
    return doc.idealWidth() / 100.0


class Manual:
    """Lays the manual out as a QTextDocument of fixed page size."""

    def __init__(self, tours, shots: dict, cover, version: str, date: str, device):
        from PySide6.QtCore import QSizeF
        from PySide6.QtGui import QFont, QTextDocument

        self.tours, self.shots, self.cover = tours, shots, cover
        self.version, self.date = version, date
        self.page_w = device.width() - 2 * MARGIN_MM * MM
        self.page_h = device.height() - 2 * MARGIN_MM * MM - FOOTER_MM * MM
        self.doc = QTextDocument()
        self.doc.documentLayout().setPaintDevice(device)
        self.family = pick_font()
        font = QFont(self.family)
        font.setPointSizeF(10.5)
        self.doc.setDefaultFont(font)
        self.doc.setPageSize(QSizeF(self.page_w, self.page_h))
        self.doc.setDocumentMargin(0)
        self.doc.setDefaultStyleSheet(
            f"a {{ color: {BLUE}; }} table {{ border-collapse: collapse; }}"
        )
        self.image_scale = measure_image_scale(device)
        self.cursor = None
        #: tag -> page-break-before (step cards that would straddle a page)
        self.breaks: set[str] = set()
        #: chapter number -> page number (second pass)
        self.pages: dict[str, int] = {}
        self.toc: list[tuple[str, str]] = []  # (tag, text) of chapter headings

    # -- small helpers -----------------------------------------------------------
    def _fmt(self, size=10.5, bold=False, color=INK, italic=False):
        from PySide6.QtGui import QColor, QFont, QTextCharFormat

        f = QTextCharFormat()
        font = QFont(self.family)
        font.setPointSizeF(size)
        font.setBold(bold)
        font.setItalic(italic)
        f.setFont(font)
        f.setForeground(QColor(color))
        return f

    def _block(self, tag=None, top=0, bottom=6, align=None, page_break=False, line=1.3):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QTextBlockFormat, QTextFormat

        b = QTextBlockFormat()
        b.setTopMargin(top * MM / 3)
        b.setBottomMargin(bottom * MM / 3)
        b.setLineHeight(line * 100, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        b.setAlignment(align or Qt.AlignmentFlag.AlignLeft)
        if page_break or (tag and tag in self.breaks):
            b.setPageBreakPolicy(QTextBlockFormat.PageBreakFlag.PageBreak_AlwaysBefore)
        if tag:
            b.setProperty(QTextFormat.Property.UserProperty + USER_ID, tag)
        return b

    def para(
        self,
        text,
        tag=None,
        size=10.5,
        bold=False,
        color=INK,
        top=0,
        bottom=6,
        align=None,
        page_break=False,
        italic=False,
        line=None,
    ):
        c = self.cursor
        line = line or (1.1 if size >= 12 else 1.3)
        if not c.atBlockStart() or c.block().length() > 1:
            c.insertBlock(
                self._block(tag, top, bottom, align, page_break, line),
                self._fmt(size, bold, color, italic),
            )
        else:
            c.setBlockFormat(self._block(tag, top, bottom, align, page_break, line))
        c.setCharFormat(self._fmt(size, bold, color, italic))
        c.insertText(text)

    def runs(self, parts, tag=None, bottom=6, top=0):
        """A paragraph of (text, bold) runs."""
        c = self.cursor
        c.insertBlock(self._block(tag, top, bottom), self._fmt())
        for text, bold in parts:
            c.setCharFormat(self._fmt(bold=bold))
            c.insertText(text)

    def heading1(self, text, tag):
        self.toc.append((tag, text))
        self.para(text, tag=tag, size=22, bold=True, color=BLUE, bottom=6, page_break=True)
        self.doc.documentLayout()  # (keeps the layout object alive)

    def heading2(self, text, tag=None):
        self.para(text, tag=tag, size=13.5, bold=True, color=BLUE, top=6, bottom=3)

    def markdown(self, md: str):
        """Insert markdown text with the manual's fonts."""
        from PySide6.QtGui import QTextDocument, QTextDocumentFragment

        tmp = QTextDocument()
        tmp.setDefaultFont(self.doc.defaultFont())
        tmp.setDocumentMargin(0)
        tmp.setDefaultStyleSheet(self.doc.defaultStyleSheet())
        tmp.setMarkdown(md)
        c = self.cursor
        c.insertBlock(self._block(bottom=2), self._fmt())
        c.insertFragment(QTextDocumentFragment(tmp))

    def picture(self, name: str, image, width_fraction=0.94):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QTextDocument, QTextImageFormat

        url = f"shot://{name}"
        self.doc.addResource(QTextDocument.ResourceType.ImageResource, QUrl(url), image)
        fmt = QTextImageFormat()
        fmt.setName(url)
        w = self.page_w * width_fraction / self.image_scale
        fmt.setWidth(w)
        fmt.setHeight(w * image.height() / image.width())
        c = self.cursor
        c.insertBlock(self._block(bottom=3, line=1.0), self._fmt())
        c.insertImage(fmt)

    # -- sections -------------------------------------------------------------------
    def title_page(self):
        from PySide6.QtCore import Qt

        center = Qt.AlignmentFlag.AlignHCenter
        self.para("mef3io-studio", size=40, bold=True, color=BLUE, top=40, bottom=4, align=center)
        self.para("User manual", size=24, color=INK, bottom=12, align=center)
        self.para(
            "How to open, check, trim, join and anonymize recordings",
            size=13,
            color=MUTED,
            bottom=16,
            align=center,
        )
        self.para(f"Version {self.version}", size=14, bold=True, bottom=2, align=center)
        self.para(self.date, size=12, color=MUTED, bottom=14, align=center)
        if self.cover:
            self.picture(*self.cover, 0.8)
        self.para(
            "All the pictures were taken from the app on made-up recordings. "
            "No patient data is shown.",
            size=9.5,
            color=MUTED,
            top=6,
            align=center,
        )

    def contents(self):

        self.para("Contents", tag="contents", size=22, bold=True, color=BLUE, page_break=True)
        self._toc_cursor_block = self.cursor.block().blockNumber()
        self._toc_start = self.cursor.position()
        # placeholder lines are written by write_toc() once the chapters exist
        self._toc_lines = []

    def write_toc(self, entries):
        """Fill in the contents (one line per chapter) at the reserved place."""
        from PySide6.QtGui import QTextCursor, QTextOption

        c = QTextCursor(self.doc)
        c.setPosition(self._toc_start)
        c.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        for tag, text in entries:
            fmt = self._block(bottom=3, top=1)
            fmt.setTabPositions(
                [
                    QTextOption.Tab(
                        (self.page_w - 12) / self.image_scale, QTextOption.TabType.RightTab
                    )
                ]
            )
            c.insertBlock(fmt, self._fmt())
            c.setCharFormat(self._fmt(11.5))
            c.insertText(f"{text}\t{self.pages.get(tag, '')}")

    def getting_started(self):
        self.heading1("1. Getting started", "start")
        self.para(
            "mef3io-studio is a desktop app for looking at, checking, trimming, joining "
            "and anonymizing MEF 3.0 recordings. The app never changes an original "
            "recording: everything that changes the content writes a new, checked copy.",
            bottom=8,
        )
        self.heading2("Install the app")
        self.markdown(markdown_section(ROOT / "README.md", "Getting the app"))
        self.heading2("Open your recordings")
        self.para(
            "Start the app and choose Explore in the list on the left. Choose Open a "
            "recording, or drag recordings from your file manager onto the window. A "
            "recording is a folder ending in .mefd, or a single file ending in .mefd.tar. "
            "To find every recording in a folder, choose Scan a folder. Opening a "
            "recording never changes it and does not load its signal, so even very "
            "large recordings open quickly.",
            bottom=8,
        )
        self.heading2("How this manual is organised")
        self.para(
            "Each chapter after this one is a how-to: the steps in order, each with a "
            "picture of the real app. An orange frame marks the part of the screen the "
            "step is about. The last chapters are short reference notes.",
        )

    def chapter(self, number: int, tour):
        tag = f"ch-{tour.key}"
        self.heading1(f"{number}. {tour.title}", tag)
        self.para(tour.summary, bottom=8)
        if tour.before:
            self.heading2("Before you start")
            for line in tour.before:
                self.para("•  " + line, bottom=3)
        self.heading2("Steps", tag=None)
        for i, rec in enumerate(self.shots[tour.key], start=1):
            sid = f"step-{tour.key}-{i}"
            self.para(
                f"{i}. {rec['title']}", tag=sid, size=12, bold=True, top=6, bottom=3, color=BLUE
            )
            self.picture(rec["image"], rec["qimage"])
            self.para(rec["caption"], bottom=3)
            then = rec["then"]
            end = f"{sid}-end"
            if then:
                self.para(
                    "Then " + " and ".join(then) + ".", tag=end, italic=True, color=MUTED, bottom=6
                )
            else:
                self.para("", tag=end, bottom=2)

    def reference(self, number: int):
        tag = "ref"
        self.heading1(f"{number}. Reference notes", tag)
        self.para(
            "Short notes from the user guide that the how-to chapters rely on. The full "
            "user guide (docs/user-guide.md) covers every page of the app.",
            bottom=6,
        )
        for title, path, heading in REFERENCE:
            self.heading2(title)
            self.markdown(markdown_section(ROOT / path, heading))

    def build(self, with_toc_pages: dict | None = None):
        from PySide6.QtGui import QTextCursor

        self.cursor = QTextCursor(self.doc)
        self.pages = with_toc_pages or {}
        self.title_page()
        self.contents()
        self.getting_started()
        number = 2
        for tour in self.tours:
            self.chapter(number, tour)
            number += 1
        self.reference(number)
        self.write_toc(self.toc)
        return self.doc

    # -- measuring ---------------------------------------------------------------------
    def tagged_blocks(self) -> dict[str, tuple[float, float]]:
        """tag -> (top, bottom) of each tagged block in document coordinates."""
        from PySide6.QtGui import QTextFormat

        out = {}
        self.doc.pageCount()  # lays the whole document out
        layout = self.doc.documentLayout()
        block = self.doc.begin()
        while block.isValid():
            tag = block.blockFormat().property(QTextFormat.Property.UserProperty + USER_ID)
            if tag:
                r = layout.blockBoundingRect(block)
                out[str(tag)] = (r.top(), r.bottom())
            block = block.next()
        return out

    def page_of(self, y: float) -> int:
        return int((y + 1) // self.page_h) + 1


def settle_layout(make_manual, tours_count_hint=0):
    """Build, find step cards that straddle a page boundary, force a page break
    before them, and build again (a few rounds); then fill in the contents'
    page numbers with one more build. Returns the final Manual."""
    breaks: set[str] = set()
    pages: dict[str, int] = {}
    for _ in range(12):
        m = make_manual()
        m.breaks = set(breaks)
        m.build(pages)
        marks = m.tagged_blocks()
        new = set()
        for tag, (top, _bottom) in marks.items():
            if not tag.startswith("step-") or tag.endswith("-end"):
                continue
            end = marks.get(f"{tag}-end", (top, top))[1]
            if m.page_of(top) != m.page_of(end - 1) and tag not in breaks:
                new.add(tag)
        pages = {tag: m.page_of(marks[tag][0]) for tag, _t in m.toc if tag in marks}
        if not new:
            return m, pages, marks
        breaks |= new
    raise RuntimeError("The manual's page breaks did not settle.")


def final_manual(make_manual):
    # Rounds until stable, with the contents' page numbers filled in.
    m, pages, _marks = settle_layout(make_manual)
    breaks = set(m.breaks)
    for _ in range(4):
        m2 = make_manual()
        m2.breaks = set(breaks)
        m2.build(pages)
        marks = m2.tagged_blocks()
        pages2 = {tag: m2.page_of(marks[tag][0]) for tag, _t in m2.toc if tag in marks}
        if pages2 == pages:
            return m2
        pages = pages2
    return m2


# --------------------------------------------------------------------------
# Output


def paint_pages(manual: Manual, device_for_page, finish_page):
    """Draw every page with its footer. ``device_for_page(i)`` returns the
    QPainter to use for page ``i``; ``finish_page(i, painter)`` closes it."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont

    doc = manual.doc
    pages = doc.pageCount()
    for i in range(pages):
        painter, dev = device_for_page(i)
        w, h = dev.width(), dev.height()
        painter.fillRect(QRectF(0, 0, w, h), QColor("white"))
        painter.save()
        painter.translate(MARGIN_MM * MM, MARGIN_MM * MM - i * manual.page_h)
        doc.drawContents(painter, QRectF(0, i * manual.page_h, manual.page_w, manual.page_h))
        painter.restore()
        if i > 0:  # no footer on the title page
            font = QFont(manual.family)
            font.setPointSizeF(8.5)
            painter.setFont(font)
            painter.setPen(QColor(MUTED))
            y = h - MARGIN_MM * MM + 2 * MM
            area = QRectF(MARGIN_MM * MM, y, manual.page_w, 6 * MM)
            painter.drawText(
                area,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                f"mef3io-studio user manual, version {manual.version}",
            )
            painter.drawText(
                area,
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                f"Page {i + 1} of {pages}",
            )
        finish_page(i, painter)
    return pages


def write_pdf(make_manual, path: Path) -> int:
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize, QPainter, QPdfWriter

    writer = QPdfWriter(str(path))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
    writer.setResolution(DPI)
    writer.setTitle("mef3io-studio user manual")
    writer.setCreator("mef3io-studio manual builder")
    manual = final_manual(lambda: make_manual(writer))
    painter = QPainter(writer)

    def device_for_page(i):
        if i:
            writer.newPage()
        return painter, writer

    pages = paint_pages(manual, device_for_page, lambda i, p: None)
    painter.end()
    return pages


def write_previews(make_manual, folder: Path) -> int:
    """Page images of the same layout (for a look without a PDF viewer)."""
    from PySide6.QtGui import QImage, QPainter

    folder.mkdir(parents=True, exist_ok=True)
    w, h = round(210 * MM), round(297 * MM)

    def blank():
        img = QImage(w, h, QImage.Format.Format_RGB32)
        img.setDotsPerMeterX(round(DPI / 0.0254))
        img.setDotsPerMeterY(round(DPI / 0.0254))
        return img

    probe = blank()
    manual = final_manual(lambda: make_manual(probe))
    state = {}

    def device_for_page(i):
        img = blank()
        state["img"] = img
        return QPainter(img), img

    def finish_page(i, painter):
        painter.end()
        state["img"].save(str(folder / f"page-{i + 1:02d}.png"))

    return paint_pages(manual, device_for_page, finish_page)


# --------------------------------------------------------------------------


def collect_shots(tours, shots_dir: Path | None):
    """Run every tour (or reuse ``shots_dir``) -> {tour key: [records]} with
    the screenshots loaded as QImage."""
    from PySide6.QtGui import QImage

    out = {}
    for tour in tours:
        folder = shots_dir or tourlib.scratch("manual-shots-")
        if shots_dir is None or not (folder / f"{tour.key}.json").exists():
            print(f"running {tour.key}: {tour.title}", flush=True)
            run_tour.run_to_folder(tour, folder)
        records = json.loads((folder / f"{tour.key}.json").read_text(encoding="utf-8"))
        for rec in records:
            rec["qimage"] = QImage(str(folder / rec["image"]))
        out[tour.key] = records
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(ROOT / "dist" / "manual"))
    ap.add_argument("--shots", help="reuse screenshots in this folder (made if missing)")
    ap.add_argument("--preview", help="also save page images in this folder")
    ap.add_argument("--tour", action="append", help="only these tours (default: all)")
    args = ap.parse_args(argv)

    tourlib.prepare_environment(Path(tourlib.scratch("manual-env-")))
    from mef3io_studio._version import app_version
    from mef3io_studio.app import create_application

    app = create_application()  # noqa: F841 (fonts need an application)
    tours = tourlib.load_tours(args.tour)
    shots_dir = Path(args.shots) if args.shots else None
    if shots_dir:
        shots_dir.mkdir(parents=True, exist_ok=True)
    shots = collect_shots(tours, shots_dir)
    records = next(iter(shots.values()))
    first = records[min(2, len(records) - 1)]
    version = app_version()
    date = datetime.date.today().strftime("%d %B %Y").lstrip("0")

    def make_manual(device):
        return Manual(tours, shots, (first["image"], first["qimage"]), version, date, device)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pdf = out / f"mef3io-studio-manual-{version}.pdf"
    pages = write_pdf(make_manual, pdf)
    print(f"{pdf}  {pdf.stat().st_size / 1e6:.1f} MB, {pages} pages")
    if args.preview:
        write_previews(make_manual, Path(args.preview))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
