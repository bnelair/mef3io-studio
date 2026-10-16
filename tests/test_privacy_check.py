"""The Privacy check card (mef3io-studio#5): on an anonymized copy, on the
original, on a recording from another program, and on a copy that kept its
note text. Built from inspect() only: it reads no file of its own."""

from __future__ import annotations

import builtins
import os
from pathlib import Path

import mef3io
import pytest
from explorer_sessions import (
    DAY,
    FIRST,
    LAST,
    PATIENT_ID,
    T0,
    set_time_offset,
    write_foreign_like,
    write_plain,
    write_with_time_zone,
)

from mef3io_studio.explorer.panels import HIDDEN
from mef3io_studio.model.inspect import inspect_recording
from mef3io_studio.model.privacycheck import (
    MOVED_DATES,
    NOT_MADE_BY_ANONYMIZER,
    Mark,
    build_check,
    headline,
)
from mef3io_studio.model.recording import (
    ChannelInfo,
    History,
    IdentityFact,
    Part,
    RecordingDetails,
)

HAS_ANONYMIZER = callable(getattr(mef3io, "anonymize_session", None))
needs_anonymizer = pytest.mark.skipif(
    not HAS_ANONYMIZER,
    reason="this mef3io has no anonymize_session (needs the release with bnelair/mef3io#104)",
)
SHIFT = 4000 * DAY


def details_of(path) -> RecordingDetails:
    outcome = inspect_recording(str(path))
    assert outcome.details is not None, outcome.error
    return outcome.details


def marks(check):
    return {i.key: i.mark for i in check.items}


# --------------------------------------------------------------------------
# The model, on real recordings


def test_the_original_lists_what_identifies_the_patient(tmp_path):
    check = build_check(details_of(write_plain(tmp_path / "orig.mefd")))
    m = marks(check)
    assert m["patient"] is Mark.FOUND and m["location"] is Mark.FOUND
    assert m["notes"] is Mark.FOUND and m["descriptions"] is Mark.FOUND
    assert m["offset"] is Mark.EMPTY and m["zone"] is Mark.EMPTY
    assert m["private"] is Mark.UNKNOWN  # only a copy made by the anonymizer is known clean
    assert not check.made_by_anonymizer
    assert headline(check) == "Found 4 details that may identify the patient"
    patient = check.item("patient")
    assert set(patient.values) == {FIRST, LAST, PATIENT_ID}


def test_a_recording_from_another_program_with_empty_fields(tmp_path):
    details = details_of(write_foreign_like(tmp_path / "foreign.mefd"))
    assert details.history_groups()[0][0] is None  # no history: another program
    check = build_check(details)
    assert check.clean
    assert headline(check) == "No patient details found in this recording"
    assert all(i.mark in (Mark.EMPTY, Mark.UNKNOWN) for i in check.items)
    assert not check.made_by_anonymizer and not check.partly_anonymizer


def test_the_date_hiding_offset_and_the_time_zone_are_found(tmp_path):
    rto = set_time_offset(write_plain(tmp_path / "rto.mefd", notes=False), 5 * DAY)
    assert marks(build_check(details_of(rto)))["offset"] is Mark.FOUND
    zoned = write_with_time_zone(tmp_path / "zone.mefd")
    assert marks(build_check(details_of(zoned)))["zone"] is Mark.FOUND


def test_a_name_that_contains_a_patient_detail_is_found(tmp_path):
    path = write_plain(tmp_path / f"{LAST}_eeg.mefd", notes=False)
    check = build_check(details_of(path))
    assert check.item("name").mark is Mark.FOUND
    assert check.item("files").mark is Mark.FOUND


def _synthetic(**kw) -> RecordingDetails:
    history = History("1.4.0", "1.4.0", "anonymize", ("create", "anonymize"), 1)
    parts = (Part(0, T0, T0 + 10, 10, 1, history),)
    base = dict(
        location="S1.mefd",
        channels=(ChannelInfo("c", 256.0, "uV", 10, T0, T0 + 10, parts),),
        identity=(
            IdentityFact("subject_name_1", False),
            IdentityFact("session_name", True, ("S1",), 3),
            IdentityFact("anonymized_name", True, ("P-7",), 3),
        ),
    )
    base.update(kw)
    return RecordingDetails(**base)


def test_locked_fields_are_unknown_never_empty():
    details = _synthetic(
        identity=(IdentityFact("subject_name_1", None), IdentityFact("record_text", None)),
        gmt_offset=None,
        dst_set=None,
        time_offset_status="locked",
    )
    m = marks(build_check(details))
    assert m["patient"] is Mark.UNKNOWN and m["notes"] is Mark.UNKNOWN
    assert m["zone"] is Mark.UNKNOWN and m["offset"] is Mark.UNKNOWN


