"""A new copy with changes (model/changes.py): the details that can be
changed, the new passwords, the refusals in plain words, and real runs on
small recordings written by mef3io (made-up signals only)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import mef3io
import numpy as np
import pytest
from explorer_sessions import (
    FIRST,
    LAST,
    PASSWORD_BASIC,
    PASSWORD_FULL,
    PATIENT_ID,
    SITE,
    T0,
    write_encrypted,
    write_plain,
)

from mef3io_studio.model import changes as c
from mef3io_studio.model.cancel import CancelToken
from mef3io_studio.model.inspect import inspect_recording
from mef3io_studio.model.recording import operation_words

NEW_BASIC, NEW_FULL = "new-basic-77", "new-full-88"


def tree_hash(folder: Path) -> dict[str, str]:
    """Every file of a recording: name -> SHA-256 (the original must stay
    byte-identical)."""
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def read(path: Path, channel="a1", password=""):
    reader = mef3io.Reader(str(path), password=password) if password else mef3io.Reader(str(path))
    return np.asarray(reader.read(channel), dtype=float)


def request(src: Path, dst: Path, **kwargs) -> c.ChangeRequest:
    return c.ChangeRequest(str(src), str(dst), **kwargs)


# --------------------------------------------------------------------------
# What can be changed


def test_the_limits_are_mefs():
    assert c.DETAIL_LIMITS == dict(mef3io.transform.METADATA_FIELDS)


def test_a_text_fits_when_its_bytes_and_the_terminator_fit():
    assert c.fits("x" * 127, "subject_id") and not c.fits("x" * 128, "subject_id")
    # Bytes, not characters.
    assert c.fits("é" * 63, "subject_id") and not c.fits("é" * 64, "subject_id")
    assert c.remaining("abc", "subject_id") == 124
    assert c.too_long({"subject_id": "x" * 128, "recording_location": "ok"}) == ["subject_id"]
    assert c.too_long({"channel_descriptions": {"a1": "x" * 2048}}) == ["channel_descriptions"]
    assert c.too_long(None) == []


def test_only_what_changed_is_sent():
    current = {"subject_name_1": "Ann", "subject_id": "7", "recording_location": ""}
    new = {"subject_name_1": "Ann", "subject_id": "", "recording_location": "Ward 1"}
    assert c.changed_details(current, new) == {"subject_id": "", "recording_location": "Ward 1"}
    # Never shown: any text is a change, an empty box means "keep".
    hidden = {name: None for name in current}
    assert c.changed_details(hidden, {"subject_name_1": "", "subject_id": "9"}) == {
        "subject_id": "9"
    }
    # Per channel, the same way; references are never shown.
    got = c.changed_details(
        {},
        {},
        {"a1": "old", "a2": "same"},
        {"a1": "new", "a2": "same", "a3": ""},
        {"a1": "ref", "a2": ""},
    )
    assert got == {"channel_descriptions": {"a1": "new"}, "reference_descriptions": {"a1": "ref"}}
    assert c.changed_details({}, {}) == {}


def test_a_request_hides_everything_in_its_repr(tmp_path):
    r = request(
        tmp_path / "a.mefd",
        tmp_path / "b.mefd",
        password="pw-source-1",
        output_password=("nb-1", "nf-2"),
        metadata={"subject_id": "ID-5551"},
        exclude_channels=("a2",),
    )
    text = repr(r) + repr(c.ChangeResult(r.destination, channels_kept=("a1",)))
    for secret in ("pw-source-1", "nb-1", "nf-2", "ID-5551", str(tmp_path), "a2"):
        assert secret not in text


def test_the_model_does_not_import_qt():
    import ast

    tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        assert not any(n.split(".")[0] in ("PySide6", "mef3io_studio") for n in names), names


# --------------------------------------------------------------------------
# A real copy


def test_other_details_in_a_new_copy_and_the_original_untouched(tmp_path):
    src = write_plain(tmp_path / "orig.mefd")
    before = tree_hash(src)
    dst = tmp_path / "edited.mefd"
    metadata = {
        "subject_name_1": "Alex",
        "subject_id": "",
        "recording_location": "Room 12",
        "channel_descriptions": {"a1": "frontal, left"},
    }
    assert c.check_changes(request(src, dst, metadata=metadata)) == []
    sent = []
    result = c.make_changes(request(src, dst, metadata=metadata), send=sent.append)
    assert result.ok and result.message is None, result.message
    assert result.details_changed > 0 and result.bytes_written > 0
    assert result.channels_kept == ("a1", "a2", "a3")
    # The original is byte-identical, the copy is a new recording next to it.
    assert tree_hash(src) == before
    assert dst.exists() and not (tmp_path / "edited.mefd.partial").exists()
    # Samples and times are as stored.
    for name in ("a1", "a2", "a3"):
        np.testing.assert_array_equal(read(src, name), read(dst, name))
    assert mef3io.time_range(str(src)).start_uutc == mef3io.time_range(str(dst)).start_uutc
    # The details: changed where asked, kept elsewhere (the last name).
    reader = mef3io.Reader(str(dst))
    subject = reader.metadata.subject
    assert subject.name_1 == "Alex" and subject.name_2 == LAST
    assert (subject.id or "") == "" and subject.recording_location == "Room 12"
    summary = mef3io.inspect(str(dst), include_values=True)
    assert "frontal, left" in {ch.channel_description for ch in summary.channels if ch.name == "a1"}
    # The original still says what it said.
    old = mef3io.Reader(str(src)).metadata.subject
    assert old.name_1 == FIRST and old.id == PATIENT_ID and old.recording_location == SITE


def test_the_history_shows_the_copy_with_changes(tmp_path):
    src = write_plain(tmp_path / "orig.mefd")
    dst = tmp_path / "edited.mefd"
    assert c.make_changes(request(src, dst, metadata={"subject_id": "X-1"})).ok
    details = inspect_recording(str(dst)).details
    history, parts = details.history_groups()[0]
    assert history.last_operation == "transform" and parts == 3
    assert operation_words("transform") == "copied with changes"


def test_clearing_a_detail_and_keeping_the_rest(tmp_path):
    src = write_plain(tmp_path / "orig.mefd")
    dst = tmp_path / "cleared.mefd"
    assert c.make_changes(request(src, dst, metadata={"recording_location": ""})).ok
    subject = mef3io.Reader(str(dst)).metadata.subject
    assert (subject.recording_location or "") == ""
    assert subject.name_1 == FIRST and subject.id == PATIENT_ID


def test_channels_and_a_part_of_the_time_go_through_the_same_call(tmp_path):
    src = write_plain(tmp_path / "orig.mefd")
    dst = tmp_path / "part.mefd"
    r = request(src, dst, exclude_channels=("a3",), time_range=(T0, T0 + 5_000_000))
    result = c.make_changes(r)
    assert result.ok and result.channels_excluded == ("a3",)
    assert mef3io.Reader(str(dst)).channels == ["a1", "a2"]


# --------------------------------------------------------------------------
# Passwords


def test_a_password_protected_original_to_a_copy_without_passwords(tmp_path):
    src = write_encrypted(tmp_path / "orig.mefd")
    dst = tmp_path / "open.mefd"
    r = request(src, dst, password=PASSWORD_FULL, metadata={"subject_id": "Z-9"})
    assert c.check_changes(r) == []
    assert c.make_changes(r).ok
    # No password needed; the original's are not carried over.
    reader = mef3io.Reader(str(dst))
    assert reader.metadata.subject.id == "Z-9"
    np.testing.assert_array_equal(read(src, "c", PASSWORD_FULL), read(dst, "c"))
    with pytest.raises(mef3io.PasswordError):
        mef3io.Reader(str(src))


def test_a_new_pair_replaces_the_old_passwords(tmp_path):
    src = write_encrypted(tmp_path / "orig.mefd")
    dst = tmp_path / "new.mefd"
    r = request(
        src, dst, password=PASSWORD_FULL, output_password=(NEW_BASIC, NEW_FULL),
        metadata={"recording_location": "Elsewhere"},
    )  # fmt: skip
    assert c.make_changes(r).ok
    reader = mef3io.Reader(str(dst), password=NEW_FULL)
    assert reader.metadata.subject.recording_location == "Elsewhere"
    assert reader.metadata.subject.name_1 == FIRST
    for old in (PASSWORD_FULL, PASSWORD_BASIC):
        with pytest.raises(mef3io.PasswordError):
            mef3io.Reader(str(dst), password=old)


def test_a_plain_original_gets_a_new_pair(tmp_path):
    src = write_plain(tmp_path / "orig.mefd")
    dst = tmp_path / "locked.mefd"
    assert c.make_changes(request(src, dst, output_password=(NEW_BASIC, NEW_FULL))).ok
    with pytest.raises(mef3io.PasswordError):
        mef3io.Reader(str(dst))
    assert mef3io.Reader(str(dst), password=NEW_FULL).metadata.subject.name_1 == FIRST


def test_a_wrong_source_password_is_a_plain_message(tmp_path):
    src = write_encrypted(tmp_path / "orig.mefd")
    dst = tmp_path / "x.mefd"
    r = request(src, dst, password="not-the-password", output_password=(NEW_BASIC, NEW_FULL))
    found = c.check_changes(r)
    assert found and found[0].code == "password-wrong"
    result = c.make_changes(r)
    assert not result.ok and not dst.exists()
    assert result.message.title == "The password did not open a recording"
    for secret in ("not-the-password", NEW_BASIC, NEW_FULL, str(tmp_path)):
        assert secret not in result.message.text + result.message.detail


def test_basic_access_is_not_enough_to_copy(tmp_path):
    src = write_encrypted(tmp_path / "orig.mefd")
    result = c.make_changes(request(src, tmp_path / "x.mefd", password=PASSWORD_BASIC))
    assert not result.ok
    assert result.message.title == "This recording needs its full-access password"


def test_an_empty_full_access_password_is_refused(tmp_path):
    src = write_plain(tmp_path / "orig.mefd")
    dst = tmp_path / "x.mefd"
    result = c.make_changes(request(src, dst, output_password=("some-basic", "")))
    assert not result.ok and not dst.exists()
    assert result.message.title == "Type both passwords"
    assert c.ChangeResult(str(dst)).retryable is False


def test_no_password_reaches_the_log(tmp_path, caplog):
    src = write_encrypted(tmp_path / "orig.mefd")
    secrets_ = (PASSWORD_FULL, NEW_BASIC, NEW_FULL)
    with caplog.at_level(logging.DEBUG):
        r = request(src, tmp_path / "ok.mefd", password=PASSWORD_FULL)
        r = c.ChangeRequest(**{**r.__dict__, "output_password": (NEW_BASIC, NEW_FULL)})
        assert c.make_changes(r).ok
        c.check_changes(r)
    text = caplog.text + "".join(repr(rec.args) for rec in caplog.records)
    assert not any(s in text for s in secrets_)
    assert not any(s in repr(r) for s in secrets_)


# --------------------------------------------------------------------------
# Refusals, in words


def test_a_text_that_is_too_long_is_refused_never_cut(tmp_path):
    src = write_plain(tmp_path / "orig.mefd")
    dst = tmp_path / "x.mefd"
    r = request(src, dst, metadata={"subject_name_1": "x" * 200})
    found = c.check_changes(r)
    assert [f.code for f in found] == ["metadata-too-long"]
    assert found[0].message.title == "A detail is too long"
    assert "first name" in found[0].message.text and "x" * 20 not in found[0].message.text
    result = c.make_changes(r)
    assert not result.ok and result.message.title == "A detail is too long"
    assert not dst.exists() and not (tmp_path / "x.mefd.partial").exists()


def test_a_description_for_an_unknown_channel_is_refused(tmp_path):
    src = write_plain(tmp_path / "orig.mefd")
    r = request(src, tmp_path / "x.mefd", metadata={"channel_descriptions": {"nope": "hi"}})
    found = c.check_changes(r)
    assert [f.code for f in found] == ["metadata-channel-unknown"]
    assert "nope" in found[0].message.text


def test_a_taken_name_is_found_by_the_check(tmp_path):
    src = write_plain(tmp_path / "orig.mefd")
    taken = write_plain(tmp_path / "taken.mefd")
    found = c.check_changes(request(src, taken, metadata={"subject_id": "1"}))
    assert [f.code for f in found] == ["dst-exists"]


def test_every_transform_refusal_has_words_without_jargon():
    from test_ui_language import BANNED_RE

    from mef3io_studio.model.refusals import JOIN_WORDS

    for code in mef3io.transform.REFUSAL_CODES:
        title, text = JOIN_WORDS[code]({"channel": "x1", "field": "subject_id"})
        assert title and "were not changed" in text and not BANNED_RE.search(title + text), code
    # The detail refusals say what to do, not "a choice was not valid".
    assert JOIN_WORDS["metadata-too-long"]({"field": "recording_location"})[0] == (
        "A detail is too long"
    )


def test_stop_leaves_nothing_behind(tmp_path):
    from mef3io_studio.model.cancel import JobCancelled

    src = write_plain(tmp_path / "orig.mefd")
    token = CancelToken()
    token.cancel()
    with pytest.raises(JobCancelled):
        c.make_changes(request(src, tmp_path / "x.mefd"), cancel=token)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["orig.mefd"]


def test_transform_session_has_one_call_site():
    """Edit details and Join or trim both copy through model/changes.py."""
    import ast

    src = Path(c.__file__).resolve().parents[1]
    users = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "transform_session":
                users.append(path.relative_to(src).as_posix())
    assert users == ["model/changes.py"]
