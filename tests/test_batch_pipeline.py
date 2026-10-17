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
    yield
    shutil.rmtree(journal.journal_dir(), ignore_errors=True)
    journal._save_temporaries([])


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


@pytest.mark.parametrize("choice", ["keep", "redo"])
def test_a_result_made_just_before_a_crash_is_kept_or_made_again(
    qtbot, window, tmp_path, monkeypatch, choice
):
    """Review round 2 (5): the result is there but the journal never heard:
    the user decides; resume is never blocked by it."""
    window._closing = True
    batch, dest, _work = make_batch(tmp_path, pipeline.FOLDERS, three(tmp_path)[:1])
    j = journal.Journal.new(1, pipeline.to_payload(batch))
    # The copy exists (a stand-in), the journal still says waiting.
    (dest / "S001.mefd").mkdir()
    (dest / "S001.mefd" / "marker").write_text("made before", encoding="utf-8")
    page = window.anonymizer
    asked = []
    page.resume(j, lambda s: "", None, lambda n, total: asked.append(n) or choice)
    qtbot.waitUntil(lambda: page.run.finished, timeout=180_000)
    assert asked == [[0]]
    status = page.run_step.result_texts["k0"]
    if choice == "keep":
        assert status == "Made before the interruption (not confirmed)"
        assert (dest / "S001.mefd" / "marker").exists()
    else:
        assert status == "Saved and checked"
        assert not (dest / "S001.mefd" / "marker").exists()
        assert mef3io.inspect(str(dest / "S001.mefd")).start_time == T0 + SHIFT


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
