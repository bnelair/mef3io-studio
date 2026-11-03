"""Rename (#12), review round 1: a partly failed undo, stale viewer and
recency checks, a forced stop, the undo password, pattern limits, notes
scrubbing, newest-part order."""

from __future__ import annotations

import logging
import os
import unicodedata

import mef3io
import pytest
import test_rename_ui as base
from explorer_sessions import PASSWORD_BASIC, T0, write_encrypted, write_plain
from test_rename_model import age
from test_rename_ui import open_all, opened_dialog, settle, write_at

from mef3io_studio.common import privacy
from mef3io_studio.common.logging_setup import setup_logging, teardown_logging
from mef3io_studio.model import rename as rn
from mef3io_studio.model import rename_ops as ops
from mef3io_studio.model.cancel import JobCancelled
from mef3io_studio.workers import tasks

page = base.page  # the fixtures of the Rename window tests
three = base.three

# -- 1. a partly failed undo keeps only what is still to do ------------------------------------


def test_a_partly_failed_undo_keeps_the_steps_still_to_do(tmp_path, monkeypatch):
    src = age(write_plain(tmp_path / "old.mefd"))
    os.rename(src, tmp_path / "other.mefd")
    other = tmp_path / "other.mefd"
    folder = str(tmp_path / "bk")
    (result,) = ops.run_renames(
        [ops.RenameJob("k", str(other), "final", fix_drift_first=True)], folder
    )
    assert [s.kind for s in result.steps] == ["drift", "rename"]
    real = mef3io.naming.fix_name_drift

    def failing(*a, **kw):
        raise mef3io.IoError("simulated")

    monkeypatch.setattr(mef3io.naming, "fix_name_drift", failing)
    first = ops.undo_steps(result.steps, "", folder)
    assert not first.ok
    # The rename WAS undone, so only the drift step is left, and the place is the old folder.
    assert [s.kind for s in first.steps] == ["drift"]
    assert first.new_location == str(other) and other.exists()
    monkeypatch.setattr(mef3io.naming, "fix_name_drift", real)
    retry = ops.undo_steps(first.steps, "", folder)  # does not re-run the rename undo
    assert retry.ok and retry.steps == ()
    assert set(mef3io.name_drift(other).stored_names) == {"old"}


def test_the_window_keeps_only_the_steps_still_to_do_after_a_partial_undo(
    qtbot, page, three, tmp_path, monkeypatch
):
    os.rename(three[0], tmp_path / "moved.mefd")
    open_all(qtbot, page, [tmp_path / "moved.mefd"])
    dialog = opened_dialog(qtbot, page)
    dialog.set_new_name(0, "final")
    dialog.confirm = lambda text, more: True
    dialog.start()
    qtbot.waitUntil(lambda: dialog._done, timeout=60_000)
    unit = dialog.batch.units[0]
    assert [s.kind for s in unit.steps] == ["drift", "rename"]
    monkeypatch.setattr(
        mef3io.naming, "fix_name_drift", lambda *a, **k: (_ for _ in ()).throw(mef3io.IoError("x"))
    )
    results = ops.undo_many([unit], ops.backup_dir())
    monkeypatch.undo()
    moved = []
    dialog.relocated.connect(moved.append)
    dialog._undone((results, False))
    (left,) = dialog.batch.units
    assert [s.kind for s in left.steps] == ["drift"]
    assert left.password == "" and moved and moved[0][0][1].endswith("moved.mefd")


# -- 2. viewers and the list are asked again at the confirmation and at undo -------------------


