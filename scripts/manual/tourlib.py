"""Tours: the single source of truth for the user manual and the tutorial videos.

A tour (``docs/manual/tours/*.py``) defines ``TOUR``: an ordered list of
steps. Each step drives the REAL main window (select rows, click a button by
the text it shows, type into a field, drag a line on the map) and has a
caption in plain words. The driver here runs a tour against made-up
recordings and tells a *sink* what happened, so one run can make
screenshots for the PDF (``build_manual.py``) or frames for a video
(``build_videos.py``). Nothing here reads real data.

A tour file::

    from tourlib import Tour, Step, click, nav, pick_rows, type_in, wait

    TOUR = Tour(
        key="t9", title="...", summary="...", steps=[
            Step("Open Explore", "Choose Explore in the list on the left.",
                 do=[nav("Explore")]),
            ...
        ])

Targets (what to point at / click) are the visible text of a button, radio
button, check box, tab or sidebar entry (``"Next"``), a widget, or a function
``f(ctx) -> QWidget | QRect | None``. A target that cannot be found stops the
tour with a clear message: that is how a changed screen breaks a tour in CI.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOURS_DIR = ROOT / "docs" / "manual" / "tours"

#: The made-up recordings use 2000-01-06 and later (no real dates).
SECOND = 1_000_000
HOUR = 3600 * SECOND
T0 = 946_684_800_000_000 + 5 * 24 * HOUR  # 2000-01-06 00:00 UTC
NIGHT_HOURS = 2  # each made-up night is two hours long


# --------------------------------------------------------------------------
# The environment (before the app is imported)


def prepare_environment(data_dir: Path) -> None:
    """A private data folder, no start-up questions, no sleep inhibitor, a
    private log folder, and the offscreen platform unless one is set."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["MEF3IO_STUDIO_DATA_DIR"] = str(data_dir)
    os.environ["MEF3IO_STUDIO_NO_STARTUP_OFFERS"] = "1"
    os.environ["MEF3IO_STUDIO_NO_KEEP_AWAKE"] = "1"
    os.environ.setdefault("MEF3IO_STUDIO_LOG_DIR", str(data_dir / "logs"))


# --------------------------------------------------------------------------
# Made-up recordings


@dataclass
class Demo:
    """Where the made-up recordings are and what they hold."""

    folder: Path
    #: name -> path of the .mefd folder
    paths: dict[str, Path] = field(default_factory=dict)
    #: (start_us, end_us) of each recording, by name
    spans: dict[str, tuple[int, int]] = field(default_factory=dict)
    channels: tuple[str, ...] = ()
    out: Path | None = None

    @property
    def names(self) -> list[str]:
        return list(self.paths)


NIGHTS = ("study-monday", "study-tuesday", "study-wednesday")
CHANNELS = ("F3", "F4", "C3", "C4", "ECG", "EMG")


def write_demo(folder: Path, fs: float = 256.0) -> Demo:
    """Three made-up nights of 2 hours each (22:00 to 24:00 UTC on 6, 7 and
    8 January 2000) sampled at 256 Hz, six channels (ECG and EMG are the
    ones the tours leave out), a few notes and events, and a ten-minute gap
    in the second night (at 22:50). A made-up patient."""
    import mef3io
    import numpy as np

    folder.mkdir(parents=True, exist_ok=True)
    demo = Demo(folder=folder, channels=CHANNELS, out=folder.parent / "My recordings")
    demo.out.mkdir(exist_ok=True)
    meta = mef3io.Metadata(
        subject=mef3io.Subject(name_1="Demo", name_2="Person", id="X-0001"),
    )
    rng = np.random.default_rng(7)
    for night, name in enumerate(NIGHTS):
        start = T0 + (22 + 24 * night) * HOUR
        path = folder / f"{name}.mefd"
        n = int(NIGHT_HOURS * 3600 * fs)
        t = np.arange(n) / fs  # seconds
        with mef3io.Writer(str(path), overwrite=True, units="uV", metadata=meta) as w:
            for c, ch in enumerate(CHANNELS):
                sig = (
                    30 * np.sin(2 * np.pi * (1.5 + 0.2 * c) * t)  # slow waves
                    + 12 * np.sin(2 * np.pi * (9.0 + 0.5 * c) * t + night)  # alpha
                    + 5 * rng.standard_normal(n)
                ).round(1)
                if night == 1:
                    cut = int(50 * 60 * fs)  # a gap of ten minutes after 50 minutes
                    gap = int(10 * 60 * fs)
                    w.write(ch, sig[:cut], start, fs)
                    w.write(ch, sig[cut + gap :], start + round((cut + gap) * SECOND / fs), fs)
                else:
                    w.write(ch, sig, start, fs)
            w.write_annotations(
                [
                    {"time": start + 5 * 60 * SECOND, "text": "lights off"},
                    {"time": start + 40 * 60 * SECOND, "type": "Note", "text": "restless"},
                    {"time": start + 110 * 60 * SECOND, "text": "lights on"},
                ]
            )
        demo.paths[name] = path
        demo.spans[name] = (start, start + NIGHT_HOURS * HOUR)
    return demo


