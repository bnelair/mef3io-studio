"""The explorer's plain-Python layer, against real recordings written by
mef3io: the inspect adapter, checks, scanning, Copy summary, pack/unpack
planning and time formatting."""

from __future__ import annotations

import json
import os
from datetime import UTC

import mef3io
import pytest
from explorer_sessions import (
    FIRST,
    LAST,
    PASSWORD_BASIC,
    PASSWORD_FULL,
    PATIENT_ID,
    SECOND,
    SITE,
    T0,
    crc32,
    damage_details,
    damage_index,
    set_time_offset,
    write_encrypted,
    write_gappy,
    write_plain,
)

from mef3io_studio.model import archive
from mef3io_studio.model.cancel import CancelToken, JobCancelled
from mef3io_studio.model.inspect import inspect_many, inspect_recording
from mef3io_studio.model.problems import PLAIN_CHECKS, check_recording
from mef3io_studio.model.recording import ACCESS_BASIC, ACCESS_FULL, ACCESS_OPEN, recording_name
from mef3io_studio.model.scan import find_recordings
from mef3io_studio.model.summary import HIDDEN, as_json, as_text, build_summary
from mef3io_studio.model.timefmt import (
    format_rates,
    format_size,
    format_span,
    format_time,
    resolve_zone,
    zone_names,
)

DAY = 86_400 * SECOND


def test_crc_helper_matches_meflib():
    # Koopman-32 reflected: the table entry for 1 (meflib CRC_KOOPMAN32 table).
    assert crc32(b"\x01", 0) == 0x9695C4CA


# --------------------------------------------------------------------------
# The adapter
# --------------------------------------------------------------------------


def test_plain_recording(tmp_path):
    path = write_plain(tmp_path / "plain.mefd")
    outcome = inspect_recording(str(path))
    assert outcome.error is None and not outcome.needs_password
    d = outcome.details
    assert [c.name for c in d.channels] == ["a1", "a2", "a3"]
    assert d.start == T0 and d.end == T0 + 10 * SECOND
    assert d.sampling_rates == (256.0,)
    assert d.access == ACCESS_OPEN and not d.password_protected
    assert d.recording_time_offset == 0 and d.times_verified and not d.uses_time_offset
    assert d.patient.first_name == FIRST and d.patient.patient_id == PATIENT_ID
    assert d.patient.location == SITE
    assert outcome.size_bytes == sum(
        os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs
    )
    # Notes at all three levels (mef3io 1.2): session, channel and part.
    levels = sorted((n.level, n.where) for n in d.notes)
    assert levels == [("channel", "a1"), ("segment", "a1, part 1"), ("session", "Whole recording")]
    assert d.notes_problem is None
    # History: every part created by this mef3io.
    groups = d.history_groups()
    assert len(groups) == 1 and groups[0][0].created_by == mef3io.__version__
    assert groups[0][1] == 3
    # Everything identifying is offered to the log filter.
    values = d.identity_values()
    for value in (FIRST, LAST, PATIENT_ID, SITE, "plain", f"{LAST}, {FIRST}"):
        assert value in values


def test_gaps_between_and_inside_parts(tmp_path):
    d = inspect_recording(str(write_gappy(tmp_path / "gappy.mefd"))).details
    x, y = d.channels
    assert len(x.parts) == 1 and len(y.parts) == 2
    assert x.missing_seconds == pytest.approx(10.0)
    assert y.missing_seconds == 0
    assert d.part_count == 2
    assert d.gaps() == [(T0 + 30 * SECOND, T0 + 60 * SECOND)]
    assert d.parts_with_gaps_inside == 1


def test_encrypted_recording_at_each_access_level(tmp_path):
    path = str(write_encrypted(tmp_path / "locked.mefd"))
    locked = inspect_recording(path)
    assert locked.needs_password and not locked.wrong_password and locked.details is None
    assert locked.size_bytes and locked.size_bytes > 0

    wrong = inspect_recording(path, "nope")
    assert wrong.needs_password and wrong.wrong_password
    assert wrong.error.title == "This recording needs its password"

    basic = inspect_recording(path, PASSWORD_BASIC).details
    assert basic.access == ACCESS_BASIC
    assert not basic.patient.names_available and basic.patient.first_name is None
    # Without the full-access password the offset is unknown: unverified.
    assert basic.recording_time_offset is None and not basic.times_verified

    full = inspect_recording(path, PASSWORD_FULL).details
    assert full.access == ACCESS_FULL and full.times_verified
    assert full.patient.last_name == LAST


