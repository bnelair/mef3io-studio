"""The unattended batch (mef3io-studio#5, "one unattended pipeline"): per
recording anonymize → archive (or add to the batch archive) → check → delete
the work copy; Stop, a failing recording, a crash and resume, free space,
keeping awake, and nothing identifying in the log, the journal or the
settings.

Tests that make copies need mef3io's ``anonymize_session``; on a mef3io
without it they are skipped, saying so."""

from __future__ import annotations

import json
import logging
import os
import tarfile
from pathlib import Path

import mef3io
import pytest
from explorer_sessions import DAY, FIRST, LAST, PATIENT_ID, T0, write_large, write_plain

from mef3io_studio.model import anonymize as a
from mef3io_studio.model import bundle, journal, pipeline
from mef3io_studio.model.awake import KeepAwake
from mef3io_studio.workers import tasks

HAS_ANONYMIZER = callable(getattr(mef3io, "anonymize_session", None))
needs_anonymizer = pytest.mark.skipif(
    not HAS_ANONYMIZER,
    reason="this mef3io has no anonymize_session (needs the release with bnelair/mef3io#104)",
)
SHIFT = 4000 * DAY


def make_batch(tmp_path, output, sources, names=None, exclude=("a3",), bundle_name="batch.tar"):
    """A batch straight from the model (relative dates: one shift for all)."""
    dest = tmp_path / "out"
    work = tmp_path / "work"
    dest.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    names = names or [f"S{i + 1:03d}" for i in range(len(sources))]
    requests, estimates, originals = [], {}, {}
    for i, (src, name) in enumerate(zip(sources, names, strict=True)):
        key = f"k{i}"
        requests.append(
            a.CopyRequest(
                key=key,
                source=str(src),
                destination=str(dest / f"{name}.mefd"),
                shift_us=SHIFT,
                exclude_channels=tuple(exclude),
            )
        )
        estimates[key] = 1_000_000
        originals[key] = (Path(src).name[:-5], T0, T0 + 10_000_000)
    batch = pipeline.plan_batch(
        output, requests, str(dest), str(work), estimates, originals, bundle_name=bundle_name
    )
    return batch, dest, work


def run(qtbot, jobs, batch, journal_=None, timeout=180_000):
    handle = jobs.submit("batch", tasks.run_batch, batch, journal_)
    published = []
    handle.published.connect(published.extend)
    with qtbot.waitSignal(handle.finished, timeout=timeout):
        pass
    return handle, published


def results_by_key(published):
    return {r.key: r for r in published if isinstance(r, a.CopyResult)}


def three(tmp_path):
    return [write_plain(tmp_path / f"rec{i}.mefd") for i in range(3)]


# --------------------------------------------------------------------------
# The three outputs


@needs_anonymizer
def test_a_batch_of_three_into_archives(qtbot, jobs, tmp_path):
    batch, dest, work = make_batch(tmp_path, pipeline.ARCHIVES, three(tmp_path))
    handle, published = run(qtbot, jobs, batch)
    assert handle.record.status.name == "SUCCEEDED"
    results = results_by_key(published)
    assert all(r.ok for r in results.values()) and len(results) == 3
    for i in range(3):
        tar = dest / f"S{i + 1:03d}.mefd.tar"
        assert tar.is_file()
        summary = mef3io.inspect(str(tar))  # opens directly, no extracting
        assert [c.name for c in summary.channels] == ["a1", "a2"]
        assert summary.start_time == T0 + SHIFT  # relative: the same shift for all
    assert sorted(p.name for p in dest.iterdir()) == [f"S00{i}.mefd.tar" for i in (1, 2, 3)]
    assert list(work.iterdir()) == []  # every work copy deleted after its check
    assert journal.unfinished() == []


