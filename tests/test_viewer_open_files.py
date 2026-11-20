# ruff: noqa: F811, F401
"""The viewer keeps signal files open between reads (mef3io 2.4 ``open_files``) and lets go of
them whenever something else may need the recording: closing the window, saving or removing notes,
every job the app starts, and a quiet moment. Synthetic recordings in tmp_path only."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import mef3io
import numpy as np
import pytest
from test_viewer_edit import Spy, ed, turn_on  # noqa: F401
from test_viewer_marks_ui import make_window, pair, wait_marks, write  # noqa: F401

from mef3io_studio.model import open_files as of
from mef3io_studio.model.viewer_source import open_recordings
from mef3io_studio.workers.jobs import JobManager

T0 = 946_684_800_000_000
SEC = 1_000_000
LINUX = sys.platform.startswith("linux")


# --- the number ---------------------------------------------------------------------------------


def test_the_cap_follows_the_descriptor_limit_of_each_platform():
    assert of.cap("linux", 1024) == 128  # an eighth of the usual soft limit
    assert of.cap("linux", 65536) == of.HARD_CAP  # never more than the hard cap
    assert of.cap("darwin", 256) == 32  # a GUI app on macOS starts with 256
    # Windows: the library's own suggestion (assumed, not measured here)
    assert of.cap("win32") == of.WINDOWS_SUGGESTED == 128
    assert of.cap("linux", 4) == 0  # no room: keep nothing open


def test_the_budget_is_two_files_per_channel_within_the_cap():
    assert of.budget(0, "linux", 1024) == 0
    assert of.budget(3, "linux", 1024) == 6
    assert of.budget(16, "linux", 1024) == 32
    assert of.budget(256, "linux", 1024) == 128
    assert of.budget(256, "win32") == 128
    assert of.budget(40, "win32") == 80
    assert of.budget(256, "darwin", 256) == 32
    assert of.budget(3, "linux", 4) == 0


def test_the_viewer_opens_its_reader_with_the_budget(tmp_path):
    p = write(tmp_path / "a.mefd")
    data = open_recordings([str(p)])
    try:
        assert data.open_files == of.budget(len(data.channels)) > 0
    finally:
        data.close()


def test_two_recordings_share_one_budget(pair):
    data = open_recordings([str(p) for p in pair])
    try:
        assert data.open_files == of.budget(len(data.channels))
    finally:
        data.close()


# --- files are really held, and let go ------------------------------------------------------------


#: Whether this system lists a process's open files as /proc does (Linux). macOS and Windows
#: give no such list without a helper program or a new dependency, so the checks that need the
#: list run on Linux only; what does not need it is kept on every platform.
HAVE_FD_LIST = os.path.isdir("/proc/self/fd")


def held_signal_files(under) -> list[str]:
    """The signal files below ``under`` that this process holds open right now (other tests'
    windows may still hold theirs: only this test's folder counts)."""
    out = []
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if target.endswith(".tdat") and target.startswith(str(under)):
            out.append(target)
    return out


@pytest.mark.skipif(not LINUX, reason="counts descriptors through /proc")
def test_files_stay_open_between_reads_and_release_closes_them(tmp_path):
    p = write(tmp_path / "a.mefd", seconds=120)
    data = open_recordings([str(p)])
    try:
        names = list(data.channels)
        rate = data.rate(names[0])
        data.read_tile(names, rate, 0)
        held = held_signal_files(tmp_path)
        assert 0 < len(held) <= data.open_files  # kept, and never more than the budget
        data.read_tile(names, rate, 1)
        assert sorted(held_signal_files(tmp_path)) == sorted(held)  # the same, not new ones
        data.release_files()
        assert held_signal_files(tmp_path) == []
        data.read_tile(names, rate, 0)  # the reader is still usable
        assert held_signal_files(tmp_path)
        data.close()
        assert held_signal_files(tmp_path) == []
    finally:
        data.close()


@pytest.mark.skipif(not LINUX, reason="counts descriptors through /proc")
def test_a_quiet_viewer_lets_go_by_itself(make_window, tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr(of, "IDLE_RELEASE", 0.3)
    win = make_window(write(tmp_path / "a.mefd"))
    names = list(win.data.channels)
    win.data.read_tile(names, win.data.rate(names[0]), 0)
    assert held_signal_files(tmp_path)
    qtbot.waitUntil(lambda: not held_signal_files(tmp_path), timeout=10_000)
    assert win.data.open_files > 0  # only the held files went; the setting stays


@pytest.mark.skipif(not LINUX, reason="counts descriptors through /proc")
def test_closing_the_window_lets_go_of_every_file(make_window, tmp_path, qtbot):
    win = make_window(write(tmp_path / "a.mefd"))
    names = list(win.data.channels)
    win.data.read_tile(names, win.data.rate(names[0]), 0)
    assert held_signal_files(tmp_path)
    win.close()
    qtbot.waitUntil(lambda: not held_signal_files(tmp_path), timeout=10_000)


def test_the_window_asks_the_data_to_release_when_it_closes(make_window, tmp_path, qtbot):
    win = make_window(write(tmp_path / "a.mefd"))
    seen = []
    real = win.data.release_files
    win.data.release_files = lambda: (seen.append("release"), real())[1]
    win.close()
    qtbot.waitUntil(lambda: bool(seen), timeout=10_000)


# --- fewer opens while paging (counted by the system, not by the library) -------------------------


class OpenCounter:
    """Counts how often the system opens a signal file (``.tdat``): the kernel's own
    ``inotify`` on the folders that hold them, so the C++ side of the library is counted too
    (a Python audit hook cannot see it). Linux only; in this process, no helper program."""

    IN_OPEN = 0x20

    def __init__(self, recording: Path):
        import ctypes

        self._libc = ctypes.CDLL(None, use_errno=True)  # the C library already in the process
        self._fd = self._libc.inotify_init1(os.O_NONBLOCK)
        if self._fd < 0:
            raise OSError("inotify is not available")
        folders = {str(f.parent) for f in Path(recording).rglob("*.tdat")}
        for folder in folders:
            if self._libc.inotify_add_watch(self._fd, folder.encode(), self.IN_OPEN) < 0:
                raise OSError("cannot watch a folder")

    def count(self) -> int:
        """The opens since the last call."""
        import struct

        n = 0
        while True:
            try:
                data = os.read(self._fd, 65536)
            except BlockingIOError:
                return n
            at = 0
            while at < len(data):
                _wd, _mask, _cookie, size = struct.unpack_from("iIII", data, at)
                name = data[at + 16 : at + 16 + size].split(b"\0", 1)[0]
                n += name.endswith(b".tdat")
                at += 16 + size

    def close(self) -> None:
        os.close(self._fd)


def page_over(data, names, rate, repeats=3) -> None:
    for _ in range(repeats):
        for k in range(min(data.tile_count(rate), 6)):
            data.read_tile(names, rate, k)


@pytest.mark.skipif(not LINUX, reason="counts opens through inotify")
def test_paging_opens_far_fewer_signal_files_with_open_files_on(tmp_path):
    # 240 s at 2048 Hz: eight tiles of one channel group, six of them are read three times
    p = tmp_path / "a.mefd"
    fs = 2048.0
    x = np.sin(np.arange(int(240 * fs)) / 20.0)
    with mef3io.Writer(str(p), overwrite=True, units="uV") as w:
        for name in ("Fp1", "Fp2", "Cz", "Pz"):
            w.write(name, np.round(40 * x, 2), T0, fs)
    data = open_recordings([str(p)])
    try:
        names = list(data.channels)
        rate = data.rate(names[0])
        counter = OpenCounter(p)
    except OSError:
        data.close()
        pytest.skip("inotify is not available here")
    try:
        data.set_open_files(0)  # as the library always did: every read opens and closes
        counter.count()
        page_over(data, names, rate)
        off = counter.count()
        data.set_open_files(of.budget(len(names)))  # as the viewer opens it
        data.release_files()
        counter.count()
        page_over(data, names, rate)
        on = counter.count()
    finally:
        counter.close()
        data.close()
    assert off >= 10 * on, (off, on)  # 6 tiles x 3 passes without, one open per channel with
    assert 0 < on <= len(names) + 2, on  # one open per channel, however often it is read


# --- the app lets go before it writes -------------------------------------------------------------


def test_a_job_starts_after_the_prologue_on_its_own_thread(qtbot, qapp):
    jobs = JobManager(qapp)
    order = []
    jobs.prologue = lambda: order.append(("prologue", threading.current_thread().name))
    handle = jobs.submit(
        "A job", lambda ctx: order.append(("job", threading.current_thread().name))
    )
    with qtbot.waitSignal(handle.succeeded, timeout=10_000):
        pass
    assert [o[0] for o in order] == ["prologue", "job"]
    assert (
        order[0][1] == order[1][1] != threading.main_thread().name
    )  # a worker thread, not the GUI


def test_a_failing_prologue_never_stops_the_job(qtbot, qapp):
    jobs = JobManager(qapp)
    jobs.prologue = lambda: 1 / 0
    ran = []
    handle = jobs.submit("A job", lambda ctx: ran.append(1))
    with qtbot.waitSignal(handle.succeeded, timeout=10_000):
        pass
    assert ran == [1]


def test_saving_notes_releases_the_files_before_the_first_write(ed, qtbot, monkeypatch):
    from test_viewer_edit import finish_save

    win, spy, _ = ed
    order = []
    real_release = win.data.release_files
    win.data.release_files = lambda: (order.append("release"), real_release())[1]
    real_writer = mef3io.Writer
    monkeypatch.setattr(
        mef3io, "Writer", lambda *a, **k: (order.append("write"), real_writer(*a, **k))[1]
    )
    turn_on(win)
    win.edit.add(T0 + 30 * SEC, None, "Spike", "x")
    finish_save(qtbot, win)
    assert not spy.problems
    assert "release" in order and "write" in order
    assert order.index("release") < order.index("write")


@pytest.mark.skipif(not LINUX, reason="counts descriptors through /proc")
def test_starting_any_task_in_the_app_lets_the_viewers_go_of_their_files(window, tmp_path, qtbot):
    """Rename, join, anonymize, pack and "make them again" all start as tasks of the one job
    manager: none meets a recording that a viewer holds open."""
    from types import SimpleNamespace

    data = open_recordings([str(write(tmp_path / "a.mefd"))])
    window.viewers.append(SimpleNamespace(data=data))
    try:
        names = list(data.channels)
        data.read_tile(names, data.rate(names[0]), 0)
        assert held_signal_files(tmp_path)
        handle = window.jobs.submit("Some task", lambda ctx: 1, changes_recordings=False)
        with qtbot.waitSignal(handle.finished, timeout=20_000):
            pass
        assert held_signal_files(tmp_path) == []
    finally:
        window.viewers.clear()
        data.close()


def test_the_app_releases_every_viewer_before_a_job():
    seen = []

    class Data:
        def release_files(self):
            seen.append("released")

    class Window:
        data = Data()

    assert of.release_all([Window(), Window()]) == 2
    assert seen == ["released", "released"]

    class Broken:
        class data:  # noqa: N801
            @staticmethod
            def release_files():
                raise OSError("gone")

    assert of.release_all([Broken(), Window()]) == 1  # one that fails is skipped
