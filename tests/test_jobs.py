"""The background job layer: results, throttled progress, cancellation,
cleanup, plain-language errors, and — most importantly — the UI never freezes."""

import time

import pytest
from PySide6.QtCore import QTimer

from mef3io_studio.model.jobs import JobStatus
from mef3io_studio.model.source import LocalRecordingSource
from mef3io_studio.workers.tasks import summarize_recording


def wait_done(qtbot, handle, timeout=10_000):
    qtbot.waitUntil(lambda: handle.is_finished, timeout=timeout)


class TimerProbe:
    """A 20 ms UI timer; records when it actually fired."""

    def __init__(self):
        self.ticks = []
        self.timer = QTimer()
        self.timer.setInterval(20)
        self.timer.timeout.connect(lambda: self.ticks.append(time.monotonic()))

    def __enter__(self):
        self.timer.start()
        return self

    def __exit__(self, *exc):
        self.timer.stop()

    @property
    def max_gap(self) -> float:
        gaps = [b - a for a, b in zip(self.ticks, self.ticks[1:], strict=False)]
        return max(gaps) if gaps else float("inf")


def test_job_result_arrives_on_ui_thread(qtbot, jobs, synthetic_session):
    handle = jobs.submit(
        "Opening a recording", summarize_recording, LocalRecordingSource(), str(synthetic_session)
    )
    with qtbot.waitSignal(handle.succeeded, timeout=30_000) as blocker:
        pass
    assert blocker.args[0].channel_count == 3
    assert handle.record.status is JobStatus.SUCCEEDED
    wait_done(qtbot, handle)


def test_progress_is_throttled_even_with_alternating_phases(qtbot, jobs):
    received = []

    def chatty(ctx):
        n = 100_000
        for i in range(n):  # alternating phases: the old throttle let all through
            ctx.report(i + 1, n, "Reading" if i % 2 else "Checking")
        time.sleep(0.35)
        return "ok"

    handle = jobs.submit("Counting", chatty)
    handle.progress.connect(received.append)
    start = time.monotonic()
    with qtbot.waitSignal(handle.finished, timeout=20_000):
        pass
    elapsed = time.monotonic() - start
    assert received, "expected some progress"
    # at most 10 a second, plus the final flush
    assert len(received) <= elapsed * 10 + 3, (len(received), elapsed)
    last = received[-1]
    assert (last.done, last.total) == (100_000, 100_000)
    assert handle.record.progress.fraction == 1.0
    assert [p for p in received if p.done == p.total].count(last) == 1  # "finished" once


def test_failure_becomes_plain_language_message(qtbot, jobs, tmp_path):
    handle = jobs.submit(
        "Opening a recording",
        summarize_recording,
        LocalRecordingSource(),
        str(tmp_path / "missing.mefd"),
    )
    with qtbot.waitSignal(handle.failed, timeout=30_000) as blocker:
        pass
    message = blocker.args[0]
    assert message.title == "The recording could not be opened"
    assert handle.record.status is JobStatus.FAILED
    assert handle.record.result_text == message.title


@pytest.mark.parametrize("exc", [SystemExit(3), KeyboardInterrupt(), GeneratorExit()])
def test_base_exception_still_ends_the_job(qtbot, jobs, exc):
    def explode(ctx):
        raise exc

    handle = jobs.submit("Exploding", explode)
    outcomes = []
    for name in ("succeeded", "failed", "cancelled"):
        getattr(handle, name).connect(lambda *a, n=name: outcomes.append(n))
    with qtbot.waitSignal(handle.finished, timeout=5000):
        pass
    qtbot.wait(50)
    assert outcomes == ["failed"]
    assert handle.record.status is JobStatus.FAILED


def test_cancel_stops_a_running_job_and_runs_cleanup(qtbot, jobs, tmp_path):
    partial = tmp_path / "partial-copy"

    def slow(ctx):
        partial.write_text("half written")
        ctx.add_cleanup(partial.unlink)
        ctx.report(0, None, "Waiting")
        for _ in range(500):
            ctx.check_cancelled()
            time.sleep(0.01)
        return "not cancelled"

    handle = jobs.submit("Slow task", slow)
    qtbot.waitUntil(lambda: handle.record.status is JobStatus.RUNNING, timeout=5000)
    qtbot.waitUntil(partial.exists, timeout=5000)
    with qtbot.waitSignal(handle.cancelled, timeout=5000):
        handle.cancel()
    assert handle.record.status is JobStatus.CANCELLED
    assert handle.record.result_text == "Stopped"
    assert not partial.exists(), "cleanup should remove partial output"