@needs_anonymizer
def test_a_batch_of_three_into_one_bundle(qtbot, jobs, tmp_path):
    batch, dest, work = make_batch(tmp_path, pipeline.BUNDLE, three(tmp_path))
    handle, published = run(qtbot, jobs, batch)
    outcome = next(p for p in published if isinstance(p, tasks.BundleOutcome))
    assert outcome.ok and outcome.recordings == 3
    path = dest / "batch.tar"
    assert sorted(p.name for p in dest.iterdir()) == ["batch.tar"]
    with tarfile.open(path) as tar:
        names = tar.getnames()
        roots = sorted({n.split("/")[0] for n in names})
        assert roots == ["S001.mefd", "S002.mefd", "S003.mefd"]
        assert names == sorted(names)  # deterministic order
        assert {m.mtime for m in tar.getmembers()} == {0}
        assert {(m.uname, m.gname, m.uid, m.gid) for m in tar.getmembers()} == {("", "", 0, 0)}
        tar.extractall(tmp_path / "unpacked", filter="data")
    for root in roots:
        summary = mef3io.inspect(str(tmp_path / "unpacked" / root))
        assert [c.name for c in summary.channels] == ["a1", "a2"]
        assert summary.start_time == T0 + SHIFT
    assert list(work.iterdir()) == []


@needs_anonymizer
def test_one_failing_recording_does_not_stop_the_batch(qtbot, jobs, tmp_path):
    from test_anonymizer_ui import damage_signal

    sources = three(tmp_path)
    damage_signal(sources[1])
    batch, dest, work = make_batch(tmp_path, pipeline.ARCHIVES, sources)
    _handle, published = run(qtbot, jobs, batch)
    results = results_by_key(published)
    assert results["k0"].ok and results["k2"].ok and not results["k1"].ok
    assert results["k1"].message.title == "Part of the recording is damaged"
    assert sorted(p.name for p in dest.iterdir()) == ["S001.mefd.tar", "S003.mefd.tar"]
    assert list(work.iterdir()) == []


# --------------------------------------------------------------------------
# Stop


@needs_anonymizer
@pytest.mark.parametrize("output", [pipeline.FOLDERS, pipeline.ARCHIVES, pipeline.BUNDLE])
def test_stop_while_anonymizing_leaves_nothing_half_made(qtbot, jobs, tmp_path, output):
    big = write_large(tmp_path / "big.mefd", channels=2, parts=2, seconds=900)
    batch, dest, work = make_batch(tmp_path, output, [big], exclude=())
    handle = jobs.submit("batch", tasks.run_batch, batch, None)
    qtbot.waitUntil(
        lambda: (
            handle.record.progress is not None
            and "Anonymizing" in handle.record.progress.phase
            and (handle.record.progress.fraction or 0) > 0.01
        ),
        timeout=60_000,
    )
    handle.cancel()
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    assert list(dest.iterdir()) == []
    assert list(work.iterdir()) == []
    assert journal.unfinished() == []  # a deliberate stop needs no resume


def test_stop_while_adding_to_the_bundle_cuts_it_back(tmp_path):
    from mef3io_studio.model.cancel import CancelToken, JobCancelled

    first = write_plain(tmp_path / "A1.mefd", notes=False)
    second = write_large(tmp_path / "B2.mefd", channels=1, parts=1, seconds=600)
    writer = bundle.BundleWriter(str(tmp_path / "x.tar"))
    writer.add(str(first))
    offset = writer.offset
    token = CancelToken()
    seen = [0]

    def on_bytes(n: int) -> None:
        seen[0] += n
        if seen[0] > 2_000_000:
            token.cancel()

    with pytest.raises(JobCancelled):
        writer.add(str(second), token, on_bytes)
    assert os.path.getsize(writer.temporary) == offset  # the half-added one is gone
    path = writer.finish()  # what is complete can still be delivered
    with tarfile.open(path) as tar:
        assert {n.split("/")[0] for n in tar.getnames()} == {"A1.mefd"}
    assert not os.path.exists(writer.temporary)


@needs_anonymizer
def test_stop_in_a_bundle_batch_delivers_the_finished_recordings(qtbot, jobs, tmp_path):
    small = write_plain(tmp_path / "small.mefd", notes=False)
    big = write_large(tmp_path / "big.mefd", channels=2, parts=2, seconds=900)
    batch, dest, work = make_batch(tmp_path, pipeline.BUNDLE, [small, big], exclude=())
    handle = jobs.submit("batch", tasks.run_batch, batch, None)
    published = []
    handle.published.connect(published.extend)
    qtbot.waitUntil(
        lambda: (
            handle.record.progress is not None
            and handle.record.progress.phase.startswith("Recording 2 of 2")
        ),
        timeout=60_000,
    )
    handle.cancel()
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    outcome = next(p for p in published if isinstance(p, tasks.BundleOutcome))
    assert outcome.ok and outcome.recordings == 1
    assert sorted(p.name for p in dest.iterdir()) == ["batch.tar"]
    assert list(work.iterdir()) == []


