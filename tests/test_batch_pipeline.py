"""The unattended batch (mef3io-studio#5, "one unattended pipeline"): per
recording anonymize → archive (or add to the batch archive) → check → delete
the work copy; Stop, a failing recording, a crash and resume, free space,
keeping awake, and nothing identifying in the log, the journal or the
settings.

mef3io 1.4.0 (the pinned version) has everything these tests use."""

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

SHIFT = 4000 * DAY


@pytest.fixture(autouse=True)
def _no_journals_from_other_tests():
    """Every test starts with no unfinished batch and no known temporaries
    (they share the test session's app-data folder)."""
    import shutil

    shutil.rmtree(journal.journal_dir(), ignore_errors=True)
    journal._save_temporaries([])
    journal._save_kept([])
    yield
    shutil.rmtree(journal.journal_dir(), ignore_errors=True)
    journal._save_temporaries([])
    journal._save_kept([])


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


def test_stop_in_a_bundle_batch_keeps_it_unfinished_then_finishes_on_request(qtbot, jobs, tmp_path):
    """Review round 2 (8): Stop never publishes an incomplete batch archive
    under its final name. The unfinished archive and the journal stay, so
    the batch can be continued; "Finish the archive with what is done" names
    it and reports it as partial."""
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
    assert not any(isinstance(p, tasks.BundleOutcome) for p in published)
    assert [p.name for p in dest.iterdir() if not p.name.startswith(".")] == []
    state = next(p for p in published if isinstance(p, tasks.BatchState))
    assert state.bundle_pending and state.journal is not None
    (pending,) = journal.unfinished()
    assert pending.done == 1
    handle = jobs.submit("finish", tasks.finish_bundle_now, pending)
    got = []
    handle.succeeded.connect(got.append)
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    (outcome,) = got
    assert outcome.ok and outcome.partial and (outcome.recordings, outcome.total) == (1, 2)
    assert sorted(p.name for p in dest.iterdir()) == ["batch.tar"]
    with tarfile.open(dest / "batch.tar") as tar:
        assert {n.split("/")[0] for n in tar.getnames()} == {"S001.mefd"}
    assert list(work.iterdir()) == []
    assert journal.unfinished() == []


# --------------------------------------------------------------------------
# Crash and resume


def test_resume_after_a_crash(qtbot, window, tmp_path, monkeypatch):
    """A real process death: the batch runs in its own process, which is
    killed while the second recording is being made. The journal stays; the
    next start offers to continue; continuing checks again, removes the
    half-made work, skips the finished one and makes the rest; the date key
    is complete (dates read again from the originals)."""
    import pickle
    import subprocess
    import sys
    import time

    window._closing = True
    small = write_plain(tmp_path / "rec0.mefd")
    big = write_large(tmp_path / "rec1.mefd", channels=2, parts=2, seconds=900)
    batch, dest, work = make_batch(tmp_path, pipeline.ARCHIVES, [small, big], exclude=())
    blob = tmp_path / "batch.pickle"
    blob.write_bytes(pickle.dumps(batch))
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join([src, str(Path(__file__).parent), env["PYTHONPATH"]])
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "batch_child.py"), str(blob)], env=env
    )
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            pending = journal.unfinished()
            if pending and pending[0].done == 1 and Path(batch.items[1].work).exists():
                time.sleep(0.5)  # well inside the second recording
                break
            assert child.poll() is None, "the batch ended before it could be interrupted"
            time.sleep(0.05)
        child.kill()  # SIGKILL / TerminateProcess: no cleanup runs
        child.wait(30)
    finally:
        if child.poll() is None:
            child.kill()
    (pending,) = journal.unfinished()
    assert (pending.done, pending.total) == (1, 2)
    assert any(work.iterdir())  # the half-made work copy is still there
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
    assert list(work.iterdir()) == []
    assert journal.unfinished() == []
    rows = page.date_key_rows()
    assert [(r.original_name, r.original_start) for r in rows] == [("rec0", T0), ("rec1", T0)]


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


# --------------------------------------------------------------------------
# Review round 2


def _flip_member_byte(path: str, start: int = 0, end: int | None = None) -> None:
    """Damage one byte inside the first non-empty file member's data."""
    with open(path, "rb") as raw:
        raw.seek(start)
        view = bundle._Limited(raw, end if end is not None else os.path.getsize(path))
        with tarfile.open(fileobj=view, mode="r:") as tar:
            member = next(m for m in tar.getmembers() if m.isfile() and m.size > 0)
    offset = member.offset_data
    with open(path, "r+b") as handle:
        handle.seek(offset)
        byte = handle.read(1)
        handle.seek(offset)
        handle.write(bytes([byte[0] ^ 0xFF]))


