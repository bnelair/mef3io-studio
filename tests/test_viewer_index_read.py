# ruff: noqa: F811
"""The ONE automatic read of the whole recording's block index (the overview band and the
strip's real stretches share it): after the first page, never before the page and its look-ahead
are loaded, never again on paging, and never on the GUI thread. Synthetic recordings only."""

from __future__ import annotations

import threading
import time

import pytest
from test_viewer_marks_ui import pair, write  # noqa: F401
from test_viewer_ui import LIGHT, open_data, settle

from mef3io_studio.viewer.window import ViewerWindow

T0 = 946_684_800_000_000


class Spied:
    """A window whose data records every index read and every tile read."""

    def __init__(self, qtbot, *paths, slow_tiles: float = 0.0):
        self.data = open_data(*paths)
        self.events: list[tuple] = []
        self.threads: list[threading.Thread] = []
        data = self.data
        real_many, real_one, real_tile = data.envelope_many, data.envelope, data.read_tile
        self.win: ViewerWindow | None = None

        def state():
            ctl = self.win.controller if self.win is not None else None
            if ctl is None:
                return (None, None)
            return (len(ctl._jobs), ctl.scheduler.pending())

        def many(names, *a, **k):
            self.events.append(("index", tuple(names), time.monotonic(), state()))
            self.threads.append(threading.current_thread())
            return real_many(names, *a, **k)

        def one(name, *a, **k):
            self.events.append(("index", (name,), time.monotonic(), state()))
            self.threads.append(threading.current_thread())
            return real_one(name, *a, **k)

        def tile(*a, **k):
            if slow_tiles:
                time.sleep(slow_tiles)
            out = real_tile(*a, **k)
            self.events.append(("tile", None, time.monotonic(), None))
            return out

        data.envelope_many, data.envelope, data.read_tile = many, one, tile
        self.created = time.monotonic()
        self.win = ViewerWindow(data, theme=LIGHT)
        qtbot.addWidget(self.win)
        self.win.resize(1200, 700)
        self.win.show()
        self.qtbot = qtbot

    @property
    def index_reads(self):
        return [e for e in self.events if e[0] == "index"]

    def wait_index(self, timeout=30_000):
        self.qtbot.waitUntil(lambda: bool(self.index_reads), timeout=timeout)
        ctl = self.win.controller
        self.qtbot.waitUntil(
            lambda: all(c in ctl.overview for c in self.win.overview_channels()),
            timeout=timeout,
        )

    def close(self):
        self.win.close()


@pytest.fixture
def spied(qtbot, qapp):
    made = []

    def make(*paths, **kw):
        s = Spied(qtbot, *paths, **kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def test_the_index_is_read_only_after_the_page_and_its_look_ahead_are_loaded(spied, tmp_path):
    s = spied(write(tmp_path / "a.mefd", seconds=120), slow_tiles=0.15)
    s.wait_index()
    first = s.index_reads[0]
    assert any(e[0] == "tile" for e in s.events if e[2] < first[2])  # the page came first
    assert first[3] == (0, 0), first[3]  # nothing was waiting for a block when it began


def test_the_strip_and_the_band_share_one_call(spied, pair):
    s = spied(*pair)
    s.wait_index()
    settle(s.qtbot, s.win)
    reads = s.index_reads
    assert len(reads) == 1, [r[1] for r in reads]  # one call for strip channels + band channel
    band = s.win.overview_channels()
    assert set(band) <= set(reads[0][1])


def test_paging_never_reads_the_index_again(spied, tmp_path):
    s = spied(write(tmp_path / "a.mefd", seconds=300))
    s.wait_index()
    settle(s.qtbot, s.win)
    done = len(s.index_reads)
    s.win.set_timebase(2)
    for _ in range(4):
        s.win.move_by(0.5 * s.win.page_us(), 1)
        settle(s.qtbot, s.win)
    s.win.go_to(T0 + 10_000_000)
    settle(s.qtbot, s.win)
    s.qtbot.wait(300)
    assert len(s.index_reads) == done


def test_the_index_is_never_read_on_the_gui_thread(spied, tmp_path):
    s = spied(write(tmp_path / "a.mefd"))
    s.wait_index()
    assert s.threads and all(t is not threading.main_thread() for t in s.threads)
    assert all(t.name.startswith("viewer-strip") for t in s.threads)


def test_a_viewer_that_never_gets_quiet_still_reads_the_index(spied, tmp_path, monkeypatch):
    """The wait for a quiet scheduler is bounded, so the strip is never starved."""
    from mef3io_studio.viewer import controller as ctl
    from mef3io_studio.workers.viewer_scheduler import TileScheduler

    monkeypatch.setattr(ctl, "INDEX_WAIT", 0.3)
    monkeypatch.setattr(TileScheduler, "pending", lambda self: 1)  # never quiet
    s = spied(write(tmp_path / "a.mefd"))
    s.wait_index()
    assert s.index_reads and s.index_reads[0][2] - s.created >= 0.25  # it did wait, then went on