def test_cleanup_runs_on_failure_not_on_success(qtbot, jobs):
    calls = []

    def ok(ctx):
        ctx.add_cleanup(lambda: calls.append("ok-cleanup"))
        return 1

    def bad(ctx):
        ctx.add_cleanup(lambda: calls.append("bad-cleanup"))
        raise ValueError("x")

    for fn in (ok, bad):
        handle = jobs.submit("t", fn)
        with qtbot.waitSignal(handle.finished, timeout=5000):
            pass
    assert calls == ["bad-cleanup"]


def test_ui_event_loop_keeps_running_during_a_slow_job(qtbot, jobs):
    """The requirement "never freeze": while a deliberately slow job runs,
    a 20 ms UI timer must keep firing. (This job releases the GIL often;
    see the next tests for the real mef3io call.)"""

    def slow(ctx):
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            ctx.check_cancelled()
            sum(range(2000))
            time.sleep(0.001)
            ctx.report(time.monotonic() - (end - 1.5), 1.5, "Working")
        return "done"

    with TimerProbe() as probe:
        handle = jobs.submit("Slow task", slow)
        with qtbot.waitSignal(handle.succeeded, timeout=15_000):
            pass
    assert len(probe.ticks) >= 25, f"UI timer fired only {len(probe.ticks)} times in ~1.5 s"
    assert probe.max_gap < 0.5, f"UI stalled for {probe.max_gap:.2f} s"


def test_real_open_on_a_thread_does_not_freeze_the_ui(qtbot, jobs, multisegment_session):
    """Pins the requirement against the real library call on a thread.

    The summary uses ``mef3io.inspect`` (mef3io 1.3), which lets other
    threads run while it works. ``mef3io.Reader`` still holds the GIL for a
    whole open (bnelair/mef3io#79), which is why opens stay in a helper
    process (``LocalRecordingSource.isolate``).

    Only meaningful when the call itself takes clearly longer than the
    allowed stall (then an inspect that held the GIL throughout would fail
    the bound). On a machine fast enough to finish sooner it is skipped,
    saying so, rather than passing on nothing."""

    class InThread(LocalRecordingSource):
        isolate = False

    bound = 0.2
    with TimerProbe() as probe:
        start = time.monotonic()
        handle = jobs.submit("Opening", summarize_recording, InThread(), str(multisegment_session))
        with qtbot.waitSignal(handle.succeeded, timeout=60_000):
            pass
        took = time.monotonic() - start
    assert handle.result.channel_count == 64
    if took <= 1.5 * bound:
        pytest.skip(f"call took {took:.2f}s, too short to show a held GIL")
    assert probe.max_gap < bound, f"UI stalled for {probe.max_gap:.2f} s"


def test_real_open_in_the_app_does_not_freeze_the_ui(qtbot, jobs, multisegment_session):
    """What the app actually does today: the open runs in a helper process."""
    with TimerProbe() as probe:
        handle = jobs.submit(
            "Opening", summarize_recording, LocalRecordingSource(), str(multisegment_session)
        )
        with qtbot.waitSignal(handle.succeeded, timeout=60_000):
            pass
    assert handle.result.channel_count == 64
    assert probe.max_gap < 0.2, f"UI stalled for {probe.max_gap:.2f} s"


def test_cancel_stops_the_helper_process(qtbot, jobs):
    def sleepy(ctx):
        return ctx.run_isolated(time.sleep, 30)

    handle = jobs.submit("Sleeping helper", sleepy)
    qtbot.waitUntil(lambda: handle.record.status is JobStatus.RUNNING, timeout=5000)
    qtbot.wait(300)
    start = time.monotonic()
    with qtbot.waitSignal(handle.cancelled, timeout=10_000):
        handle.cancel()
    assert time.monotonic() - start < 5


def test_helper_process_errors_keep_their_type(qtbot, jobs, tmp_path):
    import mef3io

    from mef3io_studio.workers.isolated import run_isolated

    with pytest.raises(mef3io.IoError):
        run_isolated(LocalRecordingSource().summarize, str(tmp_path / "missing.mefd"))


def test_many_jobs_and_dispose(qtbot, jobs):
    def short(ctx, n):
        time.sleep(0.01)
        return n

    handles = [jobs.submit(f"Task {i}", short, i) for i in range(30)]
    qtbot.waitUntil(lambda: all(h.is_finished for h in handles), timeout=20_000)
    assert [h.result for h in handles] == list(range(30))
    assert jobs.running_count() == 0
    assert len(jobs.clear_finished()) == 30
    assert jobs.handles == []


