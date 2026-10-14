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


@pytest.mark.xfail(
    reason="bnelair/mef3io#79: mef3io.Reader holds the GIL for the whole open, "
    "so an open on a worker THREAD freezes the UI. Remove this xfail when it ships.",
    strict=False,
)
def test_real_open_on_a_thread_does_not_freeze_the_ui(qtbot, jobs, multisegment_session):
    """Pins the requirement against the real library call on a thread."""

    class InThread(LocalRecordingSource):
        isolate = False

    with TimerProbe() as probe:
        handle = jobs.submit("Opening", summarize_recording, InThread(), str(multisegment_session))
        with qtbot.waitSignal(handle.succeeded, timeout=60_000):
            pass
    assert handle.result.channel_count == 64
    assert probe.max_gap < 0.2, f"UI stalled for {probe.max_gap:.2f} s"


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