# --------------------------------------------------------------------------
# Targets and actions


def _strip(text: str) -> str:
    return text.replace("&", "").replace("…", "").replace("...", "").strip().lower()


def _scroll_into_view(widget) -> None:
    from PySide6.QtWidgets import QScrollArea

    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QScrollArea) and parent.widget() is not None:
            parent.ensureWidgetVisible(widget, 20, 60)
        parent = parent.parentWidget()


def scroll_to_top_of(widget, margin: int = 24) -> None:
    """Scroll the wizard so that ``widget`` starts near the top of the view."""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QScrollArea

    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QScrollArea) and parent.widget() is not None:
            y = widget.mapTo(parent.widget(), QPoint(0, 0)).y()
            parent.verticalScrollBar().setValue(max(0, y - margin))
        parent = parent.parentWidget()


def around(*widgets) -> Callable:
    """A target: the rectangle around several widgets (or functions
    ``f(ctx) -> widget``), scrolled so that the first one is near the top."""

    def find(ctx):
        from PySide6.QtCore import QRect

        found = [w(ctx) if callable(w) else w for w in widgets]
        scroll_to_top_of(found[0])
        ctx.app.processEvents()
        rects = [rect_in_window(ctx.window, w) for w in found]
        out = QRect(rects[0])
        for r in rects[1:]:
            out = out.united(r)
        return out

    return find


def rect_in_window(window, widget, local=None):
    """The rectangle of ``widget`` (or of ``local`` inside it) in window
    coordinates, scrolling it into view first."""
    from PySide6.QtCore import QPoint, QRect

    _scroll_into_view(widget)
    local = local if local is not None else QRect(QPoint(0, 0), widget.size())
    return QRect(widget.mapTo(window, local.topLeft()), local.size())


def find_target(window, text: str, want_action: bool = False):
    """The visible button, radio button, check box, tab or sidebar entry
    showing ``text`` (case and trailing ellipsis ignored). Exact matches win
    over 'starts with'. Returns a rectangle in window coordinates, or, with
    ``want_action``, ``(rectangle, function that clicks it)``."""
    from PySide6.QtWidgets import QAbstractButton, QLabel, QListWidget, QTabBar, QWidget

    want = _strip(text)
    exact: list = []
    prefix: list = []

    def consider(candidate: str, rect_fn, act_fn) -> None:
        have = _strip(candidate)
        if have == want:
            exact.append((rect_fn, act_fn))
        elif have.startswith(want):
            prefix.append((rect_fn, act_fn))

    for w in window.findChildren(QWidget):
        if not w.isVisible():
            continue
        if isinstance(w, QAbstractButton) and w.text():
            consider(w.text(), lambda w=w: rect_in_window(window, w), w.click)
        elif isinstance(w, QTabBar):
            for i in range(w.count()):
                consider(
                    w.tabText(i),
                    lambda w=w, i=i: rect_in_window(window, w, w.tabRect(i)),
                    lambda w=w, i=i: w.setCurrentIndex(i),
                )
        elif isinstance(w, QListWidget):
            for i in range(w.count()):
                item = w.item(i)
                consider(
                    item.text(),
                    lambda w=w, item=item: rect_in_window(
                        window, w.viewport(), w.visualItemRect(item)
                    ),
                    lambda w=w, i=i: w.setCurrentRow(i),
                )
        elif isinstance(w, QLabel) and w.text() and _strip(w.text()) == want:
            exact.append((lambda w=w: rect_in_window(window, w), None))
    found = exact or prefix
    if not found:
        raise LookupError(f"The tour looks for {text!r} on screen but it is not there.")
    rect_fn, act_fn = found[0]
    rect = rect_fn()
    return (rect, act_fn) if want_action else rect