@pytest.mark.parametrize("phase", ["Reading", ""])
def test_activity_panel_lists_jobs(qtbot, jobs, phase):
    from mef3io_studio.common.activity import ActivityIndicator, ActivityPanel

    panel = ActivityPanel(jobs)
    indicator = ActivityIndicator(jobs)
    qtbot.addWidget(panel)
    qtbot.addWidget(indicator)
    panel.show()

    def slow(ctx):
        for i in range(300):
            ctx.check_cancelled()
            ctx.report(i, 300 if phase else None, phase)
            time.sleep(0.02)
        return 1

    handle = jobs.submit("Listing", slow)
    qtbot.waitUntil(lambda: handle.record.status is JobStatus.RUNNING, timeout=5000)
    assert panel.model.rowCount() == 1
    assert "running" in indicator.button.text()
    panel.table.selectRow(0)
    assert panel.cancel_button.isEnabled()
    panel.cancel_selected()
    wait_done(qtbot, handle)
    assert handle.record.status is JobStatus.CANCELLED
    assert indicator.button.text() == "No tasks running"
    panel.clear_finished()
    assert panel.model.rowCount() == 0


class _BigResult:
    """A job result large enough that a leak would show (weakref-able)."""

    def __init__(self):
        self.data = bytearray(2_000_000)


def test_finished_handles_and_results_are_freed(qtbot, jobs):
    """Regression: a closure over the handle connected to its own signal is a
    cycle through C++ the GC cannot see; clear_finished() must break it."""
    import gc
    import weakref

    def work(ctx):
        return _BigResult()

    handle_refs, result_refs = [], []
    for _round in range(3):
        handles = [jobs.submit("t", work) for _ in range(25)]
        for h in handles:
            # the pattern app code naturally writes: a closure over the handle
            h.succeeded.connect(lambda r, h=h: setattr(h.record, "result_text", "ok"))
            handle_refs.append(weakref.ref(h))
        qtbot.waitUntil(lambda hs=handles: all(h.is_finished for h in hs), timeout=20_000)
        result_refs += [weakref.ref(h.result) for h in handles]
        del handles, h
        jobs.clear_finished()
        qtbot.wait(20)
        gc.collect()
        alive = [r for r in handle_refs if r() is not None]
        assert not alive, f"{len(alive)} handles still alive after round {_round}"
        assert all(r() is None for r in result_refs), "job results still alive"


def test_self_check_handle_is_freed(window, qtbot):
    import gc
    import weakref

    handle = window.run_self_check()
    ref = weakref.ref(handle)
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    del handle
    window.jobs.clear_finished()
    qtbot.wait(20)
    gc.collect()
    assert ref() is None


@pytest.mark.parametrize("target", ["native_crash", "hard_exit"])
def test_helper_crash_gets_a_specific_plain_message(target):
    import helper_targets

    from mef3io_studio.model.errors import HelperCrashed, describe_error
    from mef3io_studio.workers.isolated import run_isolated

    with pytest.raises(HelperCrashed) as info:
        run_isolated(getattr(helper_targets, target))
    message = describe_error(info.value)
    assert message.title == "The app stopped while reading this recording"
    assert "may be damaged" in message.text and "Nothing was changed" in message.text


def test_successful_helper_is_not_terminated(monkeypatch):
    import multiprocessing.context

    import helper_targets

    from mef3io_studio.workers.isolated import run_isolated

    calls = []
    real = multiprocessing.context.SpawnProcess.terminate

    def spy(self):
        calls.append(self)
        return real(self)

    monkeypatch.setattr(multiprocessing.context.SpawnProcess, "terminate", spy)
    for _ in range(3):
        assert run_isolated(helper_targets.answer) == 42
    assert calls == [], "a helper that answered was terminated instead of exiting"


def test_stop_all_helpers_leaves_no_orphans():
    import multiprocessing
    import threading

    import helper_targets

    from mef3io_studio.model.errors import HelperCrashed
    from mef3io_studio.workers.isolated import run_isolated, stop_all_helpers

    outcome = []

    def run():
        try:
            run_isolated(helper_targets.sleep_then, 30)
        except HelperCrashed as exc:
            outcome.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 30
    while not multiprocessing.active_children() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert multiprocessing.active_children()
    start = time.monotonic()
    stop_all_helpers()
    thread.join(10)
    assert time.monotonic() - start < 5
    assert not multiprocessing.active_children()
    assert outcome and isinstance(outcome[0], HelperCrashed)