def test_made_by_the_anonymizer_needs_every_part_stamped():
    stamped = _synthetic()
    check = build_check(stamped)
    assert check.made_by_anonymizer and check.item("private").mark is Mark.EMPTY
    assert check.item("name").visible_value == "S1 (pseudonym: P-7)"
    plain = History("1.4.0", "1.4.0", "create", ("create",), 0)
    mixed = _synthetic(
        channels=(
            ChannelInfo(
                "c",
                256.0,
                "uV",
                20,
                T0,
                T0 + 20,
                (
                    Part(0, T0, T0 + 10, 10, 1, stamped.channels[0].parts[0].history),
                    Part(1, T0 + 10, T0 + 20, 10, 1, plain),
                ),
            ),
        )
    )
    check = build_check(mixed)
    assert not check.made_by_anonymizer and check.partly_anonymizer
    assert check.item("private").mark is Mark.UNKNOWN


def test_the_check_reads_no_file(tmp_path, monkeypatch):
    """Built from the details already read: no open, no stat."""
    details = details_of(write_plain(tmp_path / "orig.mefd"))
    calls = []
    for name, module in (("open", builtins), ("stat", os), ("scandir", os), ("listdir", os)):
        real = getattr(module, name)
        monkeypatch.setattr(module, name, lambda *a, _r=real, _n=name, **k: calls.append(_n))
    build_check(details)
    monkeypatch.undo()
    assert calls == []


# --------------------------------------------------------------------------
# The card in the Explorer


@pytest.fixture
def explorer(window):
    window._closing = True
    window.show()
    return window.explorer


def show_card(qtbot, explorer, path):
    explorer.open_for_privacy_check(str(path))
    qtbot.waitUntil(
        lambda: (
            explorer.current_entry() is not None
            and explorer.current_entry().details is not None
            and not explorer.current_entry().busy
        ),
        timeout=60_000,
    )
    assert explorer.tabs.currentWidget() is explorer.privacy
    return explorer.privacy


def test_card_on_the_original_masks_values_until_show(explorer, qtbot, tmp_path):
    card = show_card(qtbot, explorer, write_plain(tmp_path / "orig.mefd"))
    assert card.headline.text() == "Found 4 details that may identify the patient"
    assert card.headline.objectName() == "PrivacyBad"
    assert card.origin_text == NOT_MADE_BY_ANONYMIZER
    assert card.rows["Patient names and ID"][0] == "✗"
    assert FIRST not in card.found_list.text() and HIDDEN in card.found_list.text()
    assert all(FIRST not in text for _s, text in card.rows.values())
    card.button.click()  # Show: this recording only, shared with Patient details
    assert explorer.current_entry().revealed
    assert FIRST in card.found_list.text() and PATIENT_ID in card.rows["Patient names and ID"][1]
    assert "Dates as stored" in card.dates.text()
    assert not card.dates_note.isVisibleTo(card)


def test_card_on_a_foreign_recording(explorer, qtbot, tmp_path):
    card = show_card(qtbot, explorer, write_foreign_like(tmp_path / "foreign.mefd"))
    assert card.headline.text() == "No patient details found in this recording"
    assert card.headline.objectName() == "PrivacyGood"
    assert all(symbol in ("✓", "–") for symbol, _ in card.rows.values()), card.rows
    assert card.origin_text == (
        "This recording wasn't made by the anonymizer, so we can't confirm its dates were moved."
    )


@needs_anonymizer
def test_card_on_an_anonymized_copy(explorer, qtbot, tmp_path):
    src = write_plain(tmp_path / "orig.mefd")
    mef3io.anonymize_session(str(src), str(tmp_path / "S1.mefd"), SHIFT, pseudonym="P-7")
    card = show_card(qtbot, explorer, tmp_path / "S1.mefd")
    assert card.headline.text() == "No patient details found in this recording"
    assert card.origin_text == "Made by the anonymizer."
    assert card.dates_note.text() == MOVED_DATES
    assert "Moved dates: 2010-12-14 00:00:00" in card.dates.text()
    assert all(symbol == "✓" for symbol, _ in card.rows.values()), card.rows
    assert card.rows["The recording's name"][1].endswith("S1 (pseudonym: P-7)")
    assert card.rows["Text of notes and events"][1] == "Removed."
    assert card.rows["Other programs' private areas"][1] == "Cleared by the anonymizer."