# --------------------------------------------------------------------------
# Crash and resume


@needs_anonymizer
def test_resume_after_a_crash(qtbot, window, tmp_path, monkeypatch):
    """The app dies while the second recording is being made: the journal
    stays; the next start offers to continue; continuing checks again, skips
    the finished one and makes the rest."""
    window._closing = True
    jobs = window.jobs
    batch, dest, work = make_batch(tmp_path, pipeline.ARCHIVES, three(tmp_path)[:2])
    real_one = tasks._one

    def crash_on_second(ctx, batch_, item, n, *rest):
        if n == 1:
            os.makedirs(item.work, exist_ok=True)  # half-made work, as a crash leaves it
            (Path(item.work) / "S002.mefd").mkdir()
            raise RuntimeError("power cut")
        return real_one(ctx, batch_, item, n, *rest)

    monkeypatch.setattr(tasks, "_one", crash_on_second)
    handle, _ = run(qtbot, jobs, batch)
    assert handle.record.status.name == "FAILED"
    monkeypatch.setattr(tasks, "_one", real_one)
    (pending,) = journal.unfinished()
    assert (pending.done, pending.total) == (1, 2)

    asked = []
    monkeypatch.setattr(
        window, "ask", lambda title, text, detail, buttons: asked.append(text) or "continue"
    )
    window.offer_pending_work()
    assert asked[0] == "Continue the batch: 1 of 2 done."
    page = window.anonymizer
    qtbot.waitUntil(lambda: page.run.finished, timeout=180_000)
    texts = page.run_step.result_texts
    assert texts["k0"] == "Saved and checked (earlier)" and texts["k1"] == "Saved and checked"
    assert sorted(p.name for p in dest.iterdir()) == ["S001.mefd.tar", "S002.mefd.tar"]
    assert list(work.iterdir()) == []  # the crashed work copy was redone, then deleted
    assert journal.unfinished() == []


def test_leftover_work_copies_are_offered_for_removal(window, tmp_path, monkeypatch):
    window._closing = True
    from mef3io_studio.common.settings_dialog import set_work_folder

    work = tmp_path / "work"
    (work / "work-abc-0" / "S1.mefd").mkdir(parents=True)
    (work / "unrelated").mkdir()
    set_work_folder(window.settings, str(work))
    asked = []
    monkeypatch.setattr(
        window, "ask", lambda title, text, detail, buttons: asked.append(text) or "remove"
    )
    window.offer_pending_work()
    assert asked and "1 unfinished anonymized copy" in asked[0]
    assert sorted(p.name for p in work.iterdir()) == ["unrelated"]


def test_the_journal_holds_only_opaque_ids(tmp_path):
    payload = {
        "items": [{"request": {"source": f"/data/{LAST}_{FIRST}.mefd", "shift_us": SHIFT}}],
        "original": [PATIENT_ID, T0, T0 + 1],
    }
    j = journal.Journal.new(3, payload)
    raw = Path(j.path).read_text(encoding="utf-8")
    for secret in (LAST, FIRST, PATIENT_ID, "/data", str(T0), str(SHIFT)):
        assert secret not in raw
    record = json.loads(raw)
    assert set(record) == {"version", "batch", "items", "sealed"}
    j.mark(0, journal.DONE)
    (back,) = journal.unfinished()
    assert back.payload == payload and back.states[0] == journal.DONE
    # A changed journal is not trusted (and removed).
    record = json.loads(Path(j.path).read_text(encoding="utf-8"))
    record["sealed"] = record["sealed"][:-8] + "AAAAAAA="
    Path(j.path).write_text(json.dumps(record), encoding="utf-8")
    assert journal.unfinished() == []
    assert not Path(j.path).exists()


# --------------------------------------------------------------------------
# Up-front checks