def resolve(ctx, spec):
    """A target spec -> QRect in window coordinates (or None)."""
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QWidget

    if spec is None:
        return None
    if callable(spec) and not isinstance(spec, QWidget):
        spec = spec(ctx)
    if spec is None or isinstance(spec, QRect):
        return spec
    if isinstance(spec, QWidget):
        return rect_in_window(ctx.window, spec)
    return find_target(ctx.window, str(spec))


@dataclass
class Act:
    """One visible action: point at ``target``, then ``run``."""

    run: Callable
    target: object = None
    label: str = ""
    click: bool = True


def click(text: str, label: str | None = None) -> Act:
    """Click a button / radio button / check box / tab by its visible text."""

    def run(ctx):
        _rect, act = find_target(ctx.window, text, want_action=True)
        if act is None:
            raise LookupError(f"{text!r} is on screen but is not something to click.")
        widget = getattr(act, "__self__", None)
        if widget is not None and hasattr(widget, "isEnabled") and not widget.isEnabled():
            raise RuntimeError(f"{text!r} is on screen but cannot be clicked yet.")
        act()

    return Act(run, text, label if label is not None else f"choose {text}")


def nav(name: str) -> Act:
    """Choose a page in the list on the left."""

    def run(ctx):
        win = ctx.window
        for i in range(win.module_list.count()):
            if _strip(win.module_list.item(i).text()) == _strip(name):
                win.module_list.setCurrentRow(i)
                return
        raise LookupError(f"No page called {name!r} in the list on the left.")

    return Act(run, name, f"choose {name} in the list on the left")


def do(run: Callable, target=None, label: str = "", click: bool = True) -> Act:
    """Anything else: ``run(ctx)`` with an optional pointer target."""
    return Act(run, target, label, click)


def pick_rows(names: list[str]) -> list[Act]:
    """Select recordings in Explore's list, one after another (like Ctrl+click)."""
    acts = []
    for i, name in enumerate(names):

        def target(ctx, name=name):
            return row_rect(ctx, name)

        def run(ctx, name=name, first=(i == 0)):
            from PySide6.QtCore import QItemSelectionModel

            page = ctx.window.explorer
            entry = next(e for e in page.entries() if e.name == name)
            index = page.proxy.mapFromSource(page.model.index(page.model.row_of(entry), 0))
            flag = QItemSelectionModel.SelectionFlag
            flags = flag.Select | flag.Rows
            if first:
                flags |= flag.Clear
            page.table.selectionModel().select(index, flags)
            page.table.selectionModel().setCurrentIndex(index, flag.NoUpdate)

        acts.append(Act(run, target, "select " + name if i == 0 else f"and {name}"))
    return acts


def row_rect(ctx, name: str):
    page = ctx.window.explorer
    entry = next((e for e in page.entries() if e.name == name), None)
    if entry is None:
        raise LookupError(f"Explore does not list a recording called {name!r}.")
    index = page.proxy.mapFromSource(page.model.index(page.model.row_of(entry), 0))
    rect = page.table.visualRect(index)
    rect.setWidth(page.table.viewport().width())
    return rect_in_window(ctx.window, page.table.viewport(), rect)


def type_in(field_spec: Callable | object, text: str, label: str = "") -> Act:
    """Type ``text`` into a line edit (character by character) and press Enter."""

    def widget(ctx):
        return field_spec(ctx) if callable(field_spec) else field_spec

    def run(ctx):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        edit = widget(ctx)
        edit.setFocus()
        edit.clear()
        for i, ch in enumerate(text):
            QTest.keyClicks(edit, ch)
            if i % 2 == 1:
                ctx.tick()
        QTest.keyClick(edit, Qt.Key.Key_Return)
        ctx.settle(0.1)

    return Act(run, widget, label or f"type {text}")