@needs_anonymizer
def test_card_on_a_copy_that_kept_its_note_text(explorer, qtbot, tmp_path):
    src = write_plain(tmp_path / "orig.mefd")
    mef3io.anonymize_session(str(src), str(tmp_path / "K1.mefd"), SHIFT, text="keep")
    card = show_card(qtbot, explorer, tmp_path / "K1.mefd")
    symbol, text = card.rows["Text of notes and events"]
    assert symbol == "!" and text.startswith("Kept, as chosen")
    assert "not found automatically" in text
    # Kept text is a warning to read it, not a found patient detail.
    assert card.headline.text() == "No patient details found in this recording"
    assert "Note text was kept" in card.found_list.text()
    assert LAST not in text  # the patient's name was taken out of it, and it stays hidden


def test_privacy_tab_sits_next_to_patient_details(explorer):
    from mef3io_studio.explorer.page import TABS

    assert TABS.index("Privacy check") == TABS.index("Patient details") + 1
    assert Path(__file__).exists()


# --------------------------------------------------------------------------
# Review round 1


def _locked_details(**kw) -> RecordingDetails:
    """As the basic password leaves a recording: section 3 locked (names,
    location, time zone, the offset) and note text behind the full-access
    password; nothing found in what could be read."""
    plain = History("1.3.0", "1.3.0", "create", ("create",), 0)
    parts = (Part(0, T0, T0 + 10, 10, 1, plain),)
    base = dict(
        location="locked.mefd",
        channels=(ChannelInfo("c", 256.0, "uV", 10, T0, T0 + 10, parts),),
        identity=(
            IdentityFact("subject_name_1", None),
            IdentityFact("subject_id", None),
            IdentityFact("recording_location", None),
            IdentityFact("session_description", False),
            IdentityFact("record_text", None),
        ),
        gmt_offset=None,
        dst_set=None,
        time_offset_status="locked",
        access="basic",
    )
    base.update(kw)
    return RecordingDetails(**base)


def test_never_green_while_identity_rows_could_not_be_checked(tmp_path):
    from explorer_sessions import PASSWORD_BASIC, write_encrypted

    from mef3io_studio.model.privacycheck import UNCHECKED

    check = build_check(_locked_details())
    assert not check.found and check.unchecked
    assert check.state == "unknown" and not check.clean
    assert headline(check) == UNCHECKED
    # A real recording opened with the basic password: its locked rows are
    # never counted as empty either.
    outcome = inspect_recording(str(write_encrypted(tmp_path / "l.mefd")), PASSWORD_BASIC)
    real = build_check(outcome.details)
    assert {i.key for i in real.unchecked} >= {"patient", "location", "zone", "offset"}
    assert headline(real) != "No patient details found in this recording"


def test_card_with_unchecked_rows_is_not_green(explorer, qtbot):
    from mef3io_studio.explorer.state import RecordingEntry
    from mef3io_studio.model.recording import InspectOutcome

    details = _locked_details()
    entry = RecordingEntry(
        location="/nowhere/locked.mefd",
        outcome=InspectOutcome("/nowhere/locked.mefd", details=details),
    )
    explorer.model.add([entry])
    explorer._refresh_body()
    explorer.select_entry(entry)
    explorer.tabs.setCurrentWidget(explorer.privacy)
    card = explorer.privacy
    assert card.headline.objectName() == "PrivacyUnknown"
    assert card.headline.text().startswith("Some details could not be checked")
    assert "could not be checked" in card.found_list.text()


def test_appending_after_anonymizing_is_not_made_by_the_anonymizer():
    from mef3io_studio.model.privacycheck import CHANGED_AFTER

    appended = History("1.4.0", "1.4.0", "append", ("create", "anonymize", "append"), 2)
    details = _synthetic(
        channels=(
            ChannelInfo(
                "c", 256.0, "uV", 10, T0, T0 + 10, (Part(0, T0, T0 + 10, 10, 1, appended),)
            ),
        )
    )
    assert not details.made_by_anonymizer and details.changed_after_anonymizing
    check = build_check(details)
    assert not check.made_by_anonymizer and check.partly_anonymizer
    assert check.item("private").mark is Mark.UNKNOWN
    assert "changed afterwards" in CHANGED_AFTER


@needs_anonymizer
def test_card_on_a_copy_with_data_added_afterwards(explorer, qtbot, tmp_path):
    import numpy as np

    from mef3io_studio.model.privacycheck import CHANGED_AFTER

    src = write_plain(tmp_path / "orig.mefd", notes=False)
    copy = tmp_path / "S2.mefd"
    mef3io.anonymize_session(str(src), str(copy), SHIFT)
    with mef3io.Writer(str(copy)) as w:  # an append with a time from the real dates
        w.write("a1", np.zeros(256), T0 + SHIFT + 20 * 1_000_000, 256.0)
    card = show_card(qtbot, explorer, copy)
    assert card.origin_text == CHANGED_AFTER
    assert card.dates_note.text() == ""