def test_time_offset_recording_is_flagged(tmp_path):
    path = set_time_offset(write_plain(tmp_path / "masked.mefd", notes=False), 3 * DAY)
    d = inspect_recording(str(path)).details
    assert d.recording_time_offset == 3 * DAY
    # Deprecated date masking: identifiable, and the times are unverified
    # (the legacy sign convention cannot be told from the bytes).
    assert d.uses_time_offset and not d.times_verified
    # The times shown are the ones mef3io returns for this file.
    with mef3io.Reader(str(path), warn_declarations=False) as reader:
        assert d.start == reader.info("a1")["start_time"]


def test_single_file_recording(tmp_path):
    folder = write_plain(tmp_path / "packed.mefd")
    tar = mef3io.archive_session(str(folder), str(tmp_path / "packed.mefd.tar"))
    outcome = inspect_recording(tar)
    assert outcome.packed and outcome.name == "packed"
    assert outcome.size_bytes == os.path.getsize(tar)
    direct = inspect_recording(str(folder)).details
    assert [c.parts for c in outcome.details.channels] == [c.parts for c in direct.channels]


def test_damaged_recording_explains_itself(tmp_path):
    path = damage_details(write_plain(tmp_path / "damaged.mefd", notes=False))
    outcome = inspect_recording(str(path))
    assert outcome.details is None and not outcome.needs_password
    assert outcome.error.title == "Part of this recording looks damaged"
    assert "Check for problems" in outcome.error.text
    # The technical text never names the location: check the check, too.
    report = check_recording(str(path))
    assert not report.ok and report.problem_count >= 1
    assert str(tmp_path) not in report.technical


def test_missing_recording(tmp_path):
    outcome = inspect_recording(str(tmp_path / "gone.mefd"))
    assert outcome.error.title == "The recording could not be opened"


def test_inspect_many_streams_outcomes(tmp_path):
    a = str(write_plain(tmp_path / "a.mefd", notes=False))
    b = str(write_encrypted(tmp_path / "b.mefd"))
    sent = []
    assert inspect_many([(a, ""), (b, "")], send=sent.append) == []
    assert [o.location for o in sent] == [a, b]
    assert [o.location for o in inspect_many([(a, "")])] == [a]


# --------------------------------------------------------------------------
# Check for problems
# --------------------------------------------------------------------------


def test_check_clean_and_damaged(tmp_path):
    clean = check_recording(str(write_plain(tmp_path / "ok.mefd")))
    assert clean.ok and clean.headline() == "No problems found"
    assert clean.short_status() == "No problems"
    bad = check_recording(str(damage_index(write_plain(tmp_path / "bad.mefd", notes=False))))
    assert not bad.ok
    assert bad.groups[0].title == PLAIN_CHECKS["crc.index"][0]
    assert "crc.index" in bad.groups[0].technical


def test_every_mef3io_check_has_plain_words():
    ids = {c.id for c in mef3io.available_checks()}
    assert ids <= set(PLAIN_CHECKS), ids - set(PLAIN_CHECKS)


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------


def test_scan_finds_recordings_and_skips_their_insides(tmp_path):
    root = tmp_path / "data"
    write_plain(root / "one" / "x.mefd", notes=False)
    write_plain(root / "two" / "deeper" / "y.mefd", notes=False)
    mef3io.archive_session(str(root / "one" / "x.mefd"), str(root / "z.mefd.tar"))
    (root / "empty").mkdir()
    (root / ".mef3io-studio-abc.mefd").mkdir()  # an unfinished copy: ignored
    result = find_recordings(str(root))
    names = sorted(recording_name(p) for p in result.locations)
    assert names == ["x", "y", "z"]
    assert result.folders_skipped == 0