@pytest.mark.parametrize("output", [pipeline.ARCHIVES, pipeline.BUNDLE])
def test_a_damaged_archive_keeps_the_work_copy_and_fails_the_item(
    qtbot, jobs, tmp_path, monkeypatch, output
):
    """Review round 2 (1): every byte is compared before the work copy is
    deleted; a mismatch keeps the copy and reports the recording failed."""
    if output == pipeline.ARCHIVES:
        monkeypatch.setattr(tasks, "before_archive_check", lambda path: _flip_member_byte(path))
    else:
        monkeypatch.setattr(
            bundle, "before_check", lambda path, start, end: _flip_member_byte(path)
        )
    batch, dest, work = make_batch(tmp_path, output, three(tmp_path)[:1])
    _handle, published = run(qtbot, jobs, batch)
    (result,) = results_by_key(published).values()
    assert not result.ok and result.message.title == "The archive did not match the checked copy"
    kept = list(work.iterdir())
    assert len(kept) == 1 and (kept[0] / "S001.mefd").is_dir()  # the checked copy is kept
    assert [p for p in dest.iterdir() if not p.name.startswith(".")] == []


def test_the_byte_check_finds_any_changed_byte(tmp_path):
    folder = write_plain(tmp_path / "A1.mefd", notes=False)
    hashes = bundle.hash_folder(str(folder))
    writer = bundle.BundleWriter(str(tmp_path / "x.tar"))
    writer.add(str(folder))
    path = writer.finish()
    assert bundle.without_root(bundle.tar_member_hashes(path)) == hashes
    _flip_member_byte(path)
    assert bundle.without_root(bundle.tar_member_hashes(path)) != hashes


def test_resume_asks_each_originals_password_and_new_ones_twice(qtbot):
    from mef3io_studio.anonymizer.resume import ResumePasswordsDialog

    dialog = ResumePasswordsDialog([0, 2], 3, True)
    qtbot.addWidget(dialog)
    assert set(dialog.sources) == {0, 2}
    assert "every original" in dialog.problem_text()
    dialog.sources[0].setText("pw-a")
    dialog.sources[2].setText("pw-c")
    dialog.basic.setText("b1")
    dialog.basic_again.setText("b2")
    dialog.full.setText("f1")
    dialog.full_again.setText("f1")
    assert "basic passwords are not the same" in dialog.problem_text()
    dialog.basic_again.setText("b1")
    assert dialog.problem_text() == ""
    assert dialog.values() == ({0: "pw-a", 2: "pw-c"}, ("b1", "f1"))


def test_up_front_checks_open_each_original_with_its_own_password(qtbot, jobs, tmp_path):
    from explorer_sessions import PASSWORD_FULL, write_encrypted

    plain = write_plain(tmp_path / "plain.mefd", notes=False, channels=("a1", "a2", "a3"))
    locked = write_encrypted(tmp_path / "locked.mefd")
    batch, _dest, _work = make_batch(tmp_path, pipeline.FOLDERS, [plain, locked], exclude=())
    items = list(batch.items)
    items[1] = replace_request(items[1], password="wrong")
    batch = type(batch)(**{**batch.__dict__, "items": tuple(items)})
    verdict = preflight(qtbot, jobs, batch)
    assert verdict["problems"] == [
        "Recording 2 of 2: its password did not open it. Type its full-access password again."
    ]
    items[1] = replace_request(items[1], password=PASSWORD_FULL)
    batch = type(batch)(**{**batch.__dict__, "items": tuple(items)})
    assert preflight(qtbot, jobs, batch)["problems"] == []


def replace_request(item, **kw):
    from dataclasses import replace

    return replace(item, request=replace(item.request, **kw))


def preflight(qtbot, jobs, batch, *args):
    handle = jobs.submit("checks", tasks.preflight_batch, batch, *args)
    got = []
    handle.succeeded.connect(got.append)
    with qtbot.waitSignal(handle.finished, timeout=120_000):
        pass
    return got[0]


def test_resume_tests_the_copies_password_on_a_finished_copy(qtbot, jobs, tmp_path):
    from dataclasses import replace

    sources = three(tmp_path)[:2]
    batch, _dest, _work = make_batch(tmp_path, pipeline.ARCHIVES, sources)
    batch = replace(
        batch,
        items=tuple(
            replace(i, request=replace(i.request, output_password=("bas-1", "full-2")))
            for i in batch.items
        ),
    )
    j = journal.Journal.new(2, pipeline.to_payload(batch))
    # The first recording finished before the interruption.
    first = replace(batch, items=batch.items[:1])
    run(qtbot, jobs, first)
    j.states[0] = journal.DONE
    j.save()
    wrong = preflight(qtbot, jobs, batch, list(j.states), "not-the-password", j)
    assert any("does not open the copies already made" in p for p in wrong["problems"])
    right = preflight(qtbot, jobs, batch, list(j.states), "full-2", j)
    assert right["problems"] == []
    j.delete()