def test_a_viewer_opened_after_the_window_was_shown_blocks_rename_and_undo(
    qtbot, page, three, tmp_path
):
    open_all(qtbot, page, three[:1])
    open_now: set[str] = set()
    page.in_use_keys = lambda: set(open_now)
    dialog = opened_dialog(qtbot, page)
    dialog.set_new_name(0, "first-name")
    open_now.add(page.entries()[0].key)  # a viewer is opened while the window is up
    dialog.confirm = lambda text, more: pytest.fail("must not ask")
    dialog.start()
    assert not dialog.busy and three[0].exists()
    assert "signal viewer" in dialog.table.item(0, 3).text()
    open_now.clear()
    dialog.confirm = lambda text, more: True
    dialog._recheck()
    dialog.start()
    qtbot.waitUntil(lambda: dialog._done, timeout=60_000)
    settle(qtbot, page)
    new_key = page.entries()[0].key
    open_now.add(new_key)
    jobs_before = len(page.jobs.handles)
    dialog.start_undo()
    assert "signal viewer" in dialog.status.text() and dialog.batch
    assert len(page.jobs.handles) == jobs_before  # nothing was started
    open_now.clear()
    # An entry taken out of the list is dropped from the undo batch.
    page.remove_selected() if page.selected_entries() else page.model.remove(page.entries())
    dialog.start_undo()
    assert dialog.batch is None and (tmp_path / "first-name.mefd").exists()


# -- 3. recency is checked again inside the helper, right before each rename -------------------


def test_a_recording_written_after_the_window_looked_is_not_renamed(tmp_path):
    src = write_plain(tmp_path / "busy.mefd")  # just written
    (result,) = ops.run_renames([ops.RenameJob("k", str(src), "renamed")], str(tmp_path / "bk"))
    assert not result.ok and src.exists() and not (tmp_path / "renamed.mefd").exists()
    assert "last few minutes" in result.message.text
    age(src)
    (ok,) = ops.run_renames([ops.RenameJob("k", str(src), "renamed")], str(tmp_path / "bk"))
    assert ok.ok


# -- 4. a forced stop: a marker means "not renamed" --------------------------------------------


class _Ctx:
    job_id = 1
    cancel_token = None

    def __init__(self, run):
        self._run = run

    def report(self, *a, **k):
        pass

    def run_isolated(self, fn, *args, on_message=None, **kw):
        return self._run(fn, args, on_message)


def test_stop_mid_batch_reports_an_unreported_rename_but_not_one_left_with_a_marker(
    tmp_path, monkeypatch
):
    a = age(write_plain(tmp_path / "a.mefd"))
    b = age(write_plain(tmp_path / "b.mefd"))
    jobs = [ops.RenameJob("a", str(a), "a2"), ops.RenameJob("b", str(b), "b2")]

    def run(fn, args, on_message):
        done = ops.run_renames(list(args[0]), args[1])  # both really happen ...
        on_message(done[0])  # ... but only the first answer arrives
        raise JobCancelled()

    outcome = tasks.rename_recordings(_Ctx(run), jobs)
    assert outcome.stopped and [r.key for r in outcome.results] == ["a"]
    assert outcome.unreported == (("b", str(tmp_path / "b2.mefd")),)
    # The same, but the second was left half done (its marker is still there).
    monkeypatch.setattr(mef3io, "operation_marker", lambda p: {"id": "x"})
    c = age(write_plain(tmp_path / "c.mefd"))
    outcome = tasks.rename_recordings(
        _Ctx(
            lambda fn, args, on_message: (
                ops.run_renames(list(args[0]), args[1]),
                (_ for _ in ()).throw(JobCancelled()),
            )
        ),
        [ops.RenameJob("c", str(c), "c2")],
    )
    assert outcome.unreported == ()


def test_undo_after_a_stop_undoes_what_was_reported(qtbot, page, three, tmp_path):
    open_all(qtbot, page, three[:2])
    dialog = opened_dialog(qtbot, page)
    jobs = [
        ops.RenameJob(r.entry.key, r.entry.location, f"x{n}") for n, r in enumerate(dialog.rows)
    ]
    first = ops.run_renames(jobs[:1], ops.backup_dir())  # the stop came after the first
    dialog.new_names()  # (rows stay as they were)
    dialog._renamed(tasks.RenameOutcome(tuple(first), True))
    assert "Stopped" in dialog.status.text()
    assert len(dialog.batch.units) == 1
    qtbot.waitUntil(
        lambda: page.entries() and all(not e.busy for e in page.entries()), timeout=60_000
    )
    dialog.confirm = lambda text, more: True
    dialog.start_undo()
    qtbot.waitUntil(lambda: dialog.batch is None, timeout=60_000)
    assert sorted(p.name for p in tmp_path.glob("*.mefd")) == sorted(p.name for p in three)


