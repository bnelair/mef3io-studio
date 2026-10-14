"""The background job layer: results, throttled progress, cancellation,
plain-language errors, and — most importantly — the UI never freezes."""

import time

import pytest
from PySide6.QtCore import QTimer

from mef3io_studio.model.jobs import JobStatus
from mef3io_studio.model.source import LocalRecordingSource
from mef3io_studio.workers.tasks import summarize_recording


def wait_done(qtbot, handle, timeout=10_000):
    qtbot.waitUntil(lambda: handle.is_finished, timeout=timeout)


def test_job_result_arrives_on_ui_thread(qtbot, jobs, synthetic_session):
    handle = jobs.submit(
        "Opening a recording", summarize_recording, LocalRecordingSource(), str(synthetic_session)
    )
    with qtbot.waitSignal(handle.succeeded, timeout=10_000) as blocker:
        pass
    assert blocker.args[0].channel_count == 3
    assert handle.record.status is JobStatus.SUCCEEDED
    wait_done(qtbot, handle)


def test_progress_is_throttled_and_carries_done_total_phase(qtbot, jobs):
    received = []

    def chatty(ctx):
        for i in range(200_000):  # far more reports than the UI could draw
            ctx.report(i + 1, 200_000, "Counting")
        return "ok"

    handle = jobs.submit("Counting", chatty)
    handle.progress.connect(received.append)
    with qtbot.waitSignal(handle.finished, timeout=20_000):
        pass
    assert received, "expected some progress"
    assert len(received) < 200  # throttled (≤10/s plus forced updates)
    last = received[-1]
    assert (last.done, last.total, last.phase) == (200_000, 200_000, "Counting")
    assert handle.record.progress.fraction == 1.0


def test_failure_becomes_plain_language_message(qtbot, jobs, tmp_path):
    handle = jobs.submit(
        "Opening a recording",
        summarize_recording,
        LocalRecordingSource(),
        str(tmp_path / "missing.mefd"),
    )
    with qtbot.waitSignal(handle.failed, timeout=10_000) as blocker:
        pass
    message = blocker.args[0]
    assert message.title == "The recording could not be opened"
    assert handle.record.status is JobStatus.FAILED
    assert handle.record.result_text == message.title


def test_cancel_stops_a_running_job(qtbot, jobs):
    def slow(ctx):
        ctx.report(0, None, "Waiting")
        for _ in range(500):
            ctx.check_cancelled()
            time.sleep(0.01)
        return "not cancelled"

    handle = jobs.submit("Slow task", slow)
    qtbot.waitUntil(lambda: handle.record.status is JobStatus.RUNNING, timeout=5000)
    with qtbot.waitSignal(handle.cancelled, timeout=5000):
        handle.cancel()
    assert handle.record.status is JobStatus.CANCELLED
    assert handle.record.result_text == "Cancelled"


def test_ui_event_loop_keeps_running_during_a_slow_job(qtbot, jobs):
    """The requirement "never freeze": while a deliberately slow job runs,
    a 20 ms UI timer must keep firing."""
    ticks = []
    timer = QTimer()
    timer.setInterval(20)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))

    def slow(ctx):
        # CPU-and-sleep mix for ~1.5 s, reporting progress all the way.
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            ctx.check_cancelled()
            sum(range(2000))
            time.sleep(0.001)
            ctx.report(time.monotonic() - (end - 1.5), 1.5, "Working")
        return "done"

    timer.start()
    handle = jobs.submit("Slow task", slow)
    with qtbot.waitSignal(handle.succeeded, timeout=15_000):
        pass
    timer.stop()
    assert len(ticks) >= 25, f"UI timer fired only {len(ticks)} times in ~1.5 s"
    gaps = [b - a for a, b in zip(ticks, ticks[1:], strict=False)]
    assert max(gaps) < 0.5, f"UI stalled for {max(gaps):.2f} s"


def test_many_jobs_and_shutdown(qtbot, jobs):
    def short(ctx, n):
        time.sleep(0.01)
        return n

    handles = [jobs.submit(f"Task {i}", short, i) for i in range(30)]
    qtbot.waitUntil(lambda: all(h.is_finished for h in handles), timeout=20_000)
    assert [h.result for h in handles] == list(range(30))
    assert jobs.running_count() == 0
    assert len(jobs.clear_finished()) == 30


@pytest.mark.parametrize("phase", ["Reading", ""])
def test_activity_panel_lists_jobs(qtbot, jobs, phase):
    from mef3io_studio.common.activity import ActivityIndicator, ActivityPanel

    panel = ActivityPanel(jobs)
    indicator = ActivityIndicator(jobs)
    qtbot.addWidget(panel)
    qtbot.addWidget(indicator)
    panel.show()

    def slow(ctx):
        for i in range(30):
            ctx.check_cancelled()
            ctx.report(i, 30 if phase else None, phase)
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
