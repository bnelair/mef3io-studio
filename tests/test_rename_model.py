"""Rename (#12): the name rules, the batch checks, the time order, and the
real operation on synthetic recordings (mef3io's journaled rename, drift
fix, undo, archives, passwords, an interrupted rename)."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

import mef3io
import numpy as np
import pytest
from explorer_sessions import (
    PASSWORD_BASIC,
    PASSWORD_FULL,
    SECOND,
    T0,
    set_time_offset,
    write_encrypted,
    write_plain,
)

from mef3io_studio.model import rename as rn
from mef3io_studio.model import rename_ops as ops
from mef3io_studio.model.times import RecordingTime, order_by_time, read_time, read_times


def age(path: Path) -> Path:
    """Make every file look old, so the 'written a moment ago' check passes."""
    for p in [path, *Path(path).rglob("*")] if Path(path).is_dir() else [Path(path)]:
        os.utime(p, (1e9, 1e9))
    return Path(path)


def _item(old, new, folder, packed=False, key=None):
    suffix = ".mefd.tar" if packed else ".mefd"
    return rn.RenameItem(key or old, str(folder / (old + suffix)), old, new, packed)


# -- the rules ------------------------------------------------------------------------


def test_the_length_limits_are_250_and_246_bytes():
    assert rn.check_name("a" * 250) == []
    assert rn.check_name("a" * 251) == [rn.NameProblem.TOO_LONG]
    assert rn.check_name("a" * 246, packed=True) == []
    assert rn.check_name("a" * 247, packed=True) == [rn.NameProblem.TOO_LONG]
    # Bytes, not letters: 126 two-byte letters are 252 bytes.
    assert rn.check_name("é" * 126, decomposes=False) == [rn.NameProblem.TOO_LONG]
    assert rn.check_name("é" * 125, decomposes=False) == []
    # A volume that stores accents decomposed (macOS): e + accent is 3 bytes.
    precomposed = unicodedata.normalize("NFC", "é" * 100)
    assert rn.check_name(precomposed, decomposes=False) == []
    assert rn.check_name(precomposed, decomposes=True) == [rn.NameProblem.TOO_LONG]


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        ("", rn.NameProblem.EMPTY),
        ("a/b", rn.NameProblem.ILLEGAL),
        ('a"b', rn.NameProblem.ILLEGAL),
        ("a:b", rn.NameProblem.ILLEGAL),
        ("a\x07b", rn.NameProblem.CONTROL),
        ("CON", rn.NameProblem.RESERVED),
        ("nul.x", rn.NameProblem.RESERVED),
        ("com1", rn.NameProblem.RESERVED),
        ("name.", rn.NameProblem.ENDS_WITH),
        ("name ", rn.NameProblem.ENDS_WITH),
    ],
)
def test_names_that_are_not_allowed(name, problem):
    assert problem in rn.check_name(name)
    assert rn.problem_text(problem)  # everyday words for every one


def test_every_problem_has_words_without_jargon():
    for problem in rn.NameProblem:
        text = rn.problem_text(problem)
        assert text and "mef3io" not in text.lower() and "exception" not in text.lower()


def test_a_typed_ending_is_not_doubled():
    assert rn.clean_name("  day1.mefd ") == "day1"
    assert rn.clean_name("day1.MEFD.tar", packed=True) == "day1"
    assert rn.clean_name("day1.mefd.tar", packed=False) == "day1.mefd.tar"
    assert rn.clean_name(".mefd") == ".mefd"  # nothing left: leave it for the check


# -- the batch --------------------------------------------------------------------------


def _plan(items, existing=()):
    keys = {rn.name_key(n) for n in existing}
    return rn.plan_renames(items, lister=lambda _f: keys, path_limit=None, decomposes=False)


def test_a_name_taken_in_the_folder_is_refused_without_regard_to_case(tmp_path):
    plan = _plan([_item("a", "Day1", tmp_path)], existing=["a.mefd", "day1.mefd", "day1.mefd.tar"])
    assert plan.rows[0].problems == (rn.NameProblem.TAKEN,)
    assert not plan.ok
    # The same letter typed two ways (composed and decomposed) is the same name.
    composed = "café"
    plan = _plan(
        [_item("a", unicodedata.normalize("NFD", composed), tmp_path)],
        existing=["a.mefd", composed + ".mefd"],
    )
    assert plan.rows[0].problems == (rn.NameProblem.TAKEN,)


def test_two_recordings_cannot_get_the_same_name(tmp_path):
    plan = _plan(
        [_item("a", "x", tmp_path), _item("b", "X", tmp_path)], existing=["a.mefd", "b.mefd"]
    )
    assert [r.problems for r in plan.rows] == [(rn.NameProblem.COLLIDES,)] * 2
    assert plan.order == ()


def test_a_chain_is_ordered_and_a_swap_is_refused(tmp_path):
    existing = ["a.mefd", "b.mefd"]
    chain = _plan([_item("a", "b", tmp_path), _item("b", "c", tmp_path)], existing=existing)
    assert chain.ok and chain.order == (1, 0)  # b goes first, then a takes its name
    swap = _plan([_item("a", "b", tmp_path), _item("b", "a", tmp_path)], existing=existing)
    assert [r.problems for r in swap.rows] == [(rn.NameProblem.CYCLE,)] * 2
    assert swap.order == ()
    # A chain behind something that is refused cannot go either.
    blocked = _plan([_item("a", "b", tmp_path), _item("b", "c" * 300, tmp_path)], existing=existing)
    assert rn.NameProblem.TAKEN in blocked.rows[0].problems


def test_a_case_only_change_is_allowed_and_the_same_name_is_not(tmp_path):
    assert _plan([_item("day", "DAY", tmp_path)], existing=["day.mefd"]).ok
    assert _plan([_item("day", "day", tmp_path)], existing=["day.mefd"]).rows[0].problems == (
        rn.NameProblem.SAME,
    )


def test_a_too_long_path_is_refused_on_windows(tmp_path):
    item = rn.RenameItem("k", str(tmp_path / "a.mefd"), "a", "n" * 100, False, deepest_inside=80)
    long_plan = rn.plan_renames(
        [item], lister=lambda _f: set(), path_limit=len(str(tmp_path)) + 150
    )
    assert long_plan.rows[0].problems == (rn.NameProblem.PATH_TOO_LONG,)
    short = rn.plan_renames([item], lister=lambda _f: set(), path_limit=None)
    assert short.ok


def test_blocked_reasons_are_plain():
    assert rn.blocked_reason(rn.RowState()) == ""
    for state in (
        rn.RowState(in_use=True),
        rn.RowState(needs_password=True),
        rn.RowState(recently_written=True),
        rn.RowState(unreadable="Could not be read."),
    ):
        assert rn.blocked_reason(state)
    # An out-of-step name is fixed first, it does not block.
    assert rn.blocked_reason(rn.RowState(drift=True)) == ""


# -- names from a pattern ------------------------------------------------------------------


def test_names_from_a_pattern():
    start = T0 + 5 * SECOND
    made = rn.name_from_template(
        "{index:03d}_{start:%Y%m%d-%H%M%S}", index=7, old_name="old", start_us=start
    )
    assert made == "007_20000101-000005"
    assert rn.name_from_template("{name}-x", index=1, old_name="old", start_us=None) == "old-x"
    # The site time zone, never the file's own offset.
    from zoneinfo import ZoneInfo

    local = rn.name_from_template(
        "{start:%H}", index=1, old_name="o", start_us=T0, zone=ZoneInfo("Europe/Prague")
    )
    assert local == "01"
    # An unconfirmed start (or order) cannot name anything from it.
    assert rn.name_from_template("{start:%Y}", index=1, old_name="o", start_us=None) == (
        rn.NameProblem.NO_START
    )
    assert rn.name_from_template("{index}", index=None, old_name="o", start_us=T0) == (
        rn.NameProblem.NO_START
    )


@pytest.mark.parametrize(
    "template", ["", "{other}", "{start.__class__}", "{name!r}", "{start:{index}}", "{", "{0}"]
)
def test_patterns_outside_the_allowed_forms_are_refused(template):
    assert rn.name_from_template(template, index=1, old_name="o", start_us=T0) == (
        rn.NameProblem.BAD_TEMPLATE
    )


# -- the time order ------------------------------------------------------------------------


def test_unverified_times_stay_out_of_the_absolute_order():
    times = {
        "late": RecordingTime(T0 + 10 * SECOND, T0 + 20 * SECOND, True),
        "locked": RecordingTime(T0 - 99 * SECOND, T0, False),  # would sort first if trusted
        "early": RecordingTime(T0, T0 + SECOND, True),
        "empty": RecordingTime(None, None, True),
        "failed": None,
    }
    ordered, unverified = order_by_time(times)
    assert ordered == ["early", "late"]
    assert unverified == ["locked", "empty", "failed"]


def test_times_come_from_the_shared_helper(tmp_path):
    plain = age(write_plain(tmp_path / "p.mefd"))
    locked = write_encrypted(tmp_path / "e.mefd")
    shifted = set_time_offset(write_plain(tmp_path / "s.mefd"), 5 * SECOND)
    got = read_times(
        [
            ("p", str(plain), ""),
            ("e", str(locked), ""),
            ("e2", str(locked), PASSWORD_FULL),
            ("bad", str(locked), "wrong-password"),
            ("s", str(shifted), ""),
        ]
    )
    assert got["p"].verified and got["p"].start == T0
    assert got["e"].verified is False  # protected, no password: not confirmed
    assert got["e2"].verified and got["e2"].start == T0
    assert isinstance(got["bad"], mef3io.PasswordError)
    assert read_time(str(plain)).end > read_time(str(plain)).start
    ordered, unverified = order_by_time({k: v for k, v in got.items() if k != "bad"})
    assert "e" in unverified and "p" in ordered
    # Join or trim reads its times through the same helper.
    from mef3io_studio.model import join

    assert join.read_time is read_time


# -- the operation on real recordings ----------------------------------------------------------


def _names(path):
    drift = mef3io.name_drift(path)
    return drift.folder, set(drift.stored_names), drift.record_files, drift.consistent


def test_a_rename_changes_every_place_and_keeps_notes_times_and_samples(tmp_path):
    src = age(write_plain(tmp_path / "old.mefd"))
    before = mef3io.Reader(str(src))
    samples = before.read("a1", T0, T0 + 5 * SECOND)
    notes_before = len(before.records())
    del before
    folder = str(tmp_path / "bk")
    (result,) = ops.run_renames([ops.RenameJob("k", str(src), "new-name")], folder)
    assert result.ok and not src.exists()
    new = tmp_path / "new-name.mefd"
    assert result.new_location == str(new)
    assert _names(new) == ("new-name", {"new-name"}, ("new-name",), True)
    assert (new / "new-name.rdat").exists() and (new / "new-name.ridx").exists()
    after = mef3io.Reader(str(new))
    assert len(after.records()) == notes_before  # the session notes are still found
    assert np.array_equal(after.read("a1", T0, T0 + 5 * SECOND), samples, equal_nan=True)
    assert read_time(str(new)).start == T0
    assert [s.kind for s in result.steps] == ["rename"]


def test_undo_puts_the_name_back_and_keeps_what_was_added_since(tmp_path):
    src = age(write_plain(tmp_path / "old.mefd"))
    folder = str(tmp_path / "bk")
    (result,) = ops.run_renames([ops.RenameJob("k", str(src), "new")], folder)
    new = tmp_path / "new.mefd"
    with mef3io.Writer(str(new), overwrite=False) as w:
        w.write_annotations([{"time": T0 + 3 * SECOND, "text": "added later"}])
    notes = len(mef3io.Reader(str(new)).records())
    undone = ops.undo_steps(result.steps, "", folder)
    assert undone.ok and undone.new_location == str(src)
    assert _names(src) == ("old", {"old"}, ("old",), True)
    assert len(mef3io.Reader(str(src)).records()) == notes  # the later note stays


def test_drift_is_seen_and_fixed_before_the_rename_and_undone_in_order(tmp_path):
    src = age(write_plain(tmp_path / "old.mefd"))
    os.rename(src, tmp_path / "other.mefd")  # another program renamed only the folder
    other = tmp_path / "other.mefd"
    state, folders = ops.read_state([ops.StateRequest("k", str(other), False)])
    assert state["k"].drift and not state["k"].unfinished
    assert rn.blocked_reason(state["k"]) == ""
    assert rn.name_key("other.mefd") in folders[str(tmp_path)]
    folder = str(tmp_path / "bk")
    (result,) = ops.run_renames(
        [ops.RenameJob("k", str(other), "final", fix_drift_first=True)], folder
    )
    assert result.ok and [s.kind for s in result.steps] == ["drift", "rename"]
    assert _names(tmp_path / "final.mefd") == ("final", {"final"}, ("final",), True)
    undone = ops.undo_steps(result.steps, "", folder)
    assert undone.ok
    # Back to the folder's name; the stored name is as it was before the fix.
    assert (tmp_path / "other.mefd").exists()
    folder_name, stored, _records, consistent = _names(tmp_path / "other.mefd")
    assert folder_name == "other" and stored == {"old"} and not consistent
    # Fixing only (the name stays): one step.
    (only,) = ops.run_renames([ops.RenameJob("k", str(other), "", settle_only=True)], folder)
    assert only.ok and [s.kind for s in only.steps] == ["drift"]
    assert _names(other)[3]


def test_a_drifted_recording_is_refused_by_a_plain_rename_with_plain_words(tmp_path):
    src = age(write_plain(tmp_path / "old.mefd"))
    os.rename(src, tmp_path / "other.mefd")
    (result,) = ops.run_renames(
        [ops.RenameJob("k", str(tmp_path / "other.mefd"), "final")], str(tmp_path / "bk")
    )
    assert not result.ok
    assert (tmp_path / "other.mefd").exists() and not (tmp_path / "final.mefd").exists()
    assert "Nothing was renamed" in result.message.text
    assert "does not match" in result.message.title
    assert "final" not in result.message.text  # no names in the words


def test_a_protected_recording_needs_its_password(tmp_path):
    src = age(write_encrypted(tmp_path / "e.mefd"))
    folder = str(tmp_path / "bk")
    (refused,) = ops.run_renames([ops.RenameJob("k", str(src), "e2")], folder)
    assert not refused.ok and src.exists() and not (tmp_path / "e2.mefd").exists()
    assert "password" in refused.message.text.lower()
    (wrong,) = ops.run_renames([ops.RenameJob("k", str(src), "e2", "not-it")], folder)
    assert not wrong.ok and src.exists()
    # The basic password is enough.
    (done,) = ops.run_renames([ops.RenameJob("k", str(src), "e2", PASSWORD_BASIC)], folder)
    assert done.ok and (tmp_path / "e2.mefd").exists()
    # Undo asks for the password again.
    bad = ops.undo_steps(done.steps, "", folder)
    assert not bad.ok and (tmp_path / "e2.mefd").exists()
    good = ops.undo_steps(done.steps, PASSWORD_BASIC, folder)
    assert good.ok and src.exists()


def test_an_archive_is_renamed_as_a_file_and_still_carries_the_old_name_inside(tmp_path):
    src = age(write_plain(tmp_path / "old.mefd"))
    tar = tmp_path / "old.mefd.tar"
    mef3io.archive_session(str(src), str(tar), False)
    age(tar)
    state, _ = ops.read_state([ops.StateRequest("k", str(tar), True)])
    assert not state["k"].drift and not state["k"].recently_written
    (result,) = ops.run_renames([ops.RenameJob("k", str(tar), "new", packed=True)], str(tmp_path))
    assert result.ok and (tmp_path / "new.mefd.tar").exists() and not tar.exists()
    inside = mef3io.name_drift(tmp_path / "new.mefd.tar")
    assert inside.folder == "old" and set(inside.stored_names) == {"old"}  # the inside is untouched
    assert "old name" in rn.ARCHIVE_NOTE
    undone = ops.undo_steps(result.steps, "", str(tmp_path))
    assert undone.ok and tar.exists()
    # An over-long archive name is refused by the rules before it gets here.
    assert rn.check_name("n" * 247, packed=True) == [rn.NameProblem.TOO_LONG]


def test_a_rename_left_unfinished_is_put_back_by_the_next_one(tmp_path):
    src = age(write_plain(tmp_path / "old.mefd"))
    folder = ops.backup_dir()
    with pytest.raises(mef3io.IoError):
        mef3io.rename_session(str(src), "new", backup_dir=folder, _crash_at=3)
    left = next(p for p in tmp_path.iterdir() if p.suffix == ".mefd")
    state, _ = ops.read_state([ops.StateRequest("k", str(left), False)])
    assert state["k"].unfinished and not state["k"].drift
    words = rn.UNFINISHED_NOTE
    assert "Nothing is lost" in words
    (result,) = ops.run_renames([ops.RenameJob("k", str(left), "", settle_only=True)], folder)
    assert result.ok and result.put_back
    assert _names(tmp_path / "old.mefd") == ("old", {"old"}, ("old",), True)
    assert mef3io.operation_marker(str(tmp_path / "old.mefd")) is None


def test_a_recording_written_a_moment_ago_is_not_renamed(tmp_path):
    src = write_plain(tmp_path / "fresh.mefd")
    state, _ = ops.read_state([ops.StateRequest("k", str(src), False)])
    assert state["k"].recently_written
    assert rn.blocked_reason(state["k"])
    age(src)
    state, _ = ops.read_state([ops.StateRequest("k", str(src), False)])
    assert not state["k"].recently_written
    # Files our own rename just left do not count.
    (done,) = ops.run_renames([ops.RenameJob("k", str(src), "n2")], str(tmp_path / "bk"))
    skip = dict(done.steps[0].report.files)
    new = tmp_path / "n2.mefd"
    fresh = ops.recently_written(str(new))
    assert fresh and not ops.recently_written(str(new), skip=skip)


def test_the_recovery_notes_go_to_the_app_data_folder_not_beside_the_recording(tmp_path):
    folder = Path(ops.backup_dir())
    assert folder.name == ops.BACKUP_FOLDER
    assert Path(os.environ["MEF3IO_STUDIO_DATA_DIR"]) in folder.parents
    src = age(write_plain(tmp_path / "old.mefd"))
    ops.run_renames([ops.RenameJob("k", str(src), "new")], str(folder))
    assert not any(".repair-backup" in p.name for p in tmp_path.iterdir())
    assert list(folder.iterdir()) == []  # mef3io removed its notes when it finished


def test_a_stop_between_recordings_keeps_what_was_done(tmp_path):
    from mef3io_studio.model.cancel import CancelToken

    a = age(write_plain(tmp_path / "a.mefd"))
    b = age(write_plain(tmp_path / "b.mefd"))
    token = CancelToken()
    seen = []

    def send(item):
        seen.append(item)
        token.cancel()  # Stop pressed after the first

    results = ops.run_renames(
        [ops.RenameJob("a", str(a), "a2"), ops.RenameJob("b", str(b), "b2")],
        str(tmp_path / "bk"),
        cancel=token,
        send=send,
    )
    assert len(results) == 1 and results[0].ok and len(seen) == 1
    assert (tmp_path / "a2.mefd").exists() and b.exists()


def test_the_longest_path_inside_is_found_without_knowing_the_format(tmp_path):
    src = age(write_plain(tmp_path / "deep.mefd"))
    longest = max(len(p.relative_to(src).as_posix()) for p in src.rglob("*") if p.is_file())
    assert ops.longest_inside(str(src)) == longest
    state, _ = ops.read_state([ops.StateRequest("k", str(src), False)])
    assert state["k"].deepest_inside == longest
    # Windows: a path that would pass the limit with the new name is refused.
    item = rn.RenameItem("k", str(src), "deep", "x" * 60, False, state["k"].deepest_inside)
    limit = len(str(tmp_path)) + 1 + 60 + len(".mefd") + 1 + longest
    refused = rn.plan_renames([item], lister=lambda _f: set(), path_limit=limit)
    assert refused.rows[0].problems == (rn.NameProblem.PATH_TOO_LONG,)
    assert rn.plan_renames([item], lister=lambda _f: set(), path_limit=limit + 1).ok
