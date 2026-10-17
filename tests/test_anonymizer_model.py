"""The anonymizer's plain-Python side (model/anonymize.py): plain words for
mef3io's refusals, scrubbed technical details, the early name check and its
throw-away folder, password and folder rules, the date key file."""

from __future__ import annotations

import os
import stat
import sys

import mef3io
import pytest
from explorer_sessions import DAY, LAST, PATIENT_ID, write_plain

from mef3io_studio.model import anonymize as a


def request(tmp_path, source="JaneDoe.mefd", name="S001", **kw) -> a.CopyRequest:
    return a.CopyRequest(
        key="k",
        source=str(tmp_path / source),
        destination=str(tmp_path / "out" / f"{name}.mefd"),
        shift_us=4000 * DAY,
        **kw,
    )


# --------------------------------------------------------------------------
# Technical details: no paths, names or raw times


def test_technical_details_lose_the_name_paths_and_raw_times(tmp_path):
    r = request(tmp_path)
    raw = (
        f"bad record in {tmp_path}/JaneDoe.mefd/JaneDoe.rdat (also JaneDoe-000000.segd) at "
        "1552555613000000 uUTC; archive x.mefd.tar::JaneDoe.mefd/c.timd/c-000000.segd/c.tmet"
    )
    from mef3io_studio.workers.tasks import scrubbed

    text = a.technical_text(ValueError(raw), r)
    for secret in ("JaneDoe", str(tmp_path)):
        assert secret not in text, text
    assert text.startswith("ValueError:")
    # What the UI gets has also been through the log filter's scrub.
    message = scrubbed(a.describe_copy_error(ValueError(raw), r))
    for secret in ("JaneDoe", "1552555613000000", str(tmp_path)):
        assert secret not in message.detail, message.detail


def test_app_folder_names_match_the_logging_setup():
    from mef3io_studio.common import logging_setup

    assert (a.APP_NAME, a.APP_AUTHOR) == (logging_setup.APP_NAME, logging_setup.APP_AUTHOR)


def test_a_short_name_is_only_replaced_as_a_word(tmp_path):
    r = request(tmp_path, source="ab.mefd")
    text = a.technical_text(ValueError("ab.rdat failed; table abc stays"), r)
    assert "<name>.rdat" in text and "abc" in text


# --------------------------------------------------------------------------
# Plain words: by refusal key, then by kind of error


def test_every_refusal_code_has_plain_words():
    """Every code mef3io can refuse with (REFUSAL_CODES) has its own words,
    in everyday language."""
    from test_ui_language import BANNED_RE

    from mef3io_studio.model.refusals import WORDS, words_for

    codes = set(mef3io.anonymize.REFUSAL_CODES)
    assert codes <= set(WORDS), sorted(codes - set(WORDS))
    for code in codes:
        title, text = words_for(code, {"channel": "x1", "field": "dst", "inspect_code": None})
        assert title and text
        assert not BANNED_RE.search(title) and not BANNED_RE.search(text), (code, title, text)
        assert "was not changed" in text or "were not changed" in text, code


def test_refusals_are_translated_by_code_never_by_message(tmp_path):
    exc = ValueError("whatever mef3io says today")
    exc.code = "shift-below-floor"
    exc.details = {"channel": None, "field": "shift_us", "inspect_code": None}
    message = a.describe_copy_error(exc, request(tmp_path))
    assert message.title == "The new date is too close to the real one"
    # The same words without a code fall back by the kind of error only.
    plain = a.describe_copy_error(ValueError("must be at least the time span"), request(tmp_path))
    assert plain.title == "The copy could not be made"