def test_helper_messages_arrive_in_order_before_the_result():
    import helper_targets

    from mef3io_studio.workers.isolated import run_isolated

    got = []
    assert run_isolated(helper_targets.count_to, 5, on_message=got.append) == "counted"
    assert got == [0, 1, 2, 3, 4]


def test_published_items_reach_the_ui_before_the_outcome(qtbot, jobs):
    order = []

    def work(ctx):
        for i in range(3):
            ctx.publish(i)
        return "done"

    handle = jobs.submit("Publishing", work)
    handle.published.connect(lambda items: order.append(("items", items)))
    handle.succeeded.connect(lambda r: order.append(("result", r)))
    with qtbot.waitSignal(handle.finished, timeout=5000):
        pass
    assert order[-1] == ("result", "done")
    assert [i for kind, items in order if kind == "items" for i in items] == [0, 1, 2]


@pytest.mark.skipif(
    not hasattr(__import__("os"), "kill") or __import__("sys").platform == "win32",
    reason="POSIX signals",
)
def test_helper_exits_when_the_app_dies(tmp_path):
    """bnelair/mef3io-studio#20 item 4: a killed app leaves no helper behind."""
    import os
    import signal
    import subprocess
    import sys

    pid_file = tmp_path / "pid"
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import helper_targets;"
            " helper_targets.orphan_parent(sys.argv[2])",
            os.path.dirname(__file__),
            str(pid_file),
        ]
    )
    try:
        deadline = time.monotonic() + 30
        while not (pid_file.exists() and pid_file.read_text()) and time.monotonic() < deadline:
            time.sleep(0.05)
        child = int(pid_file.read_text())
        parent.send_signal(signal.SIGKILL)
        parent.wait(10)
        deadline = time.monotonic() + 10
        alive = True
        while alive and time.monotonic() < deadline:
            try:
                os.kill(child, 0)
                time.sleep(0.05)
            except ProcessLookupError:
                alive = False
        assert not alive, "the helper outlived the app"
    finally:
        if parent.poll() is None:
            parent.kill()


class _FakeCtx:
    """Just enough of a JobContext for the open task, with a scripted
    helper: each run answers some requests, then crashes."""

    job_id = 0
    cancel_token = None

    def __init__(self, script):
        self.script = list(script)  # answers per run before it crashes; None = no crash
        self.runs = 0
        self.published = []

    def report(self, *_args):
        pass

    def publish(self, item):
        self.published.append(item)

    def run_isolated(self, fn, requests, on_message=None, **_kwargs):
        from mef3io_studio.model.errors import HelperCrashed
        from mef3io_studio.model.recording import InspectOutcome

        self.runs += 1
        answers = self.script.pop(0) if self.script else None
        for i, (location, _pw) in enumerate(requests):
            if answers is not None and i >= answers:
                raise HelperCrashed(-6)
            on_message(InspectOutcome(location))
        if answers is not None and answers >= len(requests):
            raise HelperCrashed(-6)  # answered everything, then died while exiting
        return []


class _Source:
    isolate = True

    def inspect_many(self, requests, send=None, cancel=None):  # pragma: no cover - never called
        raise AssertionError


def _run_open(script, n=5):
    from mef3io_studio.workers.tasks import open_recordings

    ctx = _FakeCtx(script)
    requests = [(f"/r/{i}.mefd", "") for i in range(n)]
    open_recordings(ctx, _Source(), requests)
    return ctx


def test_open_survives_a_helper_that_dies_after_its_last_answer():
    ctx = _run_open([5])
    assert [o.location for o in ctx.published] == [f"/r/{i}.mefd" for i in range(5)]
    assert all(o.error is None for o in ctx.published) and ctx.runs == 1


def test_open_stops_restarting_helpers_that_die_before_reading_anything():
    ctx = _run_open([0, 0, 0, 0, 0])
    assert ctx.runs == 2
    assert len(ctx.published) == 5
    assert all(o.error is not None for o in ctx.published)


def test_open_continues_after_one_helper_crash():
    ctx = _run_open([1])  # second recording crashes the first helper
    errors = [o.location for o in ctx.published if o.error is not None]
    assert errors == ["/r/1.mefd"] and len(ctx.published) == 5 and ctx.runs == 2