def test_temporary_archives_are_journaled_and_swept(qtbot, window, tmp_path, monkeypatch):
    """Review round 2 (4, 14): a temporary archive at the destination is in
    the journal and the app's sealed list before it is made; Discard removes
    it; without a journal it is still found among the leftovers."""
    from mef3io_studio.model.archive import TEMP_PREFIX

    window._closing = True
    dest = tmp_path / "out"
    dest.mkdir()
    temporary = dest / f"{TEMP_PREFIX}feedbeef.mefd.tar"
    temporary.write_bytes(b"half an archive")
    journal.note_temporary(str(temporary))
    journal.Journal.new(
        1,
        {
            "items": [
                {
                    "request": {"key": "k0", "source": "x", "destination": "y", "shift_us": 1},
                    "final": str(dest / "S1.mefd.tar"),
                    "work": "",
                    "estimate": 0,
                    "temporaries": [str(temporary)],
                }
            ]
        },
    )
    asked = []
    monkeypatch.setattr(
        window, "ask", lambda title, text, detail, buttons: asked.append(text) or "discard"
    )
    window.offer_pending_work()
    assert not temporary.exists() and journal.unfinished() == []
    assert str(temporary) not in journal.known_temporaries()
    # With no journal at all, a known temporary is listed among the leftovers.
    temporary.write_bytes(b"again")
    journal.note_temporary(str(temporary))
    asked.clear()
    monkeypatch.setattr(
        window, "ask", lambda title, text, detail, buttons: asked.append((text, detail)) or "remove"
    )
    window.offer_pending_work()
    assert asked and "1 unfinished anonymized copy" in asked[0][0]
    assert "never checked: do not use or share them" in asked[0][1]
    assert not temporary.exists()


def test_a_result_made_just_before_a_crash_can_be_kept(qtbot, window, tmp_path):
    """Review round 2 (5): the result is there but the journal never heard:
    the user decides; resume is never blocked by it. ("Make them again" of a
    result this batch made, and the refusal for anyone else's, are in
    test_make_again_removes_only_a_result_this_batch_made.)"""
    window._closing = True
    batch, dest, _work = make_batch(tmp_path, pipeline.FOLDERS, three(tmp_path)[:1])
    j = journal.Journal.new(1, pipeline.to_payload(batch))
    # The copy exists (a stand-in), the journal still says waiting.
    (dest / "S001.mefd").mkdir()
    (dest / "S001.mefd" / "marker").write_text("made before", encoding="utf-8")
    page = window.anonymizer
    asked = []
    page.resume(j, lambda s: "", None, lambda n, total: asked.append(n) or "keep")
    qtbot.waitUntil(lambda: page.run.finished, timeout=180_000)
    assert asked == [[0]]
    assert page.run_step.result_texts["k0"] == "Made before the interruption (not confirmed)"
    assert (dest / "S001.mefd" / "marker").exists()


def test_unreachable_recordings_pause_the_batch_and_can_be_retried(qtbot, window, tmp_path):
    """Review round 2 (6): failures to reach the files are not final; after
    three in a row the batch pauses; "Retry the failed ones" finishes it."""
    window._closing = True
    real = [write_plain(tmp_path / f"r{i}.mefd", notes=False) for i in range(4)]
    away = tmp_path / "away"
    away.mkdir()
    for path in real:
        path.rename(away / path.name)  # the "network drive" is gone
    batch, dest, _work = make_batch(tmp_path, pipeline.FOLDERS, real, exclude=())
    page = window.anonymizer
    page.run_batch(batch)
    qtbot.waitUntil(lambda: page.run.finished, timeout=180_000)
    state = page.batch_state
    assert state.paused and len(state.retry) == 4
    assert (
        "paused"
        in page.run_step.messages.itemAt(0).widget().findChildren(type(page.why_not))[0].text()
    )
    assert page.run_step.retry_button is not None and page.run_step.retry_button.isEnabled()
    for path in real:
        (away / path.name).rename(path)  # it is back
    page.retry_failed()
    qtbot.waitUntil(lambda: page.run.finished, timeout=180_000)
    assert all(t == "Saved and checked" for t in page.run_step.result_texts.values())
    assert sorted(p.name for p in dest.iterdir()) == [f"S00{i}.mefd" for i in (1, 2, 3, 4)]
    assert journal.unfinished() == []
    assert page._retry_batch is None  # the passwords are no longer kept