def drag_line(which: str, to_us: int) -> Act:
    """Drag the Start or End line on the "What to keep" map to a moment."""

    def the_map(ctx):
        return ctx.window.joiner.keep.map

    def target(ctx):
        m = the_map(ctx)
        from PySide6.QtCore import QPoint, QRect

        x = round(m.x_of(m.start if which == "start" else m.end))
        y = m._metrics()[3] + 30
        r = QRect(QPoint(x - 12, y - 12), QPoint(x + 12, y + 12))
        return rect_in_window(ctx.window, m, r)

    def run(ctx):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        m = the_map(ctx)
        y = m._metrics()[3] + 30
        x0 = round(m.x_of(m.start if which == "start" else m.end))
        x1 = round(m.x_of(to_us))
        left = Qt.MouseButton.LeftButton
        QTest.mousePress(m, left, pos=QPoint(x0, y))
        steps = 10
        for i in range(1, steps + 1):
            x = round(x0 + (x1 - x0) * i / steps)
            QTest.mouseMove(m, QPoint(x, y))
            ctx.app.processEvents()
            ctx.tick(m.mapTo(ctx.window, QPoint(x, y)))
        QTest.mouseRelease(m, left, pos=QPoint(x1, y))
        ctx.settle(0.1)

    return Act(run, target, f"drag the {which.capitalize()} line", click=False)


def wait(cond: Callable, what: str, timeout: float = 120.0) -> Act:
    """Wait (no pointer) until ``cond(ctx)`` is true."""

    def run(ctx):
        ctx.wait_until(lambda: cond(ctx), timeout, what)

    return Act(run, None, "", click=False)


# --------------------------------------------------------------------------
# Tours


@dataclass
class Step:
    """One picture of the manual / one held scene of the video.

    ``do`` happens first (the pointer visits each target). Then the screen is
    shown with ``caption`` and a ring around ``focus`` (default: the last
    thing acted on). ``then`` (usually ``[click("Next")]``) happens after
    that, and the manual adds "Then choose Next."."""

    title: str
    caption: str
    do: list = field(default_factory=list)
    then: list = field(default_factory=list)
    focus: object = None
    hold: float = 3.5

    def acts(self, which: str) -> list[Act]:
        out = []
        for item in getattr(self, which):
            out.extend(item if isinstance(item, list) else [item])
        return out


@dataclass
class Tour:
    key: str
    title: str
    summary: str
    steps: list[Step]
    #: Short lines for "Before you start".
    before: list[str] = field(default_factory=list)
    #: "T1" style short code used in file names
    video_title: str = ""


