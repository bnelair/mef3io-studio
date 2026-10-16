"""The anonymizer's plain-Python side (model/anonymize.py): plain words for
mef3io's refusals, scrubbed technical details, the early name check and its
throw-away folder, password and folder rules, the date key file."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

import mef3io
import pytest
from explorer_sessions import DAY, LAST, write_plain

from mef3io_studio.model import anonymize as a

HAS_ANONYMIZER = callable(getattr(mef3io, "anonymize_session", None))
needs_anonymizer = pytest.mark.skipif(
    not HAS_ANONYMIZER,
    reason="this mef3io has no anonymize_session (needs the release with bnelair/mef3io#104)",
)


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


def test_a_refusal_code_wins_over_the_wording(tmp_path):
    exc = ValueError("something new mef3io says")
    exc.code = a.SHIFT_TOO_SMALL
    assert a.refusal_code(exc) == a.SHIFT_TOO_SMALL
    assert a.describe_copy_error(exc, request(tmp_path)).title == (
        "The new date is too close to the real one"
    )


@pytest.mark.parametrize(
    ("exc", "title"),
    [
        (
            mef3io.IoError("cannot create the destination: x.mefd.partial"),
            "The copy could not be written to the chosen folder",
        ),
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
def test_fallbacks_by_kind_are_worded_for_a_copy(tmp_path, exc, title):
    message = a.describe_copy_error(exc, request(tmp_path))
    assert message.title == title
    if "damaged" in title:
        assert "nothing was written" in message.text
    if "safely" in title:
        assert "wrong date" in message.text


def test_known_refusals_by_wording(tmp_path):
    cases = {
        "the new session name (the destination folder's name) contains the patient's ID; "
        "choose another": "Choose another name for the copy",
        "the pseudonym contains the recording location; choose another": (
            "Choose another pseudonym"
        ),
        "a partial copy is in the way (...)": "An unfinished copy is in the way",
        "the destination already exists: x": "That name is already taken",
        "the copy failed verification, so it was not published (1 problem(s)):": (
            "The copy did not pass its check"
        ),
    }
    for raw, title in cases.items():
        assert a.describe_copy_error(ValueError(raw), request(tmp_path)).title == title, raw


# --------------------------------------------------------------------------
# The early name check


def test_name_check_folder_is_private_opaque_and_removed(tmp_path, monkeypatch):
    """Without the real library: a stand-in reports progress at once, as
    mef3io does after its checks; the folder is removed and the copy's new
    passwords are never handed over."""
    made, seen = [], {}
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        made.append(path)
        return path

    def fake(src, dst, shift, *, progress, cancel, **kwargs):
        seen.update(kwargs, dst=dst)
        assert Path(dst).parent.is_dir()
        progress(0, 1)
        raise AssertionError("not reached")

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(mef3io, "anonymize_session", fake, raising=False)
    r = request(tmp_path, output_password=("basic-1", "full-2"), password="orig-pw")
    assert a.check_names(r) is None
    (folder,) = made
    assert not os.path.exists(folder)
    root = a.scratch_root()
    assert os.path.dirname(folder) == root
    assert os.path.basename(folder).startswith("namecheck-")
    assert "S001" not in os.path.basename(folder)  # opaque; only the probe inside is named
    assert seen["output_password"] is None
    assert seen["password"] == "orig-pw"  # opening an encrypted original needs it


def test_leftover_name_check_folders_are_swept(tmp_path):
    root = Path(a.scratch_root())
    (root / "namecheck-dead1").mkdir()
    (root / "namecheck-dead1" / "S1.mefd").mkdir()
    (root / "something-else").mkdir(exist_ok=True)
    assert a.sweep_scratch() >= 1
    assert not (root / "namecheck-dead1").exists()
    assert (root / "something-else").exists()


@needs_anonymizer
def test_mef3io_refuses_names_before_its_first_progress_report(tmp_path):
    """Pins what check_names relies on (until mef3io#111's check()): the
    name refusal comes before any progress, and a probe that passes the
    checks writes nothing anywhere."""
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    bad = a.CopyRequest("k", str(src), str(tmp_path / f"{LAST}1.mefd"), 4000 * DAY)
    message = a.check_names(bad)
    assert message is not None and message.title == "Choose another name for the copy"
    good = a.CopyRequest("k", str(src), str(tmp_path / "S1.mefd"), 4000 * DAY)
    assert a.check_names(good) is None
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
    assert not [p for p in Path(a.scratch_root()).iterdir() if p.name.startswith("namecheck-")]


# --------------------------------------------------------------------------
# Rules


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