# -- 5. the undo password is asked for again and never kept ------------------------------------


def test_undo_asks_for_the_password_again_and_does_not_keep_it(qtbot, page, tmp_path):
    locked = write_encrypted(tmp_path / "locked.mefd")
    age(locked)
    open_all(qtbot, page, [locked], password=PASSWORD_BASIC)
    dialog = opened_dialog(qtbot, page)
    dialog.set_new_name(0, "unlocked")
    dialog.confirm = lambda text, more: True
    dialog.start()
    qtbot.waitUntil(lambda: dialog._done, timeout=60_000)
    (unit,) = dialog.batch.units
    assert unit.protected and unit.password == ""
    assert PASSWORD_BASIC not in repr(dialog.batch) and PASSWORD_BASIC not in str(unit)
    settle(qtbot, page)
    asked = []
    # Declined: nothing happens, the batch stays.
    dialog.ask_password = lambda name, wrong: asked.append(name) or ""
    dialog.start_undo()
    qtbot.waitUntil(lambda: bool(asked), timeout=60_000)
    assert (tmp_path / "unlocked.mefd").exists() and dialog.batch
    qtbot.waitUntil(lambda: not dialog.busy, timeout=30_000)
    # Wrong: refused, the batch stays.
    dialog.ask_password = lambda name, wrong: "wrong-password-9"
    dialog.start_undo()
    qtbot.waitUntil(lambda: dialog.status.text().startswith("0 recordings"), timeout=60_000)
    assert (tmp_path / "unlocked.mefd").exists() and dialog.batch
    assert dialog.batch.units[0].password == ""
    # Right: undone.
    dialog.ask_password = lambda name, wrong: PASSWORD_BASIC
    dialog.start_undo()
    qtbot.waitUntil(lambda: dialog.batch is None, timeout=60_000)
    assert locked.exists()


# -- 6. patterns -------------------------------------------------------------------------------


@pytest.mark.parametrize("template", ["{name:>9999}", "{index:99999d}", "{start:%Y.123456}"])
def test_huge_widths_are_refused(template):
    assert rn.name_from_template(template, index=1, old_name="o", start_us=T0) == (
        rn.NameProblem.BAD_TEMPLATE
    )
    assert rn.name_from_template("{name:>999}", index=1, old_name="o", start_us=T0) != (
        rn.NameProblem.BAD_TEMPLATE
    )


def test_running_out_of_memory_in_a_pattern_is_a_bad_pattern(monkeypatch):
    def boom(*a, **k):
        raise MemoryError

    monkeypatch.setattr(rn, "to_datetime", boom)
    assert rn.name_from_template("{start:%Y}", index=1, old_name="o", start_us=T0) == (
        rn.NameProblem.BAD_TEMPLATE
    )


# -- 7. only the capitals change ---------------------------------------------------------------


def test_case_only_rename_on_a_case_insensitive_volume_has_its_own_words(tmp_path, monkeypatch):
    src = age(write_plain(tmp_path / "day.mefd"))

    def refuse(*a, **k):
        raise mef3io.IoError("target exists")

    monkeypatch.setattr(mef3io, "rename_session", refuse)
    (result,) = ops.run_renames([ops.RenameJob("k", str(src), "DAY")], str(tmp_path / "bk"))
    assert result.message.title == "Only the capital letters changed"
    assert "different name first" in result.message.text and src.exists()
    # The batch check lets a case-only change through even when the folder
    # lists the (same) entry under its old capitals, as a case-insensitive volume does.
    item = rn.RenameItem("k", str(src), "day", "DAY", False)
    plan = rn.plan_renames([item], lister=lambda _f: {rn.name_key("day.mefd")}, path_limit=None)
    assert plan.ok


# -- 8. a composed and a decomposed name are the same name -------------------------------------


