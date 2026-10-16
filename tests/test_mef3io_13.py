"""mef3io 1.3 in the app: inspect() in the Explorer, its stop token and
progress in the job layer, and the password fixes (bnelair/mef3io#67, #69,
#84). Synthetic recordings only."""

from __future__ import annotations

import os
import time
from pathlib import Path

import helper_targets
import mef3io
import pytest
from explorer_sessions import (
    LAST,
    PASSWORD_BASIC,
    PASSWORD_FULL,
    damage_details,
    write_encrypted,
    write_encrypted_with_notes,
    write_large,
    write_legacy_password,
    write_plain,
)

from mef3io_studio.model import archive
from mef3io_studio.model.cancel import CancelToken, JobCancelled, is_stop, stopped_by_library
from mef3io_studio.model.errors import describe_error
from mef3io_studio.model.inspect import inspect_recording
from mef3io_studio.model.jobs import JobStatus
from mef3io_studio.model.progress import ByteProgress
from mef3io_studio.model.summary import LEGACY_PASSWORD_NOTE, UNVERIFIED_NOTE, unverified_note
from mef3io_studio.workers.isolated import run_isolated

# --------------------------------------------------------------------------
# One stop flag, inside the library too
# --------------------------------------------------------------------------


def test_library_token_follows_ours():
    token = CancelToken()
    library = token.for_library()
    assert isinstance(library, mef3io.CancelToken)
    assert token.for_library() is library  # one per task
    assert not library.cancelled
    token.cancel()
    assert library.cancelled
    late = CancelToken()
    late.cancel()
    assert late.for_library().cancelled  # linked after the fact too


def test_library_stop_is_a_stop_not_an_error():
    stop = mef3io.Cancelled("operation cancelled")
    assert is_stop(stop) and is_stop(JobCancelled())
    assert not is_stop(mef3io.MefError("broken")) and not is_stop(RuntimeError())
    assert describe_error(stop).title == "Stopped"
    with pytest.raises(JobCancelled), stopped_by_library():
        raise stop
    with pytest.raises(ValueError), stopped_by_library():
        raise ValueError("not a stop")


def test_a_job_stopped_inside_mef3io_ends_as_stopped(qtbot, jobs):
    def work(ctx):
        raise mef3io.Cancelled("operation cancelled")

    handle = jobs.submit("Stopping", work)
    with qtbot.waitSignal(handle.cancelled, timeout=10_000):
        pass
    assert handle.record.status is JobStatus.CANCELLED and handle.error is None


# --------------------------------------------------------------------------
# Helper processes that stop by themselves
# --------------------------------------------------------------------------


def _stop_soon(token, delay=0.3):
    import threading

    threading.Timer(delay, token.cancel).start()


def test_helper_gets_the_stop_and_finishes_by_itself(tmp_path):
    marker = tmp_path / "marker"
    token = CancelToken()
    _stop_soon(token)
    with pytest.raises(JobCancelled):
        run_isolated(helper_targets.stop_cleanly, str(marker), cancel=token, stop_grace=10)
    assert marker.read_text() == "stopped cleanly"


def test_helper_that_ignores_the_stop_is_ended_after_the_grace(tmp_path):
    token = CancelToken()
    _stop_soon(token, 0.2)
    start = time.monotonic()
    with pytest.raises(JobCancelled):
        run_isolated(helper_targets.ignore_stop, cancel=token, stop_grace=0.5)
    assert time.monotonic() - start < 8


# --------------------------------------------------------------------------
# Stop and progress inside mef3io's long operations
# --------------------------------------------------------------------------


def test_inspect_stops_inside_mef3io(tmp_path):
    path = str(write_plain(tmp_path / "rec.mefd"))
    token = CancelToken()
    with pytest.raises(JobCancelled):
        inspect_recording(path, cancel=token, progress=lambda done, total: token.cancel())


@pytest.fixture(scope="module")
def medium(tmp_path_factory):
    return write_large(tmp_path_factory.mktemp("medium") / "medium.mefd", parts=2, seconds=120)


def test_packing_reports_mef3io_progress_then_checks(medium, tmp_path):
    plan = archive.plan(str(medium), str(tmp_path))
    sent = []
    archive.run_in_helper(plan, send=sent.append)
    progress = [m for m in sent if isinstance(m, ByteProgress)]
    assert progress and progress[-1].done == progress[-1].total > 0
    assert sent[-1] == "checking" and "checking" not in sent[:-1]
    archive.finish(plan)


@pytest.mark.parametrize("packing", [True, False])
def test_stop_while_packing_leaves_nothing_behind(medium, tmp_path, packing):
    source = str(medium)
    if not packing:
        source = mef3io.archive_session(source, str(tmp_path / "medium.mefd.tar"))
    out = tmp_path / "out"
    out.mkdir()
    plan = archive.plan(source, str(out))
    token = CancelToken()
    with pytest.raises(JobCancelled):
        archive.run_in_helper(plan, send=lambda _m: token.cancel(), cancel=token)
    archive.remove_temporary(plan)  # what the job's cleanup does
    assert os.listdir(out) == []