def test_scan_stops_when_asked(tmp_path):
    for i in range(60):
        (tmp_path / f"d{i}" / "e").mkdir(parents=True)
    token = CancelToken()
    seen = []

    def report(done, total, phase):
        seen.append(done)
        token.cancel()

    with pytest.raises(JobCancelled):
        find_recordings(str(tmp_path), token, report)
    assert seen  # it got going before it stopped


# --------------------------------------------------------------------------
# Copy summary: names and dates hidden unless shown
# --------------------------------------------------------------------------


def _leaks(text: str) -> list[str]:
    return [v for v in (FIRST, LAST, PATIENT_ID, SITE, "2000-01-01", "moved") if v in text]


def test_summary_hides_names_and_dates(tmp_path):
    outcome = inspect_recording(str(write_plain(tmp_path / "Zebedee_2000.mefd")))
    zone = resolve_zone("Europe/Prague")
    summary = build_summary(outcome, zone, show=False)
    for text in (as_text([summary]), as_json([summary])):
        assert _leaks(text) == [], text
        assert "Zebedee_2000" not in text
        assert HIDDEN in text
    data = json.loads(as_json([summary]))
    assert data["patient"] == HIDDEN and data["start"] == HIDDEN
    assert data["notes_and_events"] == {"count": 3}
    assert data["channel_count"] == 3


def test_summary_with_details_shown(tmp_path):
    outcome = inspect_recording(str(write_plain(tmp_path / "rec.mefd")))
    text = as_text([build_summary(outcome, UTC, show=True)])
    assert FIRST in text and PATIENT_ID in text and "2000-01-01 00:00:00 UTC" in text
    assert "moved" in text


def test_summary_of_a_locked_recording(tmp_path):
    outcome = inspect_recording(str(write_encrypted(tmp_path / "locked.mefd")))
    data = build_summary(outcome, UTC)
    assert data["status"] == "password needed" and data["recording"] == HIDDEN


def test_summary_notes_offset_and_unverified_times(tmp_path):
    masked = set_time_offset(write_plain(tmp_path / "m.mefd", notes=False), 3 * DAY)
    data = build_summary(inspect_recording(str(masked)), UTC)
    assert "can be undone" in data["date_note"]
    basic = inspect_recording(str(write_encrypted(tmp_path / "e.mefd")), PASSWORD_BASIC)
    assert "times_note" in build_summary(basic, UTC)


# --------------------------------------------------------------------------
# Pack / unpack planning
# --------------------------------------------------------------------------


def test_pack_and_unpack_write_new_checked_copies(tmp_path):
    src = write_plain(tmp_path / "rec.mefd", notes=False)
    out = tmp_path / "out"
    out.mkdir()
    plan = archive.plan(str(src), str(out))
    assert plan.packing and plan.target.endswith("rec.mefd.tar")
    assert "rec" not in os.path.basename(plan.temporary)  # opaque
    archive.run_in_helper(plan)
    target = archive.finish(plan)
    assert os.path.isfile(target) and not os.path.exists(plan.temporary)

    back = tmp_path / "back"
    back.mkdir()
    plan2 = archive.plan(target, str(back))
    assert not plan2.packing
    archive.run_in_helper(plan2)
    folder = archive.finish(plan2)
    assert os.path.isdir(folder) and folder.endswith("rec.mefd")
    assert (
        inspect_recording(folder).details.channels == inspect_recording(str(src)).details.channels
    )


def test_pack_never_replaces(tmp_path):
    src = write_plain(tmp_path / "rec.mefd", notes=False)
    (tmp_path / "rec.mefd.tar").write_text("someone else's file")
    with pytest.raises(archive.TargetExists):
        archive.plan(str(src), str(tmp_path))
    out = tmp_path / "o"
    out.mkdir()
    plan = archive.plan(str(src), str(out))
    archive.run_in_helper(plan)
    (out / "rec.mefd.tar").write_text("appeared meanwhile")
    with pytest.raises(archive.TargetExists):
        archive.finish(plan)
    assert (out / "rec.mefd.tar").read_text() == "appeared meanwhile"
    archive.remove_temporary(plan)
    assert not os.path.exists(plan.temporary)


