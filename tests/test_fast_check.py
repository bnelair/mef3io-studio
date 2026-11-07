"""The check before Join or trim starts uses what Explore already knows: it
does not open the recordings again, does not walk their folders, and the run
does not repeat the work. Synthetic recordings only."""

from __future__ import annotations

import os

import mef3io
import numpy as np
import pytest
from test_anonymizer_ui import open_in_explorer
from test_join_anonymous import FS, HOUR, T0, S, write
from test_joiner_ui import choose_all, wait_for_map

from mef3io_studio.model import join as j
from mef3io_studio.model.cancel import CancelToken
from mef3io_studio.model.inspect import inspect_recording


@pytest.fixture
def window_page(window):
    window._closing = True
    window.show()
    window.module_list.setCurrentRow(window.module_keys.index("joiner"))
    return window, window.joiner


def facts(path) -> j.Layout:
    out = inspect_recording(str(path))
    d = out.details
    return j.Layout(d.start, d.end, tuple(c.name for c in d.channels), out.size_bytes, out.stamp)


def request_for(tmp_path, with_facts: bool, **kwargs):
    a = write(tmp_path / "A.mefd", T0, seconds=600)
    b = write(tmp_path / "B.mefd", T0 + HOUR, seconds=600)
    keep = j.Keep(j.KEEP_FIRST, duration_us=120_000_000)
    sources = [j.JoinSource(p.name, str(p), keep) for p in (a, b)]
    layouts = {s.key: facts(s.source) for s in sources} if with_facts else {}
    return j.new_request(sources, str(tmp_path / "out.mefd"), layouts=layouts, **kwargs), (a, b)


class Spy:
    def __init__(self, monkeypatch):
        self.opened = 0
        self.times = 0
        self.walked = 0
        real_reader, real_time, real_walk = mef3io.Reader, j.read_time, j._folder_bytes

        def reader(*a, **k):
            self.opened += 1
            return real_reader(*a, **k)

        def read_time(*a, **k):
            self.times += 1
            return real_time(*a, **k)

        def folder_bytes(*a, **k):
            self.walked += 1
            return real_walk(*a, **k)

        monkeypatch.setattr(mef3io, "Reader", reader)
        monkeypatch.setattr(j, "read_time", read_time)
        monkeypatch.setattr(j, "_folder_bytes", folder_bytes)


def test_the_check_uses_what_explore_knows_and_opens_nothing(tmp_path, monkeypatch):
    request, _ = request_for(tmp_path, with_facts=True)
    spy = Spy(monkeypatch)
    assert j.check_join(request) == []
    assert (spy.opened, spy.times, spy.walked) == (0, 0, 0)


def test_without_facts_the_recordings_are_opened(tmp_path, monkeypatch):
    request, _ = request_for(tmp_path, with_facts=False)
    spy = Spy(monkeypatch)
    assert j.check_join(request) == []
    assert spy.opened == 2 and spy.times == 2 and spy.walked == 2


def test_the_facts_give_the_same_plan_and_size_as_opening(tmp_path):
    with_facts, _ = request_for(tmp_path, with_facts=True)
    without = j.without_facts(with_facts)
    from mef3io_studio.model.cancel import CancelToken

    known, found = (
        j.read_layouts(with_facts, CancelToken())[0],
        j.read_layouts(without, CancelToken())[0],
    )
    assert not j.read_layouts(with_facts, CancelToken())[1]
    for key in known:
        assert known[key].start == found[key].start
        assert known[key].channels == found[key].channels
        assert abs(known[key].end - found[key].end) <= 1_000_000  # one sample at most
    plan = j.plan_for(with_facts, known)
    assert plan.ok
    a = j.sizing(with_facts, plan, known).total
    b = j.sizing(without, j.plan_for(without, found), found).total
    assert a > 0 and 0.9 < a / b < 1.1


def test_the_size_estimate_counts_every_file_once(tmp_path):
    root = tmp_path / "r"
    (root / "x" / "y").mkdir(parents=True)
    sizes = {"a": 10, "x/b": 200, "x/y/c": 3000, "x/y/d": 4}
    for name, n in sizes.items():
        (root / name).write_bytes(b"0" * n)
    assert j._folder_bytes(str(root)) == sum(sizes.values())
    assert j._folder_bytes(str(tmp_path / "missing")) == 0
    # the same number os.walk gives
    walked = sum(
        os.stat(os.path.join(d, f)).st_size for d, _s, files in os.walk(root) for f in files
    )
    assert walked == j._folder_bytes(str(root))


def test_the_stamp_notices_a_touched_recording(tmp_path):
    request, (a, _b) = request_for(tmp_path, with_facts=True)
    assert j.facts_still_true(request)
    os.utime(a, ns=(1, 2))
    assert not j.facts_still_true(request)
    # no facts, or a recording that is gone: not "still true"
    assert not j.facts_still_true(j.without_facts(request))
    gone = j.new_request([j.JoinSource("z", str(tmp_path / "none.mefd"))], "d")
    assert not j.facts_still_true(gone)


def append(path, start_us, seconds=60):
    """Add data to a recording after it was read (what a recording that is
    still being written does)."""
    n = int(seconds * FS)
    values = (start_us - T0) / S + np.arange(n) / FS
    with mef3io.Writer(str(path)) as w:
        for name in ("a1", "a2", "a3"):
            w.write(name, values, start_us, FS)


