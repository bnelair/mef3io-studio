"""Join or trim recordings (model/join.py): the kept parts, the checks before
anything is written, and real runs on small recordings written by mef3io
(made-up signals; every sample value encodes its own time, so a read of the
result says exactly which samples it holds)."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import mef3io
import numpy as np
import pytest
from explorer_sessions import PASSWORD_BASIC, PASSWORD_FULL, metadata

from mef3io_studio.model import join as j
from mef3io_studio.model.cancel import CancelToken, JobCancelled

T0 = 946_684_800_000_000  # 2000-01-01 00:00:00 UTC
S = 1_000_000
MINUTE = 60 * S
HOUR = 60 * MINUTE
FS = 2.0  # two samples a second: hours of signal stay tiny


def write(path: Path, start: int, seconds: int, channels=("a1", "a2"), fs=FS, password=None):
    """A recording whose sample values are their own time in seconds since
    T0 (so any kept part can be predicted exactly)."""
    n = int(seconds * fs)
    values = (start - T0) / S + np.arange(n) / fs
    kwargs = {}
    if password:
        kwargs = {"password1": PASSWORD_BASIC, "password2": PASSWORD_FULL}
    with mef3io.Writer(str(path), overwrite=True, metadata=metadata(), **kwargs) as w:
        for name in channels:
            w.write(name, values, start, fs)
    return path


def expected(start: int, end: int, fs=FS) -> np.ndarray:
    """The values of the samples in ``[start, end)``."""
    first = (start - T0) / S
    n = round((end - start) / S * fs)
    return first + np.arange(n) / fs


def kept_values(path: Path, channel="a1", password="") -> np.ndarray:
    reader = mef3io.Reader(str(path), password=password) if password else mef3io.Reader(str(path))
    data = np.asarray(reader.read(channel), dtype=float)
    return data[~np.isnan(data)]


def source(path: Path, keep: j.Keep, password: str = "") -> j.JoinSource:
    return j.JoinSource(str(path), str(path), keep, password)


def no_leftovers(folder: Path) -> list[str]:
    return [p for p in os.listdir(folder) if p.startswith(j.PARTS_PREFIX) or p.endswith(".partial")]


# --------------------------------------------------------------------------
# What each choice keeps (no files)


@pytest.mark.parametrize(
    ("keep", "want"),
    [
        (j.Keep(j.KEEP_WHOLE), (0, 100, True, True)),
        (j.Keep(j.KEEP_FIRST, 30), (0, 30, False, False)),
        (j.Keep(j.KEEP_LAST, 30), (70, 100, False, True)),
        (j.Keep(j.KEEP_FIRST, 100), (0, 100, True, True)),  # all of it
        (j.Keep(j.KEEP_LAST, 500), (0, 100, True, True)),
        (j.Keep(j.KEEP_BETWEEN, start_us=10, end_us=20), (10, 20, False, False)),
        (j.Keep(j.KEEP_BETWEEN, start_us=-50, end_us=20), (0, 20, False, False)),
        (j.Keep(j.KEEP_BETWEEN, start_us=90, end_us=500), (90, 100, False, True)),
        (j.Keep(j.KEEP_BETWEEN, start_us=-5, end_us=500), (0, 100, True, True)),
    ],
)
def test_each_choice_keeps_the_right_part(keep, want):
    span = j.resolve_keep(keep, 0, 100)
    assert (span.start, span.end, span.whole, span.to_end) == want


@pytest.mark.parametrize(
    ("keep", "code"),
    [
        (j.Keep(j.KEEP_FIRST, 0), j.DURATION_NOT_POSITIVE),
        (j.Keep(j.KEEP_LAST, -5), j.DURATION_NOT_POSITIVE),
        (j.Keep(j.KEEP_BETWEEN, start_us=20, end_us=20), j.END_NOT_AFTER_START),
        (j.Keep(j.KEEP_BETWEEN, start_us=200, end_us=300), j.OUTSIDE_RECORDING),
        (j.Keep(j.KEEP_BETWEEN), j.TIMES_MISSING),
        (j.Keep("sideways"), j.UNKNOWN_CHOICE),
    ],
)
def test_choices_that_keep_nothing_are_refused(keep, code):
    assert j.resolve_keep(keep, 0, 100) == code


def test_a_window_reaching_the_end_keeps_the_last_sample():
    span = j.resolve_keep(j.Keep(j.KEEP_LAST, 30), 0, 100)
    assert span.time_range == (70, 101)
    assert j.resolve_keep(j.Keep(j.KEEP_FIRST, 30), 0, 100).time_range == (0, 30)


def test_parts_are_put_in_time_order_and_overlaps_refused():
    a = j.JoinSource("a", "a.mefd", j.Keep(j.KEEP_LAST, 10))
    b = j.JoinSource("b", "b.mefd", j.Keep(j.KEEP_FIRST, 10))
    c = j.JoinSource("c", "c.mefd", j.Keep(j.KEEP_WHOLE))
    ranges = {"a": (0, 100), "b": (150, 300), "c": (100, 120)}
    plan = j.plan_join([b, c, a], ranges)
    assert plan.ok and plan.order == ("a", "c", "b")
    # c now covers a's kept part: refused, naming both.
    ranges["c"] = (50, 120)
    plan = j.plan_join([b, c, a], ranges)
    assert not plan.ok and plan.overlapping == {"a", "c"}
    text = j.problem_text(plan.problems[0], {"a": "Alpha", "c": "Gamma"})
    assert "Alpha" in text and "Gamma" in text and "overlap" in text
    # Overlap across a part in between is found too.
    ranges = {"a": (0, 100), "b": (95, 300), "c": (100, 120)}
    plan = j.plan_join([j.JoinSource("a", "a", j.Keep()), b, c], ranges)
    assert {p.keys for p in plan.problems} >= {("a", "b")}


def test_one_recording_kept_whole_needs_a_change():
    whole = [j.JoinSource("a", "a.mefd", j.Keep())]
    plan = j.plan_join(whole, {"a": (0, 100)}, changes=False)
    assert [p.code for p in plan.problems] == [j.NOTHING_TO_CHANGE]
    assert j.plan_join(whole, {"a": (0, 100)}, changes=True).ok
    assert j.plan_join([], {}).problems[0].code == j.NO_RECORDINGS
    assert j.plan_join(whole, {"a": (None, None)}).problems[0].code == j.TIMES_UNKNOWN


def test_the_request_hides_paths_and_passwords():
    request = j.new_request(
        [j.JoinSource("k", "/secret/Jane.mefd", j.Keep(), "pw-1")],
        "/out/Jane-part.mefd",
        output_password=("x1", "x2"),
    )
    text = repr(request)
    assert "secret" not in text and "Jane" not in text and "pw-1" not in text and "x2" not in text
    assert os.path.basename(request.parts_folder).startswith(j.PARTS_PREFIX)
    assert os.path.dirname(request.parts_folder) == os.path.abspath("/out")


# --------------------------------------------------------------------------
# Real runs


@pytest.fixture
def two(tmp_path):
    """A: two hours; a gap of one hour; B: two hours."""
    a = write(tmp_path / "A.mefd", T0, 2 * 3600)
    b = write(tmp_path / "B.mefd", T0 + 3 * HOUR, 2 * 3600)
    return tmp_path, a, b


def test_last_hour_of_one_and_first_hour_of_the_next(two):
    tmp, a, b = two
    out = tmp / "out"
    out.mkdir()
    request = j.new_request(
        # Given in the "wrong" order: the run orders them by time.
        [source(b, j.Keep(j.KEEP_FIRST, HOUR)), source(a, j.Keep(j.KEEP_LAST, HOUR))],
        str(out / "joined.mefd"),
    )
    assert j.check_join(request) == []
    progress: list[j.JoinProgress] = []
    result = j.make_joined(request, send=progress.append)
    assert result.ok, result.message
    want = np.concatenate(
        [expected(T0 + HOUR, T0 + 2 * HOUR), expected(T0 + 3 * HOUR, T0 + 4 * HOUR)]
    )
    for channel in ("a1", "a2"):
        np.testing.assert_array_equal(kept_values(out / "joined.mefd", channel), want)
    found = mef3io.time_range(str(out / "joined.mefd"))
    assert found.start_uutc == T0 + HOUR
    assert found.end_uutc == T0 + 4 * HOUR
    # Two cuts, then the join; the bar only goes forward.
    assert {p.kind for p in progress} == {"cut", "join"}
    overall = [p.overall for p in progress]
    assert overall == sorted(overall) and overall[-1] == 1.0
    assert no_leftovers(out) == []
    assert sorted(os.listdir(tmp)) == ["A.mefd", "B.mefd", "out"]


def test_three_recordings_trimmed_at_both_ends(tmp_path):
    a = write(tmp_path / "A.mefd", T0, 3600)
    b = write(tmp_path / "B.mefd", T0 + 2 * HOUR, 3600)
    c = write(tmp_path / "C.mefd", T0 + 4 * HOUR, 3600)
    request = j.new_request(
        [
            source(a, j.Keep(j.KEEP_LAST, 30 * MINUTE)),
            source(
                b,
                j.Keep(
                    j.KEEP_BETWEEN,
                    start_us=T0 + 2 * HOUR + 10 * MINUTE,
                    end_us=T0 + 3 * HOUR - 10 * MINUTE,
                ),
            ),
            source(c, j.Keep(j.KEEP_FIRST, 20 * MINUTE)),
        ],
        str(tmp_path / "three.mefd"),
    )
    result = j.make_joined(request)
    assert result.ok, result.message
    want = np.concatenate(
        [
            expected(T0 + 30 * MINUTE, T0 + HOUR),
            expected(T0 + 2 * HOUR + 10 * MINUTE, T0 + 3 * HOUR - 10 * MINUTE),
            expected(T0 + 4 * HOUR, T0 + 4 * HOUR + 20 * MINUTE),
        ]
    )
    np.testing.assert_array_equal(kept_values(tmp_path / "three.mefd"), want)
    assert no_leftovers(tmp_path) == []


def test_a_recording_kept_whole_is_joined_as_it_is(two, monkeypatch):
    tmp, a, b = two
    cuts = []
    real = mef3io.transform_session
    monkeypatch.setattr(
        mef3io, "transform_session", lambda src, *a_, **k: cuts.append(src) or real(src, *a_, **k)
    )
    request = j.new_request(
        [source(a, j.Keep()), source(b, j.Keep(j.KEEP_FIRST, 30 * MINUTE))],
        str(tmp / "j.mefd"),
    )
    assert j.make_joined(request).ok
    assert cuts == [str(b)]
    want = np.concatenate([expected(T0, T0 + 2 * HOUR), expected(T0 + 3 * HOUR, T0 + 3.5 * HOUR)])
    np.testing.assert_array_equal(kept_values(tmp / "j.mefd"), want)


def test_a_single_recording_is_trimmed_straight_into_the_new_one(two, monkeypatch):
    tmp, a, _b = two
    merges = []
    monkeypatch.setattr(mef3io, "merge_sessions", lambda *x, **k: merges.append(x))
    keep = j.Keep(j.KEEP_BETWEEN, start_us=T0 + 15 * MINUTE, end_us=T0 + 45 * MINUTE)
    request = j.new_request([source(a, keep)], str(tmp / "part.mefd"), exclude_channels=("a2",))
    assert j.check_join(request) == []
    result = j.make_joined(request)
    assert result.ok and not merges
    assert result.channels_kept == ("a1",) and result.channels_excluded == ("a2",)
    np.testing.assert_array_equal(
        kept_values(tmp / "part.mefd"), expected(T0 + 15 * MINUTE, T0 + 45 * MINUTE)
    )
    assert mef3io.Reader(str(tmp / "part.mefd")).channels == ["a1"]
    assert no_leftovers(tmp) == []


def test_one_recording_kept_whole_without_a_change_is_refused(two):
    tmp, a, _b = two
    request = j.new_request([source(a, j.Keep())], str(tmp / "copy.mefd"))
    findings = j.check_join(request)
    assert findings and "Nothing would change" in findings[0].message.text
    assert not j.make_joined(request).ok
    assert not (tmp / "copy.mefd").exists()


def test_overlapping_parts_are_refused_before_anything_is_written(tmp_path):
    a = write(tmp_path / "A.mefd", T0, 2 * 3600)
    b = write(tmp_path / "B.mefd", T0 + HOUR, 2 * 3600)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    request = j.new_request(
        [source(a, j.Keep(j.KEEP_LAST, 90 * MINUTE)), source(b, j.Keep(j.KEEP_FIRST, HOUR))],
        str(tmp_path / "j.mefd"),
    )
    findings = j.check_join(request)
    assert len(findings) == 1 and findings[0].key is None
    assert "overlap" in findings[0].message.text
    result = j.make_joined(request)
    assert not result.ok and "overlap" in result.message.text
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
    # Parts of overlapping recordings that do not overlap are fine.
    fine = j.new_request(
        [source(a, j.Keep(j.KEEP_FIRST, 30 * MINUTE)), source(b, j.Keep(j.KEEP_LAST, HOUR))],
        str(tmp_path / "j.mefd"),
    )
    assert j.check_join(fine) == []
    assert j.make_joined(fine).ok


def test_a_refusal_after_the_cuts_leaves_nothing_behind(tmp_path):
    # Different sampling rates: only the join itself (after the cut) can
    # refuse it with the real parts, but the up-front check finds it too.
    a = write(tmp_path / "A.mefd", T0, 3600)
    b = write(tmp_path / "B.mefd", T0 + 2 * HOUR, 3600, fs=4.0)
    request = j.new_request(
        [source(a, j.Keep(j.KEEP_LAST, 10 * MINUTE)), source(b, j.Keep(j.KEEP_FIRST, MINUTE))],
        str(tmp_path / "j.mefd"),
    )
    findings = j.check_join(request)
    assert [f.message.title for f in findings] == ["The sampling rates do not match"]
    result = j.make_joined(request)
    assert not result.ok and result.message.title == "The sampling rates do not match"
    assert "original recordings were not changed" in result.message.text
    assert str(tmp_path) not in result.message.detail and "A.mefd" not in result.message.detail
    assert not (tmp_path / "j.mefd").exists() and no_leftovers(tmp_path) == []


def test_stop_during_a_cut_removes_the_temporary_parts(two):
    tmp, a, b = two
    request = j.new_request(
        [source(a, j.Keep(j.KEEP_LAST, HOUR)), source(b, j.Keep(j.KEEP_FIRST, HOUR))],
        str(tmp / "j.mefd"),
    )
    token = CancelToken()
    seen = []

    def send(progress):
        seen.append(progress)
        if progress.kind == "cut":
            assert os.path.isdir(request.parts_folder)
            token.cancel()

    with pytest.raises(JobCancelled):
        j.make_joined(request, send=send, cancel=token)
    assert seen
    assert not os.path.lexists(request.parts_folder)
    assert not (tmp / "j.mefd").exists() and no_leftovers(tmp) == []


def test_tidy_removes_what_an_ended_helper_left(two):
    tmp, a, b = two
    request = j.new_request([source(a, j.Keep()), source(b, j.Keep())], str(tmp / "j.mefd"))
    j.make_parts_folder(request)
    os.makedirs(os.path.join(request.parts_folder, "part-1.mefd"))
    os.makedirs(request.destination + ".partial")
    assert j.tidy(request)
    assert no_leftovers(tmp) == []
    # Never anything that is not the run's own parts folder.
    other = j.JoinRequest(request.sources, request.destination, parts_folder=str(a))
    assert j.remove_parts(other) and a.exists()


def test_a_folder_that_was_already_there_is_never_removed(two):
    """Only a parts folder carrying THIS run's marker is removed: one that
    already existed under the name (or another run's) is left as it is,
    also when the run fails because of it."""
    tmp, a, b = two
    request = j.new_request(
        [source(a, j.Keep(j.KEEP_LAST, MINUTE)), source(b, j.Keep())], str(tmp / "j.mefd")
    )
    os.makedirs(request.parts_folder)
    (Path(request.parts_folder) / "user.txt").write_text("keep me")
    result = j.make_joined(request)
    assert not result.ok and not result.parts_left
    assert (Path(request.parts_folder) / "user.txt").read_text() == "keep me"
    assert j.tidy(request) and os.path.isdir(request.parts_folder)
    # Another run's marker is not this run's.
    (Path(request.parts_folder) / j.PARTS_MARKER).write_text("someone-else")
    assert j.remove_parts(request) and os.path.isdir(request.parts_folder)
    assert not (tmp / "j.mefd").exists()


def test_temporary_parts_are_password_protected(two, monkeypatch):
    """A cut copy sits on disk while the join runs: it is protected with
    throw-away passwords, so it never holds readable patient details."""
    tmp, a, b = two
    calls = []
    real = mef3io.transform_session

    def spy(src, dst, **kwargs):
        report = real(src, dst, **kwargs)
        calls.append((kwargs.get("output_password"), not mef3io.time_range(dst).offset_known))
        return report

    monkeypatch.setattr(mef3io, "transform_session", spy)
    request = j.new_request(
        [source(a, j.Keep(j.KEEP_LAST, HOUR)), source(b, j.Keep(j.KEEP_FIRST, HOUR))],
        str(tmp / "j.mefd"),
    )
    assert j.make_joined(request).ok
    assert len(calls) == 2
    for passwords, protected in calls:
        assert passwords is not None and passwords[0] != passwords[1] and protected
    assert len({c[0] for c in calls}) == 1  # one pair for the run


def test_password_protected_originals_and_a_protected_result(tmp_path):
    a = write(tmp_path / "A.mefd", T0, 3600, password=True)
    b = write(tmp_path / "B.mefd", T0 + 2 * HOUR, 3600)
    sources = [
        source(a, j.Keep(j.KEEP_LAST, 10 * MINUTE), PASSWORD_FULL),
        source(b, j.Keep(j.KEEP_FIRST, 10 * MINUTE)),
    ]
    request = j.new_request(sources, str(tmp_path / "j.mefd"), output_password=("new-1", "new-2"))
    assert j.check_join(request) == []
    result = j.make_joined(request)
    assert result.ok, result.message
    want = np.concatenate(
        [
            expected(T0 + 50 * MINUTE, T0 + HOUR),
            expected(T0 + 2 * HOUR, T0 + 2 * HOUR + 10 * MINUTE),
        ]
    )
    np.testing.assert_array_equal(kept_values(tmp_path / "j.mefd", password="new-2"), want)
    with pytest.raises(mef3io.PasswordError):
        mef3io.time_range(str(tmp_path / "j.mefd"), "wrong-password")
    # Without the original's password, nothing is written.
    locked = j.new_request(
        [source(a, j.Keep(j.KEEP_LAST, 10 * MINUTE)), sources[1]], str(tmp_path / "k.mefd")
    )
    findings = j.check_join(locked)
    assert findings and "password" in findings[0].message.title.lower()
    assert not j.make_joined(locked).ok and not (tmp_path / "k.mefd").exists()


def test_channels_are_left_out_of_the_result(two):
    tmp, a, b = two
    request = j.new_request(
        [source(a, j.Keep(j.KEEP_LAST, MINUTE)), source(b, j.Keep())],
        str(tmp / "j.mefd"),
        exclude_channels=("a2",),
    )
    result = j.make_joined(request)
    assert result.ok and result.channels_kept == ("a1",)
    assert mef3io.Reader(str(tmp / "j.mefd")).channels == ["a1"]


def test_a_taken_name_is_found_by_the_check(two):
    tmp, a, b = two
    (tmp / "taken.mefd").mkdir()
    request = j.new_request(
        [source(a, j.Keep(j.KEEP_LAST, MINUTE)), source(b, j.Keep())], str(tmp / "taken.mefd")
    )
    findings = j.check_join(request)
    assert [f.message.title for f in findings] == ["That name is already taken"]


# --------------------------------------------------------------------------
# Plain words


def test_every_refusal_code_has_join_words():
    """Every code transform_session and merge_sessions can refuse with has
    the joiner's own words: everyday language, nothing about anonymizing,
    and what it means for the data."""
    from test_ui_language import BANNED_RE

    from mef3io_studio.model.refusals import JOIN_WORDS

    codes = set(mef3io.transform.REFUSAL_CODES) | set(mef3io.merge.REFUSAL_CODES)
    assert codes <= set(JOIN_WORDS), sorted(codes - set(JOIN_WORDS))
    for code in codes:
        title, text = JOIN_WORDS[code]({"channel": "x1", "field": None, "inspect_code": None})
        assert title and text
        assert not BANNED_RE.search(title) and not BANNED_RE.search(text), (code, title, text)
        assert "anonymi" not in (title + text).lower(), code
        assert "were not changed" in text, code
    for code, text in j.PROBLEM_WORDS.items():
        assert not BANNED_RE.search(text), (code, text)


def test_refusals_are_translated_by_code(two):
    tmp, a, _b = two
    request = j.new_request([source(a, j.Keep())], str(tmp / "x.mefd"))
    exc = ValueError(f"whatever mef3io says about {a}")
    exc.code = "merge-identity-differs"
    exc.details = {}
    message = j.describe_join_error(exc, request)
    assert message.title == "The recordings may be of different patients"
    assert str(a) not in message.detail and "<" in message.detail


# --------------------------------------------------------------------------
# Side by side: parts of the same period with different channels


def test_overlap_is_per_channel_and_honours_left_out_channels():
    a = j.JoinSource("a", "a.mefd", j.Keep())
    b = j.JoinSource("b", "b.mefd", j.Keep())
    layouts = {"a": j.Layout(0, 100, ("a1", "x")), "b": j.Layout(0, 100, ("b1", "x"))}
    plan = j.plan_join([a, b], layouts)
    assert [p.code for p in plan.problems] == [j.OVERLAP]
    assert plan.problems[0].channels == ("x",)
    text = j.problem_text(plan.problems[0], {"a": "Alpha", "b": "Beta"})
    assert "the channel x" in text and "leave out that channel" in text
    assert j.plan_join([a, b], layouts, excluded=("x",)).ok
    disjoint = {"a": j.Layout(0, 100, ("a1",)), "b": j.Layout(0, 100, ("b1",))}
    assert j.plan_join([a, b], disjoint).ok
    # Leaving out every channel of one recording is refused for it.
    plan = j.plan_join([a, b], disjoint, excluded=("a1",))
    assert [(p.code, p.keys) for p in plan.problems] == [(j.NO_CHANNELS_LEFT, ("a",))]


def test_side_by_side_recordings_join_into_the_union_of_channels(tmp_path):
    a = write(tmp_path / "A.mefd", T0, 3600, channels=("a1", "a2"))
    b = write(tmp_path / "B.mefd", T0, 3600, channels=("b1", "b2"))
    request = j.new_request(
        [source(a, j.Keep(j.KEEP_FIRST, 30 * MINUTE)), source(b, j.Keep())],
        str(tmp_path / "j.mefd"),
    )
    assert j.check_join(request) == []
    result = j.make_joined(request)
    assert result.ok, result.message
    joined = mef3io.Reader(str(tmp_path / "j.mefd"))
    assert sorted(joined.channels) == ["a1", "a2", "b1", "b2"]
    np.testing.assert_array_equal(
        kept_values(tmp_path / "j.mefd", "a1"), expected(T0, T0 + 30 * MINUTE)
    )
    for channel in ("b1", "b2"):
        np.testing.assert_array_equal(
            kept_values(tmp_path / "j.mefd", channel), kept_values(b, channel)
        )
    assert no_leftovers(tmp_path) == []


def test_side_by_side_with_a_shared_channel_is_refused_unless_it_is_left_out(tmp_path):
    a = write(tmp_path / "A.mefd", T0, 3600, channels=("a1", "x"))
    b = write(tmp_path / "B.mefd", T0, 3600, channels=("b1", "x"))
    sources = [source(a, j.Keep()), source(b, j.Keep())]
    request = j.new_request(sources, str(tmp_path / "j.mefd"))
    findings = j.check_join(request)
    assert len(findings) == 1 and "the channel x" in findings[0].message.text
    assert not j.make_joined(request).ok and not (tmp_path / "j.mefd").exists()
    request = j.new_request(sources, str(tmp_path / "j.mefd"), exclude_channels=("x",))
    assert j.check_join(request) == []
    assert j.make_joined(request).ok
    assert sorted(mef3io.Reader(str(tmp_path / "j.mefd")).channels) == ["a1", "b1"]
    np.testing.assert_array_equal(kept_values(tmp_path / "j.mefd", "b1"), kept_values(b, "b1"))


# --------------------------------------------------------------------------
# Privacy of the plain data, and where a recording ends


def test_no_path_in_any_repr(tmp_path):
    path = str(tmp_path / "Jane_Doe.mefd")
    src = j.JoinSource(path, path, j.Keep(j.KEEP_FIRST, 10), "pw")
    plan = j.plan_join([src, j.JoinSource(path + "2", path + "2", j.Keep())], {})
    finding = j.CheckFinding(path, j.UserMessage("t", "x"))
    for thing in (src, plan, *plan.problems, finding, j.Layout(1, 2, ("Jane",))):
        assert "Jane" not in repr(thing) and str(tmp_path) not in repr(thing), repr(thing)


def test_the_end_comes_from_the_last_block_not_the_stated_end(two, monkeypatch):
    """Another writer may state the last sample's own time as the end (one
    sample period early): the run takes the end of the last block."""
    tmp, a, _b = two
    real = mef3io.time_range

    def early_end(path, password="", **kwargs):
        found = real(path, password, **kwargs)
        return dataclasses.replace(found, end_uutc=found.end_uutc - S // 2)

    monkeypatch.setattr(mef3io, "time_range", early_end)
    request = j.new_request([source(a, j.Keep())], str(tmp / "x.mefd"))
    layouts, findings = j.read_layouts(request, CancelToken())
    assert not findings
    assert layouts[str(a)].end == T0 + 2 * HOUR
    assert layouts[str(a)].channels == ("a1", "a2")
    # "The last minute" then holds exactly the last 120 samples.
    request = j.new_request([source(a, j.Keep(j.KEEP_LAST, MINUTE))], str(tmp / "x.mefd"))
    assert j.make_joined(request).ok
    np.testing.assert_array_equal(
        kept_values(tmp / "x.mefd"), expected(T0 + 2 * HOUR - MINUTE, T0 + 2 * HOUR)
    )


def test_an_end_stated_later_than_the_data_is_found_from_the_blocks(two, monkeypatch):
    """A writer may state an end LATER than its last sample: no block lies at
    that stated end, so the channel's whole index is read and its true last
    block end is used (not the overstated one)."""
    tmp, a, _b = two
    real = mef3io.time_range

    def late_end(path, password="", **kwargs):
        found = real(path, password, **kwargs)
        return dataclasses.replace(found, end_uutc=found.end_uutc + 10 * MINUTE)

    monkeypatch.setattr(mef3io, "time_range", late_end)
    request = j.new_request([source(a, j.Keep())], str(tmp / "x.mefd"))
    layouts, findings = j.read_layouts(request, CancelToken())
    assert not findings and layouts[str(a)].end == T0 + 2 * HOUR


def test_a_failed_marker_leaves_no_folder(two, monkeypatch):
    """The marker created but not written (e.g. a full disk): the half-made
    marker and the folder this run just made are both removed."""
    import builtins

    tmp, a, b = two
    request = j.new_request(
        [source(a, j.Keep(j.KEEP_LAST, MINUTE)), source(b, j.Keep())], str(tmp / "j.mefd")
    )
    real_open = builtins.open

    class Full:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def write(self, _):
            raise OSError(28, "No space left on device")

    def failing_open(path, mode="r", *args, **kwargs):
        f = real_open(path, mode, *args, **kwargs)
        return Full(f) if str(path).endswith(j.PARTS_MARKER) and "x" in mode else f

    monkeypatch.setattr(builtins, "open", failing_open)
    with pytest.raises(OSError):
        j.make_parts_folder(request)
    assert not os.path.exists(request.parts_folder)