def test_stop_button_stops_packing_in_the_job_layer(qtbot, jobs, medium, tmp_path):
    from mef3io_studio.workers.tasks import pack_or_unpack

    out = tmp_path / "out"
    out.mkdir()
    handle = jobs.submit("Packing", pack_or_unpack, str(medium), str(out))
    handle.cancel()
    with qtbot.waitSignal(handle.finished, timeout=30_000):
        pass
    assert handle.record.status is JobStatus.CANCELLED
    assert os.listdir(out) == []


class _RecordingCtx:
    """A JobContext that keeps every progress report (the real one keeps
    only the newest, and the UI samples it ten times a second)."""

    job_id = 0

    def __init__(self):
        self.cancel_token = CancelToken()
        self.reports = []
        self.cleanups = []

    def report(self, done, total=None, phase=""):
        self.reports.append((done, total, phase))

    def check_cancelled(self):
        self.cancel_token.raise_if_cancelled()

    def add_cleanup(self, fn):
        self.cleanups.append(fn)

    def run_isolated(self, fn, *args, **kwargs):
        return run_isolated(fn, *args, cancel=self.cancel_token, **kwargs)


def test_pack_job_reports_mef3io_bytes(medium, tmp_path):
    from mef3io_studio.workers.tasks import pack_or_unpack

    ctx = _RecordingCtx()
    result = pack_or_unpack(ctx, str(medium), str(tmp_path))
    assert Path(result).exists()
    copying = [(d, t) for d, t, phase in ctx.reports if phase == "Packing into one file" and t]
    assert copying and copying[-1][0] == copying[-1][1] > 0  # real byte counts, to the end
    assert ctx.reports[-1][2] == "Checking the copy"


# --------------------------------------------------------------------------
# Passwords, locked parts and unverified times
# --------------------------------------------------------------------------


def test_basic_password_times_are_unverified_not_guessed(tmp_path):
    d = inspect_recording(str(write_encrypted(tmp_path / "e.mefd")), PASSWORD_BASIC).details
    # mef3io read the times assuming the common case: shown, never "verified".
    assert d.offset_assumed_zero and not d.times_verified
    assert d.recording_time_offset is None
    assert unverified_note(d) == UNVERIFIED_NOTE
    full = inspect_recording(str(tmp_path / "e.mefd"), PASSWORD_FULL).details
    assert not full.offset_assumed_zero and full.times_verified


def test_legacy_password_is_a_note_not_an_error(tmp_path):
    path, legacy = write_legacy_password(tmp_path / "old.mefd")
    outcome = inspect_recording(str(path), legacy)
    assert outcome.error is None and outcome.details is not None
    assert outcome.details.legacy_password
    current = inspect_recording(str(path), "f©2").details
    assert current is not None and not current.legacy_password
    # ASCII passwords are never checked twice (the two rules agree on them).
    plain = inspect_recording(str(write_encrypted(tmp_path / "e.mefd")), PASSWORD_FULL)
    assert not plain.details.legacy_password


def test_locked_note_text_with_the_basic_password(tmp_path):
    path = str(write_encrypted_with_notes(tmp_path / "n.mefd"))
    basic = inspect_recording(path, PASSWORD_BASIC).details
    assert basic.notes and all(n.locked and n.text == "" for n in basic.notes)
    assert basic.notes_locked == len(basic.notes)
    full = inspect_recording(path, PASSWORD_FULL).details
    assert full.notes[0].text == f"{LAST} moved" and not full.notes[0].locked


def test_notes_of_an_unreadable_part_are_counted_not_guessed(tmp_path):
    path = damage_details(write_plain(tmp_path / "rec.mefd"))
    d = inspect_recording(str(path)).details
    assert d.part_problems and d.notes_without_times > 0
    assert all(n.time is not None for n in d.notes)


# --------------------------------------------------------------------------
# On screen
# --------------------------------------------------------------------------


@pytest.fixture
def page(window):
    window._closing = True
    window.show()
    return window.explorer


def _wait_loaded(qtbot, page, n):
    qtbot.waitUntil(
        lambda: len(page.entries()) == n and all(not e.busy for e in page.entries()),
        timeout=60_000,
    )


def _notice_texts(panel):
    from PySide6.QtWidgets import QLabel

    return [w.text() for w in panel.findChildren(QLabel)]


def test_unreadable_part_is_shown_in_plain_words(page, qtbot, tmp_path):
    path = damage_details(write_plain(tmp_path / "rec.mefd"))
    page.open_paths([str(path)], interactive=False)
    _wait_loaded(qtbot, page, 1)
    entry = page.entries()[0]
    page.select_entry(entry)
    texts = _notice_texts(page.overview)
    assert any(t.startswith("One part of this recording could not be read") for t in texts)
    assert page.model.display(entry, 8) == "Some parts could not be read"
    assert "some could not be read" in page.overview.values["Notes and events"]


def test_legacy_password_note_on_screen(page, qtbot, tmp_path):
    path, legacy = write_legacy_password(tmp_path / "old.mefd")
    page.open_paths([str(path)], interactive=False, password=legacy)
    _wait_loaded(qtbot, page, 1)
    entry = page.entries()[0]
    assert entry.details is not None and entry.details.legacy_password
    page.select_entry(entry)
    assert LEGACY_PASSWORD_NOTE in _notice_texts(page.overview)