def load_tours(only: list[str] | None = None) -> list[Tour]:
    """Every ``TOUR`` in docs/manual/tours/*.py, in file-name order."""
    import importlib.util
    import sys

    for folder in (str(TOURS_DIR), str(Path(__file__).resolve().parent)):
        if folder not in sys.path:
            sys.path.insert(0, folder)
    tours = []
    for path in sorted(TOURS_DIR.glob("t*.py")):
        spec = importlib.util.spec_from_file_location(f"manual_tour_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        tour = module.TOUR
        if not only or tour.key in only:
            tours.append(tour)
    return tours


# --------------------------------------------------------------------------
# The driver


class Sink:
    """What happens with a run. Override what you need."""

    def begin(self, tour: Tour, ctx: Ctx) -> None: ...

    def step_begin(self, ctx: Ctx, index: int, step: Step) -> None:
        """A step starts (before its actions)."""

    def pointer(self, ctx: Ctx, start, end) -> None:
        """The pointer moves from ``start`` to ``end`` (window points)."""

    def clicked(self, ctx: Ctx, at) -> None:
        """A click happened at ``at`` (already acted)."""

    def tick(self, ctx: Ctx, at) -> None:
        """Something moved during an action (typing, dragging, waiting)."""

    def shot(self, ctx: Ctx, index: int, step: Step, focus) -> None:
        """The scene after ``step.do``."""

    def end(self, ctx: Ctx) -> None: ...


class Ctx:
    def __init__(self, app, window, demo: Demo, sink: Sink):
        self.app, self.window, self.demo, self.sink = app, window, demo, sink
        self.pointer = None
        #: Shorter pauses (tests): the screen is the same, only faster.
        self.fast = False

    def settle(self, seconds: float = 0.3) -> None:
        if self.fast:
            seconds = min(seconds, 0.03)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def tick(self, at=None) -> None:
        self.sink.tick(self, at if at is not None else self.pointer)

    def wait_until(self, cond: Callable, timeout: float, what: str) -> None:
        end = time.monotonic() + timeout
        last = 0.0
        shown = 0
        while time.monotonic() < end:
            self.app.processEvents()
            if cond():
                self.settle(0.2)
                return
            if time.monotonic() - last > 0.6 and shown < 6:
                last, shown = time.monotonic(), shown + 1
                self.tick()
            time.sleep(0.01)
        raise TimeoutError(f"The tour waited {timeout:.0f} s for: {what}")

    # Helpers for tour files.
    def grab(self):
        self.app.processEvents()
        return self.window.grab().toImage()


def _center(rect):
    return rect.center() if rect is not None else None


def _run_acts(ctx: Ctx, acts: list[Act]):
    """Run the acts; returns the rectangle of the last one that had a target."""
    last = None
    for act in acts:
        rect = resolve(ctx, act.target)
        if rect is not None:
            ctx.sink.pointer(ctx, ctx.pointer, _center(rect))
            ctx.pointer = _center(rect)
            last = rect
        act.run(ctx)
        ctx.settle(0.15)
        if rect is not None and act.click:
            ctx.sink.clicked(ctx, _center(rect))
    return last


def run_tour(
    tour: Tour, sink: Sink, work: Path, size=(1280, 720), demo: Demo | None = None, fast=False
):
    """Run ``tour`` in a fresh main window on made-up recordings, calling
    ``sink`` as it goes. ``work`` is a scratch folder (kept)."""
    prepare_environment(work / "data")
    from mef3io_studio.app import build_main_window, create_application
    from mef3io_studio.common.settings import Settings

    app = create_application()
    demo = demo or write_demo(work / "recordings")
    window = build_main_window(app, Settings(work / "settings.ini"))
    window.resize(*size)
    window.show()
    window.activity_dock.hide()
    window.set_theme_mode("light")
    ctx = Ctx(app, window, demo, sink)
    ctx.fast = fast
    ctx.settle(0.5)
    ctx.pointer = window.rect().center()
    try:
        sink.begin(tour, ctx)
        for index, step in enumerate(tour.steps):
            sink.step_begin(ctx, index, step)
            focus = _run_acts(ctx, step.acts("do"))
            window.activity_dock.hide()
            ctx.settle(0.2)
            if step.focus is not None:
                focus = resolve(ctx, step.focus)
            then = step.acts("then")
            if focus is None and then:
                focus = resolve(ctx, then[0].target)
            sink.shot(ctx, index, step, focus)
            _run_acts(ctx, then)
        sink.end(ctx)
    finally:
        window._closing = True
        window.jobs.shutdown()
        window.close()
        app.processEvents()
    return ctx


def scratch(prefix="demo-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------
# Drawing (shared by the screenshots and the video frames)


def draw_highlight(image, rect, pad: int = 4):
    """Outline ``rect`` with a rounded orange ring, on a copy of ``image``."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QPainter, QPen

    out = image.copy()
    if rect is None:
        return out
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor("#e8590c"), 3))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(rect).adjusted(-pad, -pad, pad, pad), 6, 6)
    p.end()
    return out


def draw_pointer(image, at, ring: float = 0.0):
    """Draw a mouse pointer at ``at`` (in place); a click ring when ``ring`` > 0."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF

    if at is None:
        return
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    x, y = at.x(), at.y()
    if ring > 0:
        r = 8 + 22 * ring
        c = QColor("#e8590c")
        c.setAlphaF(max(0.0, 1 - ring))
        p.setPen(QPen(c, 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(x, y), r, r)
    arrow = QPolygonF(
        [
            QPointF(x + dx, y + dy)
            for dx, dy in ((0, 0), (0, 17), (4, 13), (8, 21), (11, 20), (7, 12), (13, 12))
        ]
    )
    p.setPen(QPen(QColor("black"), 1.5))
    p.setBrush(QColor("white"))
    p.drawPolygon(arrow)
    p.end()


def draw_caption(image, text: str, step_label: str = "", bar: int = 100):
    """A new image: ``image`` with a dark caption bar added below it. The
    text shrinks (down to 15 px) until it fits the bar."""
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter

    out = QImage(image.width(), image.height() + bar, QImage.Format.Format_RGB32)
    out.fill(QColor("#1f2933"))
    p = QPainter(out)
    p.drawImage(0, 0, image)
    p.setPen(QColor("white"))
    area = QRect(24, image.height() + 6, image.width() - 48, bar - 12)
    flags = int(
        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap
    )
    full = (f"{step_label}  " if step_label else "") + text
    font = QFont(p.font())
    for px in range(22, 14, -1):
        font.setPixelSize(px)
        if QFontMetrics(font).boundingRect(area, flags, full).height() <= area.height():
            break
    p.setFont(font)
    p.drawText(area, flags, full)
    p.end()
    return out