def test_data_appended_after_explore_read_it_is_noticed(tmp_path):
    request, (a, _b) = request_for(tmp_path, with_facts=True)
    assert j.facts_still_true(request)
    old_end = request.layouts[request.sources[0].key].end
    append(a, old_end + 1_000_000)
    assert not j.facts_still_true(request)
    fresh = j.read_layouts(j.without_facts(request), CancelToken())[0]
    assert fresh[request.sources[0].key].end > old_end


def test_no_recording_path_is_touched_twice_by_the_studio(tmp_path, monkeypatch):
    """Check, the "still true" test and the run: the studio itself touches
    (stat, list, open) no recording path more than once (network drives)."""
    import collections

    request, _ = request_for(tmp_path, with_facts=True)
    roots = tuple(os.path.abspath(s.source) for s in request.sources)
    touched: collections.Counter = collections.Counter()

    def note(path):
        try:
            p = os.path.abspath(os.fsdecode(path))
        except TypeError:
            return
        if p.startswith(roots):
            touched[p] += 1

    def wrap(module, name):
        real = getattr(module, name)

        def f(path, *a, **k):
            note(path)
            return real(path, *a, **k)

        monkeypatch.setattr(module, name, f)

    for name in ("stat", "lstat", "scandir", "listdir", "open"):
        wrap(os, name)
    wrap(__import__("builtins"), "open")
    assert j.check_join(request) == []
    assert j.facts_still_true(request)
    result = j.make_joined(request)
    assert result.ok, result.message
    twice = {os.path.basename(p): n for p, n in touched.items() if n > 1}
    assert not twice, twice
    # and the studio's part is small: a few stats (recording, channel folders
    # and the largest file of each folder), no listing, no open
    # (per recording: itself, 3 channel folders, 3 data files, 1 file at its root)
    assert sum(touched.values()) <= len(roots) * (1 + 3 + 3 + 1)


def test_the_run_does_not_open_the_recordings_again(tmp_path, monkeypatch):
    request, _ = request_for(tmp_path, with_facts=True)
    spy = Spy(monkeypatch)
    result = j.make_joined(request)
    assert result.ok, result.message
    assert (spy.opened, spy.times, spy.walked) == (0, 0, 0)
    assert mef3io.Reader.__name__ != "x"  # the spy is still in place


def test_a_check_unchanged_starts_the_run_and_a_touched_file_checks_again(
    window_page, qtbot, tmp_path, monkeypatch
):
    window, page = window_page
    from mef3io_studio.joiner import page as page_module

    a = write(tmp_path / "A.mefd", T0, seconds=600)
    b = write(tmp_path / "B.mefd", T0 + HOUR, seconds=600)
    open_in_explorer(qtbot, window, [a, b])
    calls = []
    real = page_module.check_join

    def spy(ctx, request):
        calls.append(bool(request.layouts))
        return real(ctx, request)

    monkeypatch.setattr(page_module, "check_join", spy)

    def to_the_end(name):
        choose_all(page)
        page.next()
        page.next()
        page.new.name.setText(name)

    # Unchanged since Explore read it: checked once, then it starts.
    to_the_end("joined")
    page.next()
    qtbot.waitUntil(lambda: page.handle is not None, timeout=120_000)
    assert calls == [True]
    with qtbot.waitSignal(page.handle.finished, timeout=120_000):
        pass
    assert page.result.ok
    # A recording is touched after Explore read it: it checks again, opening
    # everything afresh, and then starts.
    page.restart()
    calls.clear()
    os.utime(a, ns=(5, 6))
    to_the_end("joined2")
    page.next()
    qtbot.waitUntil(lambda: page.handle is not None, timeout=120_000)
    assert calls == [True, False]
    with qtbot.waitSignal(page.handle.finished, timeout=120_000):
        pass
    assert page.result.ok


def test_pick_only_a_few_channels_then_run(window_page, qtbot, tmp_path):
    window, page = window_page
    a = write(tmp_path / "A.mefd", T0, seconds=600)
    b = write(tmp_path / "B.mefd", T0 + HOUR, seconds=600)
    open_in_explorer(qtbot, window, [a, b])
    page.go(0)
    page.recordings.set_ticked([e.key for e in page.recordings.rows])
    page.next()
    from mef3io_studio.joiner.page import STEP_CHANNELS

    assert page.step == STEP_CHANNELS  # from Explore: always shown, all kept
    assert page.selection.excluded == ()
    page.picker.exclude_all.click()  # "keep only these": leave all out, tick a few
    page.picker.click("a2", True)
    assert page.selection.excluded == ("a1", "a3")
    assert "Exclude all" in page.picker.exclude_all.text()
    page.next()
    wait_for_map(page)
    page.next()
    page.next()
    page.new.name.setText("few")
    check = page.next()
    with qtbot.waitSignal(check.finished, timeout=120_000):
        pass
    qtbot.waitUntil(lambda: page.handle is not None, timeout=120_000)
    with qtbot.waitSignal(page.handle.finished, timeout=120_000):
        pass
    assert page.result.ok
    assert mef3io.Reader(page.request.destination).channels == ["a2"]