@pytest.mark.parametrize(
    ("exc", "title"),
    [
        (OSError(5, "input/output error"), "The copy could not be written to the chosen folder"),
        (mef3io.FormatError("bad section 2"), "Part of the recording is damaged"),
        (mef3io.CrcError("block CRC mismatch"), "Part of the recording is damaged"),
        (
            mef3io.WriteConflictError("something unforeseen"),
            "This recording cannot be copied safely",
        ),
        (
            mef3io.PasswordError("needs level 2"),
            "This recording needs its full-access password",
        ),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else "",
)
def test_fallbacks_without_a_code_are_worded_for_a_copy(tmp_path, exc, title):
    message = a.describe_copy_error(exc, request(tmp_path))
    assert message.title == title
    if "damaged" in title:
        assert "nothing was written" in message.text
    if "safely" in title:
        assert "wrong date" in message.text


def test_unreachable_files_are_retryable_by_code():
    for code, retry in (
        ("source-missing", True),
        ("io-error", True),
        ("dst-exists", False),
        ("name-contains-identity", False),
    ):
        exc = ValueError("x")
        exc.code = code
        assert a.is_unreachable(exc) is retry, code
    assert a.is_unreachable(OSError(5, "gone"))


def test_the_name_check_writes_nothing_and_drops_new_passwords(tmp_path, monkeypatch):
    """mef3io.anonymize.check() decides; nothing is written anywhere; the
    copy's new passwords are never handed over."""
    seen = {}
    real = mef3io.anonymize.check

    def spy(src, dst, shift, **kwargs):
        seen.update(kwargs, dst=dst)
        return real(src, dst, shift, **kwargs)

    monkeypatch.setattr(mef3io.anonymize, "check", spy)
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    good = a.CopyRequest(
        "k", str(src), str(tmp_path / "S1.mefd"), 4000 * DAY, output_password=("b-1", "f-2")
    )
    assert a.check_names(good) is None
    assert seen["output_password"] is None
    assert not os.path.exists(os.path.dirname(seen["dst"]))  # a folder that is not there
    bad = a.CopyRequest("k", str(src), str(tmp_path / f"{LAST}1.mefd"), 4000 * DAY)
    message = a.check_names(bad)
    assert message is not None and message.title == "Choose another name for the copy"
    pseudonym = a.CopyRequest(
        "k", str(src), str(tmp_path / "S1.mefd"), 4000 * DAY, pseudonym=PATIENT_ID
    )
    assert a.check_names(pseudonym).title == "Choose another pseudonym"
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_the_batch_check_sees_where_the_copy_really_goes(tmp_path):
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    (tmp_path / "S1.mefd").mkdir()
    r = a.CopyRequest("k", str(src), str(tmp_path / "S1.mefd"), 4000 * DAY)
    assert a.check_copies([r])["k"].title == "That name is already taken"
    assert a.check_names(r) is None  # the name step does not judge the folder


def test_old_name_check_folders_are_swept_once(tmp_path, monkeypatch):
    root = tmp_path / "old-scratch"
    (root / "namecheck-dead1" / "S1.mefd").mkdir(parents=True)
    monkeypatch.setenv(a.OLD_SCRATCH_DIR_ENV, str(root))
    assert a.sweep_old_name_check_folders() == 1
    assert not root.exists()
    assert a.sweep_old_name_check_folders() == 0


@pytest.mark.parametrize(
    ("password", "warn"),
    [
        ("short-pw", False),
        ("exactly15chars!", False),
        ("sixteen-chars-pw", True),
        ("café", False),  # é is Latin-1: fine, 5 bytes
        ("éééééééé", True),  # 8 characters, 16 UTF-8 bytes
        ("pass€", True),  # € is above U+00FF
    ],
)
def test_password_interop_rule(password, warn):
    assert a.password_may_not_work_elsewhere(password) is warn


@pytest.mark.parametrize("name", ["CON", "nul", "COM1", "lpt9.txt", "Aux"])
def test_windows_reserved_names_are_refused(name):
    assert "Windows" in a.name_problem(name)


def test_a_copy_inside_any_recording_is_refused(tmp_path):
    other = tmp_path / "other.mefd"
    assert a.inside_any_recording(str(other / "out" / "S1.mefd"), [str(other)])
    assert a.inside_any_recording(str(tmp_path / "x.MEFD" / "S1.mefd"))
    assert not a.inside_any_recording(str(tmp_path / "out" / "S1.mefd"), [str(other)])


def test_the_date_key_is_not_kept_with_the_copies(tmp_path):
    out = tmp_path / "out"
    assert a.date_key_too_close(str(out / "key.csv"), [str(out)])
    assert a.date_key_too_close(str(out / "S1.mefd" / "key.csv"), [str(out / "S1.mefd")])
    assert not a.date_key_too_close(str(tmp_path / "safe" / "key.csv"), [str(out)])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_the_date_key_is_readable_by_its_owner_only(tmp_path):
    path = tmp_path / "key.csv"
    a.write_date_key(str(path), [a.DateKeyRow("orig", "S1", 10**15, 10**15 + 1, DAY * 4000)])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["key.csv"]


def test_an_unremovable_unfinished_copy_is_reported(tmp_path, monkeypatch):
    destination = str(tmp_path / "S1.mefd")
    assert a.remove_unfinished(destination) is True  # nothing there
    (tmp_path / "S1.mefd.partial").mkdir()
    monkeypatch.setattr(a.shutil, "rmtree", lambda *x, **k: None)
    assert a.remove_unfinished(destination) is False