def test_free_space_is_refused_before_starting(tmp_path):
    batch, _dest, _work = make_batch(
        tmp_path, pipeline.ARCHIVES, [tmp_path / "x.mefd", tmp_path / "y.mefd"]
    )
    problems = pipeline.space_problems(batch, free_work=10, free_destination=10)
    assert len(problems) == 2
    assert "destination" in problems[0] and "work folder" in problems[1]
    assert pipeline.space_problems(batch, 10**12, 10**12) == []
    folders, _, _ = make_batch(tmp_path, pipeline.FOLDERS, [tmp_path / "x.mefd"])
    # Folders need no work folder.
    assert len(pipeline.space_problems(folders, free_work=0, free_destination=10**12)) == 0


@needs_anonymizer
def test_free_space_refusal_stops_the_wizard_before_anything_runs(
    window, qtbot, tmp_path, monkeypatch
):
    from test_anonymizer_ui import _through_to_review, go_to_channels, open_in_explorer

    window._closing = True
    window.show()
    page = window.anonymizer
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    out = tmp_path / "out"
    out.mkdir()
    _through_to_review(page, qtbot, out, names=["F1"], output="archives")
    monkeypatch.setattr(pipeline, "free_bytes", lambda _folder: 1000)
    checks = page.start()
    qtbot.waitUntil(lambda: checks.is_finished, timeout=60_000)
    assert page.handle is None and page.step != 7
    assert any("free" in p for p in page.preflight_problems)
    assert "free" in page.review.blocked.text()
    assert list(out.iterdir()) == []


def test_the_work_folder_must_be_usable(tmp_path):
    assert pipeline.work_folder_problem(str(tmp_path / "w")) is None
    assert "inside a recording" in pipeline.work_folder_problem(str(tmp_path / "r.mefd" / "w"))
    assert pipeline.work_folder_problem("") == "Choose a work folder."


# --------------------------------------------------------------------------
# Keeping awake, notifying


def test_keep_awake_holds_off_sleep_and_lets_go(monkeypatch):
    import sys

    from mef3io_studio.model import awake

    started, stopped = [], []

    class FakeProcess:
        def __init__(self, args, **_kw):
            started.append(args)

        def terminate(self):
            stopped.append(1)

        def wait(self, _timeout):
            return 0

    monkeypatch.delenv(awake.DISABLE_ENV, raising=False)
    monkeypatch.setattr(awake.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(awake.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sys, "platform", "linux")
    with KeepAwake() as keep:
        assert keep.active == "systemd-inhibit"
        assert "--what=sleep:idle" in started[0]
    assert stopped == [1] and keep.active == ""
    # Nothing available: a quiet no-op.
    monkeypatch.setattr(awake.shutil, "which", lambda name: None)
    with KeepAwake() as keep:
        assert keep.active == ""
    # Off when the user said so.
    with KeepAwake(enabled=False) as keep:
        assert keep.active == ""


@needs_anonymizer
def test_the_batch_notifies_when_done_and_logs_nothing_identifying(
    qtbot, window, tmp_path, monkeypatch
):
    from mef3io_studio.common import privacy
    from mef3io_studio.common.logging_setup import setup_logging, teardown_logging

    window._closing = True
    notified = []
    monkeypatch.setattr(window, "notify", notified.append)
    window.anonymizer.batch_finished.disconnect()
    window.anonymizer.batch_finished.connect(window.notify)
    log_file = setup_logging(tmp_path / "logs", level=logging.DEBUG)
    privacy.identities.clear()
    try:
        batch, dest, _work = make_batch(tmp_path, pipeline.BUNDLE, three(tmp_path)[:1])
        window.anonymizer.run_batch(batch)
        qtbot.waitUntil(lambda: window.anonymizer.run.finished, timeout=180_000)
    finally:
        teardown_logging()
        privacy.identities.clear()
    assert notified and "1 of 1" in notified[0]
    text = log_file.read_text(encoding="utf-8")
    for secret in (FIRST, LAST, PATIENT_ID, str(tmp_path), "S001", "2000-01-01", str(T0)):
        assert secret not in text, secret
    window.save_preferences()
    stored = Path(window.settings._qs.fileName()).read_text(encoding="utf-8", errors="replace")
    assert str(tmp_path) not in stored and "S001" not in stored