def test_not_enough_space_is_refused(tmp_path):
    src = write_plain(tmp_path / "rec.mefd", notes=False)
    out = tmp_path / "o"
    out.mkdir()
    plan = archive.plan(str(src), str(out))
    with pytest.raises(archive.NotEnoughSpace):
        archive.check_space(plan, 10**18)


# --------------------------------------------------------------------------
# Times and numbers
# --------------------------------------------------------------------------


def test_times_are_utc_until_a_zone_is_chosen():
    assert resolve_zone("") is UTC and resolve_zone("Not/AZone") is UTC
    assert format_time(T0, UTC) == "2000-01-01 00:00:00 UTC"
    assert format_time(T0, resolve_zone("Europe/Prague")) == "2000-01-01 01:00:00 +01:00"
    assert "Europe/Prague" in zone_names() and zone_names()[0] == "UTC"


def test_daylight_saving_change_matches_the_tz_database():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    zone = resolve_zone("Europe/Prague")
    # 2021-03-28: clocks went from 02:00 CET to 03:00 CEST at 01:00 UTC.
    before = int(datetime(2021, 3, 28, 0, 59, tzinfo=UTC).timestamp()) * SECOND
    after = before + 2 * 60 * SECOND
    assert format_time(before, zone) == "2021-03-28 01:59:00 +01:00"
    assert format_time(after, zone) == "2021-03-28 03:01:00 +02:00"
    for us in (before, after):
        expected = datetime.fromtimestamp(us / 1e6, ZoneInfo("Europe/Prague"))
        assert format_time(us, zone).startswith(expected.strftime("%Y-%m-%d %H:%M:%S"))


def test_number_formats():
    assert format_span(45) == "45 s"
    assert format_span(725) == "12 min 05 s"
    assert format_span(3 * 3600 + 120) == "3 h 02 min"
    assert format_span(2 * 86400 + 4 * 3600) == "2 days 4 h"
    assert format_size(41_442_800) == "41.4 MB"
    assert format_rates([256.0, 512.0]) == "256 Hz, 512 Hz"
    assert format_rates([1, 2, 3, 4]) == "1 Hz to 4 Hz (4 different)"


def test_pack_refuses_a_destination_inside_a_recording(tmp_path):
    src = write_plain(tmp_path / "rec.mefd", notes=False)
    other = write_plain(tmp_path / "other.mefd", notes=False)
    for destination in (src, src / "a1.timd", other):
        with pytest.raises(archive.InsideRecording):
            archive.plan(str(src), str(destination))


def test_pack_and_check_a_password_protected_recording(tmp_path):
    src = write_encrypted(tmp_path / "locked.mefd")
    out = tmp_path / "o"
    out.mkdir()
    plan = archive.plan(str(src), str(out))
    archive.run_in_helper(plan, PASSWORD_FULL)
    target = archive.finish(plan)
    assert inspect_recording(target, PASSWORD_FULL).details.access == ACCESS_FULL
    assert inspect_recording(target).needs_password  # the copy keeps its protection


def test_temporary_cleanup_includes_mef3io_working_files(tmp_path):
    src = write_plain(tmp_path / "rec.mefd", notes=False)
    out = tmp_path / "o"
    out.mkdir()
    plan = archive.plan(str(src), str(out))
    working = plan.temporary + ".tmp"  # what archive_session writes before renaming
    with open(working, "wb") as fh:
        fh.write(b"x" * 1000)
    assert archive.written_bytes(plan) == 1000
    (out / "unrelated.txt").write_text("keep me")
    archive.remove_temporary(plan)
    assert sorted(p.name for p in out.iterdir()) == ["unrelated.txt"]


def test_scan_of_something_that_is_not_a_folder(tmp_path):
    from mef3io_studio.model.errors import describe_error
    from mef3io_studio.model.scan import NotAFolder

    stray = tmp_path / "notes.txt"
    stray.write_text("x")
    with pytest.raises(NotAFolder) as info:
        find_recordings(str(stray))
    assert describe_error(info.value).title == "This is not a recording"