def test_bundle_rows_say_added_until_the_archive_exists(qtbot, window, tmp_path, monkeypatch):
    """Review round 2 (7): "Saved and checked" only once the batch archive
    is finished; if finishing fails, "Not delivered" and no date key rows."""
    window._closing = True
    page = window.anonymizer
    batch, _dest, _work = make_batch(tmp_path, pipeline.BUNDLE, three(tmp_path)[:1])
    monkeypatch.setattr(
        bundle.BundleWriter, "finish", lambda self: (_ for _ in ()).throw(OSError(5, "gone"))
    )
    page.run_batch(batch)
    qtbot.waitUntil(lambda: page.run.finished, timeout=180_000)
    result = page.run.results["k0"]
    assert result.ok  # the recording itself is in the (unfinished) archive
    outcome = page.bundle_outcome
    assert outcome is not None and not outcome.ok
    page.bundle_outcome = None  # before the archive is finished
    assert page.status_of(result) == "Added to the batch archive"
    page.bundle_outcome = outcome
    assert page.run_step.result_texts["k0"] == "Not delivered"
    assert page.date_key_rows() == []


def test_linux_keep_awake_ends_with_the_app(monkeypatch):
    import sys

    from mef3io_studio.model import awake

    started = []

    class FakeProcess:
        def __init__(self, args, **_kw):
            started.append(args)

        def terminate(self):
            pass

        def wait(self, _timeout):
            return 0

    monkeypatch.delenv(awake.DISABLE_ENV, raising=False)
    monkeypatch.setattr(awake.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(awake.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sys, "platform", "linux")
    with KeepAwake():
        pass
    args = started[0]
    assert f"--pid={os.getpid()}" in args and "infinity" not in args


def test_every_work_folder_ever_used_is_searched(window, tmp_path, monkeypatch):
    from mef3io_studio.common.settings_dialog import set_work_folder, work_folders_used

    window._closing = True
    first, second = tmp_path / "w1", tmp_path / "w2"
    set_work_folder(window.settings, str(first))
    set_work_folder(window.settings, str(second))
    assert {str(first), str(second)} <= set(work_folders_used(window.settings))
    (first / "work-old-0" / "S1.mefd").mkdir(parents=True)
    asked = []
    monkeypatch.setattr(
        window, "ask", lambda title, text, detail, buttons: asked.append(text) or "remove"
    )
    window.offer_pending_work()
    assert asked and not (first / "work-old-0").exists()
    stored = Path(window.settings._qs.fileName()).read_text(encoding="utf-8", errors="replace")
    assert str(tmp_path) not in stored


def test_shared_drive_needs_room_for_the_work_copy_too(tmp_path):
    batch, _dest, _work = make_batch(tmp_path, pipeline.ARCHIVES, [tmp_path / "x.mefd"])
    total = int(1_000_000 * pipeline.MARGIN) + pipeline.EXTRA_BYTES
    assert pipeline.space_problems(batch, 10**12, total, same_volume=False) == []
    assert pipeline.space_problems(batch, 10**12, total, same_volume=True)
    assert pipeline.same_volume(str(tmp_path), str(tmp_path / "out"))


def test_the_journal_holds_no_original_names_or_dates(tmp_path):
    batch, _dest, _work = make_batch(tmp_path, pipeline.ARCHIVES, [tmp_path / f"{LAST}.mefd"])
    payload = pipeline.to_payload(batch)
    text = json.dumps(payload)
    assert str(T0) not in text and '"original"' not in text
    enc, mac = journal._keys()
    assert enc != mac and len(enc) == len(mac) == 32


def test_the_tray_icon_goes_after_the_notification(window, monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon

    import mef3io_studio.app as app_module

    window._closing = True
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    monkeypatch.setattr(app_module, "NOTIFY_SECONDS", 0)
    window.notify("done")
    assert window._tray is not None
    from PySide6.QtWidgets import QApplication

    for _ in range(20):
        QApplication.processEvents()
    assert not window._tray.isVisible()


def test_placeholders_collapse_in_technical_details():
    assert a.collapse_placeholders("x <path><recording>/<name>.dat") == "x <path>.dat"


# --------------------------------------------------------------------------
# Review round 3


def test_make_again_removes_only_a_result_this_batch_made(qtbot, window, tmp_path):
    """Round 3 (A): "Make them again" removes a result only when it is
    provably this batch's (anonymizer stamp, the copy's name, the original's
    start plus the shift); a foreign folder with the same name is kept."""
    window._closing = True
    page = window.anonymizer
    batch, dest, _work = make_batch(tmp_path, pipeline.FOLDERS, three(tmp_path)[:2])
    # Recording 1: really made by this batch (then interrupted before the
    # journal heard). Recording 2: a foreign folder that happens to have the name.
    from dataclasses import replace

    first = replace(batch, items=batch.items[:1])
    run(qtbot, window.jobs, first)
    (dest / "S002.mefd").mkdir()
    (dest / "S002.mefd" / "someone-elses.txt").write_text("keep me", encoding="utf-8")
    j = journal.Journal.new(2, pipeline.to_payload(batch))
    refused = []
    page.resume_problems.connect(refused.append)
    page.resume(j, lambda s: "", None, lambda n, total: "redo")
    qtbot.waitUntil(lambda: bool(refused), timeout=120_000)
    assert "Recording 2 of 2" in refused[0] and "not made by this batch" in refused[0]
    assert (dest / "S002.mefd" / "someone-elses.txt").exists()  # left exactly as it is
    assert (dest / "S001.mefd").is_dir() and page.handle is None  # nothing started
    # Without the foreign one, "make them again" removes ours and remakes it.
    import shutil

    shutil.rmtree(dest / "S002.mefd")
    page.resume(j, lambda s: "", None, lambda n, total: "redo")
    qtbot.waitUntil(lambda: page.run.finished, timeout=180_000)
    assert page.run_step.result_texts["k0"] == "Saved and checked"
    assert sorted(p.name for p in dest.iterdir()) == ["S001.mefd", "S002.mefd"]


def _check(path, src, name="S9", shift=SHIFT, exclude=("a3",), output=None):
    return {
        "path": str(path),
        "source": str(src),
        "source_password": "",
        "name": name,
        "shift": shift,
        "exclude": list(exclude),
        "output_password": list(output) if output else None,
    }


def test_made_by_this_batch_checks_stamp_name_start_channels_and_passwords(tmp_path):
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    copy = tmp_path / "S9.mefd"
    mef3io.anonymize_session(str(src), str(copy), SHIFT, exclude_channels=["a3"])
    verdicts = pipeline.made_by_this_batch(
        [
            _check(copy, src),  # ours
            _check(copy, src, name="S8"),  # another name
            _check(copy, src, shift=SHIFT + 1),  # another shift
            _check(src, src, name="orig", shift=0, exclude=()),  # not made by the anonymizer
        ]
    )
    assert verdicts == [True, False, False, False]
    tar = mef3io.archive_session(str(copy), str(tmp_path / "S9.mefd.tar"))
    assert pipeline.made_by_this_batch([_check(tar, src)]) == [True]


def test_another_copy_of_the_same_original_is_not_ours(tmp_path):
    """Round 4 (1): the reviewer's case. A plain anonymized copy of the same
    original, with the same name and shift but other channels, is not this
    batch's result (and so is never removed); nor is one whose passwords
    differ from the ones asked for."""
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    other = tmp_path / "other" / "S9.mefd"
    other.parent.mkdir()
    mef3io.anonymize_session(str(src), str(other), SHIFT)  # all three channels
    assert pipeline.made_by_this_batch([_check(other, src, exclude=("a3",))]) == [False]
    assert pipeline.made_by_this_batch([_check(other, src, exclude=())]) == [True]
    # This batch asked for passwords: an unprotected copy is not it ...
    asked = ("bas-1", "full-2")
    assert pipeline.made_by_this_batch([_check(other, src, exclude=(), output=asked)]) == [False]
    # ... and a protected one only with exactly those passwords.
    locked = tmp_path / "locked" / "S9.mefd"
    locked.parent.mkdir()
    mef3io.anonymize_session(str(src), str(locked), SHIFT, output_password=asked)
    assert pipeline.made_by_this_batch([_check(locked, src, exclude=(), output=asked)]) == [True]
    wrong = ("bas-1", "other-9")
    assert pipeline.made_by_this_batch([_check(locked, src, exclude=(), output=wrong)]) == [False]
    assert pipeline.made_by_this_batch([_check(locked, src, exclude=())]) == [False]


def test_make_again_keeps_another_copy_of_the_same_original(qtbot, window, tmp_path):
    """Round 4 (1): end to end, "Make them again" leaves such a copy alone."""
    window._closing = True
    page = window.anonymizer
    batch, dest, _work = make_batch(tmp_path, pipeline.FOLDERS, three(tmp_path)[:1])
    mef3io.anonymize_session(str(batch.items[0].request.source), str(dest / "S001.mefd"), SHIFT)
    j = journal.Journal.new(1, pipeline.to_payload(batch))
    refused = []
    page.resume_problems.connect(refused.append)
    page.resume(j, lambda s: "", None, lambda n, total: "redo")
    qtbot.waitUntil(lambda: bool(refused), timeout=120_000)
    assert "not made by this batch" in refused[0]
    names = [c.name for c in mef3io.inspect(str(dest / "S001.mefd")).channels]
    assert names == ["a1", "a2", "a3"]  # still there, untouched


# Issue #36: the identity choices are compared too


def _copy_of(src, path, **options):
    path.parent.mkdir(parents=True, exist_ok=True)
    mef3io.anonymize_session(str(src), str(path), SHIFT, exclude_channels=["a3"], **options)
    return path


def test_made_by_this_batch_compares_the_identity_choices(tmp_path):
    """Same original, name, shift, channels and passwords: a copy whose
    patient details, location, descriptions, note text or pseudonym were
    handled differently from this batch's choices is not this batch's."""
    src = write_plain(tmp_path / "orig.mefd")  # with notes, one on a3's neighbour a1
    ours = _copy_of(src, tmp_path / "ours" / "S9.mefd")
    others = [
        _copy_of(src, tmp_path / "patient" / "S9.mefd", clear=("location", "descriptions")),
        _copy_of(src, tmp_path / "location" / "S9.mefd", clear=("subject", "descriptions")),
        _copy_of(src, tmp_path / "descriptions" / "S9.mefd", clear=("subject", "location")),
        _copy_of(src, tmp_path / "text" / "S9.mefd", text="keep"),
        _copy_of(src, tmp_path / "pseudonym" / "S9.mefd", pseudonym="P-17"),
    ]
    verdicts = pipeline.made_by_this_batch([_check(p, src) for p in [ours, *others]])
    assert verdicts == [True, False, False, False, False, False]
    tar = mef3io.archive_session(str(ours), str(tmp_path / "S9.mefd.tar"))
    assert pipeline.made_by_this_batch([_check(tar, src)]) == [True]


def test_a_batch_that_kept_identity_recognises_its_own_copy(tmp_path):
    """The other way round: a batch that kept the patient's names, the
    descriptions and the note text (names taken out of it) and left a1 out
    recognises exactly that copy, and not one with everything cleared."""
    src = write_plain(tmp_path / "orig.mefd")
    options = dict(clear=("location",), text="keep", pseudonym="P-17")
    kept = tmp_path / "kept" / "S9.mefd"
    kept.parent.mkdir()
    mef3io.anonymize_session(str(src), str(kept), SHIFT, exclude_channels=["a1"], **options)
    check = dict(_check(kept, src, exclude=("a1",)), clear=["location"], text="keep")
    check["pseudonym"] = "P-17"
    assert pipeline.made_by_this_batch([check]) == [True]
    cleared = tmp_path / "cleared" / "S9.mefd"
    cleared.parent.mkdir()
    mef3io.anonymize_session(
        str(src), str(cleared), SHIFT, exclude_channels=["a1"], pseudonym="P-17", text="keep"
    )
    assert pipeline.made_by_this_batch([dict(check, path=str(cleared))]) == [False]


def test_result_check_carries_the_identity_choices(tmp_path):
    batch, _dest, _work = make_batch(tmp_path, pipeline.FOLDERS, [tmp_path / "x.mefd"])
    item = batch.items[0]
    item = replace_request(item, pseudonym="P-1", clear=("subject",), text="keep")
    check = pipeline.result_check(item)
    assert (check["pseudonym"], check["clear"], check["text"]) == ("P-1", ["subject"], "keep")


@pytest.mark.parametrize(
    "options",
    [{"clear": ("location", "descriptions")}, {"text": "keep"}],
    ids=["patient details kept", "note text kept"],
)
def test_make_again_keeps_a_copy_with_other_identity_choices(qtbot, window, tmp_path, options):
    """End to end: "Make them again" leaves such a copy exactly as it is,
    and says so in the same plain words as for any other copy."""
    window._closing = True
    page = window.anonymizer
    batch, dest, _work = make_batch(tmp_path, pipeline.FOLDERS, three(tmp_path)[:1])
    _copy_of(batch.items[0].request.source, dest / "S001.mefd", **options)
    before = sorted(p.relative_to(dest) for p in dest.rglob("*"))
    j = journal.Journal.new(1, pipeline.to_payload(batch))
    refused = []
    page.resume_problems.connect(refused.append)
    page.resume(j, lambda s: "", None, lambda n, total: "redo")
    qtbot.waitUntil(lambda: bool(refused), timeout=120_000)
    assert "not made by this batch" in refused[0]
    assert sorted(p.relative_to(dest) for p in dest.rglob("*")) == before  # untouched
    assert page.handle is None  # nothing started


# Review of PR #38: what must survive when the batch KEPT text, descriptions
# or patient details


def _source(path, notes=(), acquisition=None, subject=None, channels=("a1", "a2", "a3")):
    """A made-up recording: patient details, descriptions and notes as given
    (notes: (seconds after the start, text, channel or None))."""
    import numpy as np

    subject = subject or mef3io.Subject(
        name_1=FIRST, name_2=LAST, id=PATIENT_ID, recording_location="Ward 9"
    )
    meta = mef3io.Metadata(subject=subject, acquisition=acquisition or mef3io.Acquisition())
    with mef3io.Writer(str(path), overwrite=True, units="uV", metadata=meta) as w:
        for name in channels:
            w.write(name, np.sin(np.arange(2560) / 10.0), T0, 256.0)
        # One call per place: a second call for the same place replaces it.
        for place in dict.fromkeys(channel for _s, _t, channel in notes):
            w.write_annotations(
                [
                    {"time": T0 + seconds * 1_000_000, "text": text}
                    for seconds, text, channel in notes
                    if channel == place
                ],
                place,
            )
    return path


def _copy(src, path, exclude=("a3",), **options):
    path.parent.mkdir(parents=True, exist_ok=True)
    mef3io.anonymize_session(
        str(src), str(path), SHIFT, exclude_channels=list(exclude) or None, **options
    )
    return path


def _asked(path, src, exclude=("a3",), clear=a.CLEAR_ALL, text="drop"):
    return dict(_check(path, src, exclude=exclude), clear=list(clear), text=text)


NAME_ONLY_NOTES = (
    (1, LAST, None),  # only a name
    (2, f"{FIRST} {LAST}, {PATIENT_ID}", None),  # only names and the ID
    (3, f"{LAST} moved", None),  # more than names: must survive
    (4, "eyes open", "a1"),
    (5, "artefact", "a3"),  # on a channel this batch leaves out
)


def test_kept_note_text_must_survive_in_the_copy(tmp_path):
    """Finding 1: a batch that KEPT note text does not take a copy whose
    text was dropped; its own copy, where the notes holding only names came
    out empty, is still its own."""
    src = _source(tmp_path / "orig.mefd", NAME_ONLY_NOTES)
    ours = _copy(src, tmp_path / "ours" / "S9.mefd", text="keep")
    dropped = _copy(src, tmp_path / "dropped" / "S9.mefd")
    from mef3io_studio.model.inspect import inspect_recording

    texts = [n.text for n in inspect_recording(str(ours), "").details.notes]
    # mef3io took every word out of the two notes holding only names.
    assert texts[:2] == ["", " , "] and texts[2] == " moved"
    asked = [_asked(p, src, text="keep") for p in (ours, dropped)]
    assert pipeline.made_by_this_batch(asked) == [True, False]


def test_kept_names_and_text_own_copy_is_recognised(tmp_path):
    """Patient details kept too: mef3io leaves the names in the text, and
    the batch's own copy is still its own; the dropped one still is not."""
    src = _source(tmp_path / "orig.mefd", NAME_ONLY_NOTES)
    keep = ("location",)
    ours = _copy(src, tmp_path / "ours" / "S9.mefd", text="keep", clear=keep)
    dropped = _copy(src, tmp_path / "dropped" / "S9.mefd", clear=keep)
    asked = [_asked(p, src, clear=keep, text="keep") for p in (ours, dropped)]
    assert pipeline.made_by_this_batch(asked) == [True, False]


def test_the_known_limit_of_kept_note_text(tmp_path):
    """What remains: when every note holds only names, a copy that dropped
    the text cannot be told from this batch's (both come out empty)."""
    src = _source(tmp_path / "orig.mefd", NAME_ONLY_NOTES[:2])
    dropped = _copy(src, tmp_path / "dropped" / "S9.mefd")
    assert pipeline.made_by_this_batch([_asked(dropped, src, text="keep")]) == [True]


DESCRIBED = dict(
    session_description="Overnight study, ward round",
    channel_description="left hippocampus",
    reference_description="linked mastoids",
)


@pytest.mark.parametrize("exclude", [("a3",), ()], ids=["a3 left out", "all channels"])
def test_kept_descriptions_must_survive_in_the_copy(tmp_path, exclude):
    """Finding 2: a batch that KEPT the descriptions does not take a copy
    whose descriptions were cleared; its own copy is its own."""
    src = _source(tmp_path / "orig.mefd", acquisition=mef3io.Acquisition(**DESCRIBED))
    keep = ("subject", "location")
    ours = _copy(src, tmp_path / "ours" / "S9.mefd", exclude=exclude, clear=keep)
    cleared = _copy(src, tmp_path / "cleared" / "S9.mefd", exclude=exclude)
    asked = [_asked(p, src, exclude=exclude, clear=keep) for p in (ours, cleared)]
    assert pipeline.made_by_this_batch(asked) == [True, False]


def test_only_the_reference_description_kept_is_still_required(tmp_path):
    """No channel left out: the reference description (it holds no names)
    must be there when the batch kept descriptions."""
    acquisition = mef3io.Acquisition(reference_description="linked mastoids")
    src = _source(tmp_path / "orig.mefd", acquisition=acquisition)
    keep = ("subject", "location")
    ours = _copy(src, tmp_path / "ours" / "S9.mefd", exclude=(), clear=keep)
    cleared = _copy(src, tmp_path / "cleared" / "S9.mefd", exclude=())
    asked = [_asked(p, src, exclude=(), clear=keep) for p in (ours, cleared)]
    assert pipeline.made_by_this_batch(asked) == [True, False]


def _mixed_patients(tmp_path):
    """a1 and a2 of one patient record, a3 written with other details (as a
    recording assembled from two exports would be)."""
    import shutil

    main = _source(tmp_path / "a" / "orig.mefd", channels=("a1", "a2"))
    other = mef3io.Subject(
        name_1="Ottilie", name_2="Quist", id="MRN-1", recording_location="Ward 9"
    )
    extra = _source(tmp_path / "b" / "orig.mefd", subject=other, channels=("a3",))
    shutil.copytree(extra / "a3.timd", main / "a3.timd")
    return main


def test_kept_patient_details_with_a_channel_left_out(tmp_path):
    """Finding 3: patient details that differ between channels. Kept, with
    the differing channel left out, the batch's own copy is still its own;
    with nothing left out, its own copy is too, and a copy without the
    patient details is not."""
    src = _mixed_patients(tmp_path)
    keep = ("location", "descriptions")
    ours = _copy(src, tmp_path / "ours" / "S9.mefd", clear=keep)
    assert pipeline.made_by_this_batch([_asked(ours, src, clear=keep)]) == [True]
    all_kept = _copy(src, tmp_path / "all" / "S9.mefd", exclude=(), clear=keep)
    cleared = _copy(src, tmp_path / "cleared" / "S9.mefd", exclude=())
    asked = [_asked(p, src, exclude=(), clear=keep) for p in (all_kept, cleared)]
    assert pipeline.made_by_this_batch(asked) == [True, False]


def test_resume_dialog_names_each_original_and_can_share_a_password(qtbot):
    from mef3io_studio.anonymizer.resume import ResumePasswordsDialog

    dialog = ResumePasswordsDialog([0, 2], 3, False, names={0: "Ward-A", 2: "Ward-C"})
    qtbot.addWidget(dialog)
    labels = [label.text() for label in dialog._labels.values()]
    assert labels == [
        "Full-access password of Ward-A (recording 1 of 3):",
        "Full-access password of Ward-C (recording 3 of 3):",
    ]
    dialog.same.setChecked(True)
    dialog.sources[0].setText("shared")
    assert dialog.problem_text() == ""
    assert dialog.values() == ({0: "shared", 2: "shared"}, None)


def test_an_unreadable_key_fails_closed_and_is_never_replaced(tmp_path, monkeypatch):
    """Round 3 (C): only a MISSING key is created; an unreadable or damaged
    one stops sealing and unsealing, keeps the file and every journal."""
    j = journal.Journal.new(1, {"items": []})
    key_path = Path(journal.data_dir()) / "journal.key"
    good = key_path.read_bytes()
    key_path.write_bytes(b"short")  # damaged
    with pytest.raises(journal.KeyUnavailable):
        journal.unfinished()
    assert key_path.read_bytes() == b"short"  # not replaced
    assert Path(j.path).exists()  # the journal was not judged unreadable
    with pytest.raises(journal.KeyUnavailable):
        journal.seal({"x": 1})
    from mef3io_studio.model.errors import describe_error

    assert describe_error(journal.KeyUnavailable()).title == "The app's notes could not be opened"
    key_path.write_bytes(good)
    assert [x.batch_id for x in journal.unfinished()] == [j.batch_id]
    j.delete()


def test_an_unreadable_key_is_reported_plainly_at_start(window, monkeypatch):
    window._closing = True
    shown = []
    monkeypatch.delenv("MEF3IO_STUDIO_NO_STARTUP_OFFERS", raising=False)
    monkeypatch.setattr(
        journal, "unfinished", lambda: (_ for _ in ()).throw(journal.KeyUnavailable())
    )
    monkeypatch.setattr(window, "show_error", shown.append)
    window._closing = False  # the offers are skipped while the window closes
    try:
        window._startup_offers()
    finally:
        window._closing = True
    assert shown and shown[0].title == "The app's notes could not be opened"
    assert "Nothing was removed" in shown[0].text


def test_a_kept_checked_copy_gets_its_own_question(window, tmp_path, monkeypatch):
    """Round 3 (D): a copy kept because its archive did not match is not a
    never-checked leftover."""
    from mef3io_studio.common.settings_dialog import set_work_folder

    window._closing = True
    work = tmp_path / "work"
    kept = work / "work-abc-0"
    (kept / "S1.mefd").mkdir(parents=True)
    set_work_folder(window.settings, str(work))
    journal.note_kept_copy(str(kept))
    asked = []
    monkeypatch.setattr(
        window, "ask", lambda title, text, detail, buttons: asked.append((title, detail)) or "keep"
    )
    window.offer_pending_work()
    assert [t for t, _d in asked] == ["Copies kept after a failed archive"]
    assert "checked and are complete" in asked[0][1]
    assert kept.exists()
    journal.forget_kept_copy(str(kept))


def test_a_full_drive_is_not_a_network_blip():
    import errno as errno_

    full = OSError(errno_.ENOSPC, "No space left on device")
    assert not a.is_unreachable(full) and not tasks._retryable(full)
    message = a.describe_copy_error(full)
    assert message.title == "The destination or the work folder is full"
    if hasattr(errno_, "EDQUOT"):
        assert not a.is_unreachable(OSError(errno_.EDQUOT, "Disk quota exceeded"))