def test_composed_and_decomposed_names_collide_in_a_batch(tmp_path):
    composed = "café"
    items = [
        rn.RenameItem("a", str(tmp_path / "a.mefd"), "a", composed, False),
        rn.RenameItem(
            "b", str(tmp_path / "b.mefd"), "b", unicodedata.normalize("NFD", composed), False
        ),
    ]
    plan = rn.plan_renames(items, lister=lambda _f: {"a.mefd", "b.mefd"}, path_limit=None)
    assert [r.problems for r in plan.rows] == [(rn.NameProblem.COLLIDES,)] * 2


def test_undo_and_error_details_never_reach_the_log(qtbot, window, tmp_path):
    log_file = setup_logging(tmp_path / "logs", level=logging.DEBUG)
    privacy.identities.clear()
    window._closing = True
    page = window.explorer
    try:
        plain = write_at(tmp_path / "Hidden-Origin-Name.mefd", T0)
        locked = write_encrypted(tmp_path / "Locked-Origin-Name.mefd")
        age(locked)
        open_all(qtbot, page, [plain, locked], password="")
        for e in page.entries():
            if e.name.startswith("Locked"):
                page._unlock_with(e, PASSWORD_BASIC)
        settle(qtbot, page)
        page.table.selectAll()
        dialog = opened_dialog(qtbot, page)
        for n in range(len(dialog.rows)):
            dialog.set_new_name(n, "Fresh-Label-" + str(n))
        # A wrong password makes one of them fail with error details.
        for row in dialog.rows:
            if row.old_name.startswith("Locked"):
                row.entry.password = "wrong-secret-pw-31"
        dialog.confirm = lambda text, more: True
        dialog.start()
        qtbot.waitUntil(lambda: dialog._done, timeout=60_000)
        failed = [r for r in dialog.rows if r.result is not None and not r.result.ok]
        assert failed and "wrong-secret-pw-31" not in failed[0].result.message.detail
        assert "Locked-Origin-Name" not in failed[0].result.message.detail
        settle(qtbot, page)
        dialog.confirm = lambda text, more: True
        dialog.start_undo()
        qtbot.waitUntil(lambda: dialog.batch is None, timeout=60_000)
    finally:
        teardown_logging()
        privacy.identities.clear()
    text = log_file.read_text(encoding="utf-8")
    for secret in (
        "Hidden-Origin-Name",
        "Locked-Origin-Name",
        "Fresh-Label-",
        "wrong-secret-pw-31",
        str(tmp_path),
    ):
        assert secret not in text, secret


# -- 9. notes are scrubbed like messages -------------------------------------------------------


def test_notes_of_a_result_are_scrubbed(tmp_path):
    privacy.identities.clear()
    privacy.register_identity("Very-Secret-Name")
    result = ops.ItemResult("k", "", (), None, False, ("left Very-Secret-Name at 2019-03-14",))
    (clean,) = tasks._scrubbed_results([result])
    assert "Very-Secret-Name" not in clean.notes[0] and "2019-03-14" not in clean.notes[0]
    privacy.identities.clear()


# -- 10. the newest part is the highest number, not the last name ------------------------------


def test_the_newest_part_is_found_by_number(tmp_path):
    rec = tmp_path / "r.mefd" / "ch.timd"
    old, new = rec / "ch-9.segd", rec / "ch-10.segd"  # "ch-9" sorts AFTER "ch-10" by name
    old.mkdir(parents=True)
    new.mkdir()
    (old / "f").write_bytes(b"x")
    (new / "f").write_bytes(b"x")
    for p in (tmp_path / "r.mefd").rglob("*"):
        os.utime(p, (1e9, 1e9))
    assert not ops.recently_written(str(tmp_path / "r.mefd"))
    os.utime(new / "f", None)  # the recorder is writing the newest part
    assert ops.recently_written(str(tmp_path / "r.mefd"))
    assert {rel for rel, _ in ops.newest_files(str(tmp_path / "r.mefd"))} == {
        "ch.timd/ch-10.segd/f"
    }
