"""mef3io's refusals in plain words, by their stable codes.

Since mef3io 1.4 every refusal of ``anonymize_session`` (and every one
``mef3io.anonymize.check`` reports) carries a stable ``code``
(``mef3io.anonymize.REFUSAL_CODES``), as ``exc.code`` / ``Refusal.code``,
plus ``channel``, ``field`` and ``inspect_code`` details. The app translates
the code, never the message: every code has words here (a test checks that
none is missing), and anything without a code gets a generic message by the
kind of error.

Each message says what happened, what it means for the data (nothing was
written; the original is untouched) and what to do next
(docs/ui-language.md). Plain Python, no Qt.
"""

from __future__ import annotations

from collections.abc import Callable

from .errors import UserMessage
from .text import technical

NOTHING = "Nothing was written, and the original was not changed."
CHECK = "Use Check for problems in Explore to see more, and keep a copy of the original."
REPORT = "Try again; if it keeps happening, report it and include the technical details."

#: Codes that mean "the files could not be reached" (a drive or a share that
#: went away), worth trying again later.
UNREACHABLE = frozenset({"source-missing", "source-unreadable", "io-error", "write-failed"})


_IDENTITY = (
    "something that identifies the patient (their name, ID or the place where it was "
    "recorded) or the original recording's name"
)


def _name(_details: dict) -> str:
    return (
        f"The name of the copy contains {_IDENTITY}, so the copy would still identify the "
        f"patient. {NOTHING} Choose one that says nothing about the patient, such as a study "
        "code."
    )


def _pseudonym(_details: dict) -> str:
    return (
        f"The pseudonym contains {_IDENTITY}, so the copy would still identify the patient. "
        f"{NOTHING} Choose one that says nothing about the patient, such as a study code."
    )


def _channel(details: dict) -> str:
    channel = details.get("channel")
    which = f" ({channel})" if channel else ""
    return (
        f"A channel to leave out{which} is not in this recording. {NOTHING} Go back to the "
        "channel step and check the selection."
    )


def _fixed(title: str, text: str) -> Callable[[dict], tuple[str, str]]:
    return lambda _details: (title, text)


def _dynamic(title: str, text: Callable[[dict], str]) -> Callable[[dict], tuple[str, str]]:
    return lambda details: (title, text(details))


#: code -> words(details) -> (title, text). One entry per code of
#: ``mef3io.anonymize.REFUSAL_CODES``.
WORDS: dict[str, Callable[[dict], tuple[str, str]]] = {
    # arguments and options: the app's own mistakes, in plain words anyway
    technical("argument-type"): _fixed(
        "The copy could not be made", f"A choice was not valid. {NOTHING} {REPORT}"
    ),
    technical("clear-unknown"): _fixed(
        "The copy could not be made", f"A choice was not valid. {NOTHING} {REPORT}"
    ),
    technical("text-invalid"): _fixed(
        "The copy could not be made", f"A choice was not valid. {NOTHING} {REPORT}"
    ),
    technical("shift-zero"): _fixed(
        "The dates would not move",
        f"The new date is the recording's own date, so nothing would move. {NOTHING} Choose "
        "another new date.",
    ),
    technical("channel-selection-conflict"): _fixed(
        "The copy could not be made", f"The channel choice was not valid. {NOTHING} {REPORT}"
    ),
    technical("text-callback-missing"): _fixed(
        "The copy could not be made", f"A choice was not valid. {NOTHING} {REPORT}"
    ),
    technical("output-password-unpaired"): _fixed(
        "Type both passwords",
        f"A password-protected copy needs both passwords. {NOTHING} Go back to the passwords "
        "step and type both, or choose No password.",
    ),
    technical("output-passwords-equal"): _fixed(
        "The two passwords must be different",
        f"The basic and the full-access password are the same. {NOTHING} Go back to the "
        "passwords step and choose two different ones.",
    ),
    # where the copy goes
    technical("dst-not-mefd"): _fixed(
        "The copy's name is not valid",
        f"A copy must be a folder ending in .mefd. {NOTHING} Choose another name. {REPORT}",
    ),
    technical("dst-exists"): _fixed(
        "That name is already taken",
        f"A file or folder with the new name is already in the chosen folder. {NOTHING} Choose "
        "another name or folder.",
    ),
    technical("partial-exists"): _fixed(
        "An unfinished copy is in the way",
        "A folder ending in .partial, left by an earlier copy that did not finish, is next to "
        f"where this copy goes. {NOTHING} If no copy is running, delete that folder and try "
        "again, or choose another folder.",
    ),
    technical("source-missing"): _fixed(
        "The recording could not be found",
        f"The original recording is not where it was. {NOTHING} Check that the drive or "
        "network share is connected, then try again.",
    ),
    technical("dst-inside-src"): _fixed(
        "Choose a folder outside the recording",
        f"The copy would be inside the original recording. {NOTHING} Choose a folder that is "
        "not part of any recording.",
    ),
    # opening the original
    technical("source-unreadable"): _fixed(
        "The recording could not be read",
        f"The original recording could not be read from the disk. {NOTHING} Check that the "
        "drive or network share is connected, then try again.",
    ),
    technical("source-damaged"): _fixed(
        "Part of the recording is damaged",
        f"Part of the original recording is damaged, so it cannot be copied safely. {NOTHING} "
        f"{CHECK}",
    ),
    technical("source-crc"): _fixed(
        "Part of the recording is damaged",
        f"Part of the original recording did not pass its built-in check. {NOTHING} {CHECK}",
    ),
    technical("records-ambiguous"): _fixed(
        "The notes of this recording are unclear",
        "The recording holds more than one file of notes for the whole recording, so the app "
        f"cannot tell which is meant. {NOTHING} {CHECK}",
    ),
    technical("password-wrong"): _fixed(
        "The password did not open the recording",
        f"The password given did not open the original. {NOTHING} Check the password (it is "
        "case-sensitive) and try again.",
    ),
    technical("password-required"): _fixed(
        "This recording needs its full-access password",
        f"The original is password-protected. {NOTHING} Open it in Explore with its "
        "full-access password, then try again.",
    ),
    technical("password-level1-only"): _fixed(
        "This recording needs its full-access password",
        f"The basic password does not open the patient details, which anonymizing must "
        f"remove. {NOTHING} Open it in Explore with its full-access password, then try again.",
    ),
    # what the recording holds that stops a safe copy
    technical("video-channels"): _fixed(
        "This recording cannot be anonymized",
        f"It has video channels, which the app cannot copy. {NOTHING}",
    ),
    technical("unreadable-segments"): _fixed(
        "This recording cannot be anonymized safely",
        f"Some parts of it could not be read. {NOTHING} {CHECK}",
    ),
    technical("incomplete-segments"): _fixed(
        "This recording cannot be anonymized safely",
        f"Some parts of it are incomplete: files are missing. {NOTHING} {CHECK}",
    ),
    technical("unread-record-files"): _fixed(
        "This recording cannot be anonymized safely",
        f"Some of its notes and events could not be read. {NOTHING} {CHECK}",
    ),
    technical("unreadable-records"): _fixed(
        "This recording cannot be anonymized safely",
        f"Some of its notes and events could not be read. {NOTHING} {CHECK}",
    ),
    technical("parts-locked"): _fixed(
        "This recording needs its full-access password",
        f"Some parts of it stay locked with the password given. {NOTHING} Open it in Explore "
        "with its full-access password, then try again.",
    ),
    technical("channel-name-identity"): _dynamic(
        "A channel's name identifies the patient",
        lambda d: (
            "The name of a channel"
            + (f" ({d.get('channel')})" if d.get("channel") else "")
            + " contains "
            "something that identifies the patient (their name, ID or the place where it was "
            f"recorded), so the copy would still identify the patient. {NOTHING} Leave that "
            "channel out in the channel step, or remove the identifying part from its name "
            "first."
        ),
    ),
    technical("time-range-offset-disputed"): _fixed(
        "The part to keep cannot be placed safely",
        "The parts of this recording disagree about how the real dates are hidden in it, so "
        f"the part to keep cannot be placed in time. {NOTHING} {CHECK}",
    ),
    technical("rto-disputed"): _fixed(
        "This recording cannot be anonymized safely",
        "Its parts disagree about how the real dates are hidden in it, so its times are "
        f"unclear. {NOTHING} {CHECK}",
    ),
    technical("rto-unreadable"): _fixed(
        "This recording cannot be anonymized safely",
        "How the real dates are hidden in it could not be read, so its times are unclear. "
        f"{NOTHING} {CHECK}",
    ),
    technical("time-before-rto"): _fixed(
        "This recording cannot be anonymized safely",
        "Some of its times are stored in an old way that cannot be read without doubt. "
        f"{NOTHING} {CHECK}",
    ),
    technical("inspect-blocker"): _fixed(
        "This recording cannot be anonymized safely",
        f"Something in it stops a safe copy (see the technical details). {NOTHING} {CHECK}",
    ),
    # the channels
    technical("channel-unknown"): _dynamic(
        "A channel to leave out is not in this recording", _channel
    ),
    technical("no-channels-left"): _fixed(
        "No channel would be kept",
        f"{NOTHING} Go back to the channel step and keep at least one channel.",
    ),
    # the shift
    technical("shift-below-floor"): _fixed(
        "The new date is too close to the real one",
        "The dates must move by at least the length of the recording plus one day, so that no "
        f"moved date can be mistaken for a real one. {NOTHING} Choose a new date further away.",
    ),
    technical("shift-not-positive"): _fixed(
        "The new date is too early",
        f"The moved dates must stay after 1 January 1970. {NOTHING} Choose a later date.",
    ),
    # the copy's names
    technical("rename-name-mismatch"): _fixed(
        "The copy's name is not valid", f"{NOTHING} Choose another name. {REPORT}"
    ),
    technical("name-contains-identity"): _dynamic("Choose another name for the copy", _name),
    technical("pseudonym-contains-identity"): _dynamic("Choose another pseudonym", _pseudonym),
    technical("name-too-long"): _fixed(
        "The name is too long",
        f"The name of the copy or the pseudonym is too long to store. {NOTHING} Choose a "
        "shorter one.",
    ),
    # the recording's own layout
    technical("segment-folder-unnumbered"): _fixed(
        "This recording cannot be anonymized safely",
        f"The folders of some of its parts are named in an unexpected way. {NOTHING} {CHECK}",
    ),
    technical("segment-number-mismatch"): _fixed(
        "This recording cannot be anonymized safely",
        f"Some of its parts are numbered inconsistently. {NOTHING} {CHECK}",
    ),
    technical("segment-number-duplicate"): _fixed(
        "This recording cannot be anonymized safely",
        f"Two of its parts have the same number. {NOTHING} {CHECK}",
    ),
    technical("record-damaged"): _fixed(
        "Part of the recording is damaged",
        f"A note or event in it is damaged. {NOTHING} {CHECK}",
    ),
    technical("record-crc"): _fixed(
        "Part of the recording is damaged",
        f"A note or event in it did not pass its built-in check. {NOTHING} {CHECK}",
    ),
    technical("text-unremovable"): _fixed(
        "Some kept text cannot be cleaned safely",
        "Some text the copy would keep (notes and events, or the recording, channel and "
        "reference descriptions) still holds a name that must be taken out, in a form the "
        f"app cannot clean safely. {NOTHING} Go back and choose Remove the text, and tick "
        "Recording, channel and reference descriptions.",
    ),
    technical("source-mid-operation"): _fixed(
        "This recording is in the middle of a change",
        "Renaming this recording was started and did not finish (the computer may have "
        f"stopped, or another program is still working on it). {NOTHING} Wait until the "
        "other program has finished, or run the rename again to undo it, then try again.",
    ),
    # Choosing a part of the recording (a time window). The app does not offer
    # it yet: these can only be the app's own mistakes.
    technical("time-range-invalid"): _fixed(
        "The copy could not be made", f"A choice was not valid. {NOTHING} {REPORT}"
    ),
    technical("time-range-empty"): _fixed(
        "The chosen time holds no recording",
        f"There is no recording in the chosen time. {NOTHING} Choose another time.",
    ),
    technical("record-locked"): _fixed(
        "This recording needs its full-access password",
        "The text of some notes is behind the full-access password, and it must be read to be "
        f"removed. {NOTHING} Open it in Explore with its full-access password, then try again.",
    ),
    technical("record-index-mismatch"): _fixed(
        "Part of the recording is damaged",
        f"The list of its notes and events does not match the notes themselves. {NOTHING} {CHECK}",
    ),
    technical("id-number-time"): _fixed(
        "This recording cannot be anonymized safely",
        "A note in it stores a number that looks like a real date, which the copy cannot "
        f"move or remove safely. {NOTHING} {CHECK}",
    ),
    # the copy pass
    technical("block-crc"): _fixed(
        "Part of the recording is damaged",
        "Part of the recorded signal did not pass its built-in check, so it cannot be copied "
        "safely; nothing was written and the original was not changed. "
        f"{CHECK}",
    ),
    technical("data-damaged"): _fixed(
        "Part of the recording is damaged",
        "Part of the recorded signal is damaged, so it cannot be copied safely; nothing was "
        f"written and the original was not changed. {CHECK}",
    ),
    technical("verify-failed"): _fixed(
        "The copy did not pass its check",
        "The copy was compared with the original and something was not right, so the copy "
        f"was removed. The original was not changed. {REPORT}",
    ),
    technical("data-locked"): _fixed(
        "This recording needs its full-access password",
        f"Part of its signal is locked. {NOTHING} Open it in Explore with its full-access "
        "password, then try again.",
    ),
    technical("time-overflow"): _fixed(
        "The new date is too far away",
        f"A moved time would not fit in the file. {NOTHING} Choose a nearer new date.",
    ),
    technical("write-failed"): _fixed(
        "The copy could not be written to the chosen folder",
        "The app could not write to the chosen folder. Anything it had started was removed, "
        "and the original was not changed. Check that the folder still exists, that you may "
        "write there and that the drive is connected, then try again.",
    ),
    # generic codes
    technical("io-error"): _fixed(
        "The files could not be reached",
        "The original or the chosen folder could not be read or written. Anything the app "
        "had started was removed, and the original was not changed. Check that the drive or "
        "network share is connected, then try again.",
    ),
    technical("write-conflict"): _fixed(
        "This recording cannot be copied safely",
        f"It cannot be copied without risking a wrong date in the copy. {NOTHING} {CHECK}",
    ),
    technical("invalid-argument"): _fixed(
        "The copy could not be made",
        f"{NOTHING} Check the choices in the earlier steps and try again; if it keeps "
        "happening, report it and include the technical details.",
    ),
}


def words_for(code: str, details: dict | None = None) -> tuple[str, str] | None:
    """(title, text) for a refusal code, or None for an unknown code."""
    words = WORDS.get(code)
    return words(details or {}) if words is not None else None


def message_for(code: str, details: dict | None, detail: str = "") -> UserMessage | None:
    found = words_for(code, details)
    if found is None:
        return None
    return UserMessage(found[0], found[1], detail)


# --------------------------------------------------------------------------
# Joining or trimming recordings (``mef3io.transform_session`` with a time
# window, and ``mef3io.merge_sessions``). Their refusal codes overlap with
# the anonymizer's, but the anonymizer's words talk about anonymizing and
# its wizard's steps, so the joiner has its own table: one entry per code of
# ``mef3io.transform.REFUSAL_CODES`` and ``mef3io.merge.REFUSAL_CODES``
# (a test checks that none is missing).

NOTHING_JOINED = "Nothing was written, and the original recordings were not changed."
JOIN_CHECK = (
    "Use Check for problems in Explore to see more about that recording, and keep a copy of it."
)
_JOIN_FAILED = "The new recording could not be made"
_JOIN_UNSAFE = "This recording cannot be used here safely"


def _join_channel(details: dict) -> str:
    channel = details.get("channel")
    which = f" ({channel})" if channel else ""
    return (
        f"A channel to leave out{which} is not in any of the chosen recordings. "
        f"{NOTHING_JOINED} Go back to the channel step and check the selection."
    )


def _unsafe(text: str) -> Callable[[dict], tuple[str, str]]:
    return _fixed(_JOIN_UNSAFE, f"{text} {NOTHING_JOINED} {JOIN_CHECK}")


def _damaged(text: str) -> Callable[[dict], tuple[str, str]]:
    return _fixed("Part of a recording is damaged", f"{text} {NOTHING_JOINED} {JOIN_CHECK}")


def _app_mistake(what: str = "A choice was not valid.") -> Callable[[dict], tuple[str, str]]:
    return _fixed(_JOIN_FAILED, f"{what} {NOTHING_JOINED} {REPORT}")


def _needs_full_access(text: str) -> Callable[[dict], tuple[str, str]]:
    return _fixed(
        "This recording needs its full-access password",
        f"{text} {NOTHING_JOINED} Open it in Explore with its full-access password, then try "
        "again.",
    )


_WRITE_FAILED = _fixed(
    "The new recording could not be written to the chosen folder",
    "The app could not write to the chosen folder. Anything it had started was removed, and "
    "the original recordings were not changed. Check that the folder still exists, that you "
    "may write there and that the drive is connected, then try again.",
)

#: code -> words(details) -> (title, text), for joining or trimming.
JOIN_WORDS: dict[str, Callable[[dict], tuple[str, str]]] = {
    # arguments: the app's own mistakes, in plain words anyway
    technical("argument-type"): _app_mistake(),
    technical("channel-selection-conflict"): _app_mistake("The channel choice was not valid."),
    technical("time-range-invalid"): _app_mistake("The part to keep was not valid."),
    technical("rename-name-mismatch"): _app_mistake("The new name was not valid."),
    technical("invalid-argument"): _app_mistake(),
    technical("metadata-unknown"): _app_mistake(),
    technical("metadata-too-long"): _app_mistake(),
    technical("metadata-invalid"): _app_mistake(),
    technical("metadata-channel-unknown"): _app_mistake(),
    technical("merge-too-few"): _app_mistake("Joining needs at least two recordings."),
    technical("merge-passwords-count"): _app_mistake(),
    technical("merge-time-range"): _app_mistake(),
    technical("merge-rename"): _app_mistake(),
    technical("output-password-unpaired"): _fixed(
        "Type both passwords",
        f"A password-protected recording needs both passwords. {NOTHING_JOINED} Type both, "
        "or choose No password.",
    ),
    technical("output-passwords-equal"): _fixed(
        "The two passwords must be different",
        f"The basic and the full-access password are the same. {NOTHING_JOINED} Choose two "
        "different ones.",
    ),
    # where the new recording goes
    technical("dst-not-mefd"): _app_mistake("The new name was not valid."),
    technical("dst-exists"): _fixed(
        "That name is already taken",
        f"A file or folder with the new name is already in the chosen folder. {NOTHING_JOINED} "
        "Choose another name or folder.",
    ),
    technical("partial-exists"): _fixed(
        "An unfinished recording is in the way",
        "A folder ending in .partial, left by an earlier run that did not finish, is next to "
        f"where the new recording goes. {NOTHING_JOINED} If nothing else is running, delete "
        "that folder and try again, or choose another folder.",
    ),
    technical("dst-inside-src"): _fixed(
        "Choose a folder outside the recordings",
        f"The new recording would be inside one of the original recordings. {NOTHING_JOINED} "
        "Choose a folder that is not part of any recording.",
    ),
    technical("source-missing"): _fixed(
        "A recording could not be found",
        f"One of the recordings is not where it was. {NOTHING_JOINED} Check that the drive or "
        "network share is connected, then try again.",
    ),
    technical("source-mid-operation"): _fixed(
        "A recording is in the middle of a change",
        "Renaming one of the recordings was started and did not finish (the computer may have "
        f"stopped, or another program is still working on it). {NOTHING_JOINED} Wait until "
        "the other program has finished, then try again.",
    ),
    # opening the originals
    technical("source-unreadable"): _fixed(
        "A recording could not be read",
        f"One of the recordings could not be read from the disk. {NOTHING_JOINED} Check that "
        "the drive or network share is connected, then try again.",
    ),
    technical("source-damaged"): _damaged("Part of a recording is damaged, so it cannot be used."),
    technical("source-crc"): _damaged("Part of a recording did not pass its built-in check."),
    technical("records-ambiguous"): _fixed(
        "The notes of a recording are unclear",
        "A recording holds more than one file of notes for the whole recording, so the app "
        f"cannot tell which is meant. {NOTHING_JOINED} {JOIN_CHECK}",
    ),
    technical("password-wrong"): _fixed(
        "The password did not open a recording",
        f"The password given did not open one of the recordings. {NOTHING_JOINED} Check the "
        "password (it is case-sensitive) and try again.",
    ),
    technical("password-required"): _needs_full_access("A recording is password-protected."),
    technical("password-level1-only"): _needs_full_access(
        "The basic password does not open everything that must be copied."
    ),
    technical("parts-locked"): _needs_full_access(
        "Some parts of a recording stay locked with the password given."
    ),
    technical("record-locked"): _needs_full_access(
        "The text of some notes is behind the full-access password."
    ),
    technical("data-locked"): _needs_full_access("Part of the signal is locked."),
    # what a recording holds that stops a safe copy
    technical("video-channels"): _fixed(
        _JOIN_UNSAFE, f"It has video channels, which the app cannot copy. {NOTHING_JOINED}"
    ),
    technical("unreadable-segments"): _unsafe("Some parts of a recording could not be read."),
    technical("incomplete-segments"): _unsafe(
        "Some parts of a recording are incomplete: files are missing."
    ),
    technical("unread-record-files"): _unsafe(
        "Some notes and events of a recording could not be read."
    ),
    technical("unreadable-records"): _unsafe(
        "Some notes and events of a recording could not be read."
    ),
    technical("inspect-blocker"): _unsafe(
        "Something in a recording stops a safe copy (see the technical details)."
    ),
    technical("segment-folder-unnumbered"): _unsafe(
        "The folders of some parts of a recording are named in an unexpected way."
    ),
    technical("segment-number-mismatch"): _unsafe(
        "Some parts of a recording are numbered inconsistently."
    ),
    technical("segment-number-duplicate"): _unsafe(
        "Two parts of a recording have the same number."
    ),
    technical("record-damaged"): _damaged("A note or event in a recording is damaged."),
    technical("record-crc"): _damaged(
        "A note or event in a recording did not pass its built-in check."
    ),
    technical("record-index-mismatch"): _damaged(
        "The list of notes and events of a recording does not match the notes themselves."
    ),
    technical("block-crc"): _damaged(
        "Part of the recorded signal did not pass its built-in check."
    ),
    technical("data-damaged"): _damaged("Part of the recorded signal is damaged."),
    # the channels and the time window
    technical("channel-unknown"): _dynamic(
        "A channel to leave out is not in the recordings", _join_channel
    ),
    technical("no-channels-left"): _fixed(
        "No channel would be kept",
        f"{NOTHING_JOINED} Go back to the channel step and keep at least one channel.",
    ),
    technical("time-range-empty"): _fixed(
        "The chosen part holds no recording",
        f"There is no recorded signal in the part chosen to keep. {NOTHING_JOINED} Choose "
        "another part.",
    ),
    technical("name-too-long"): _fixed(
        "The name is too long",
        f"The new name is too long to store. {NOTHING_JOINED} Choose a shorter one.",
    ),
    # joining
    technical("merge-overlap"): _fixed(
        "The kept parts overlap in time",
        "Two of the recordings hold signal of the same channel at the same moment, so they "
        f"cannot be joined into one. {NOTHING_JOINED} Keep less of one of them, so that the "
        "parts follow each other.",
    ),
    technical("merge-rate-differs"): _fixed(
        "The sampling rates do not match",
        "A channel was recorded at a different sampling rate in two of the recordings, and "
        f"one channel cannot change its rate. {NOTHING_JOINED} Leave that channel out, or "
        "join only recordings made with the same settings.",
    ),
    technical("merge-rto-differs"): _fixed(
        "The recordings hide their dates differently",
        "The recordings use different settings for hiding their real dates, so their times "
        f"cannot be put on one timeline. {NOTHING_JOINED} {JOIN_CHECK}",
    ),
    technical("merge-identity-differs"): _fixed(
        "The recordings may be of different patients",
        "The patient details (names, ID or the place where it was recorded) differ between "
        f"the recordings. {NOTHING_JOINED} Join only recordings of the same patient; compare "
        "their patient details in Explore.",
    ),
    # writing and checking the result
    technical("verify-failed"): _fixed(
        "The new recording did not pass its check",
        "The new recording was compared with the originals and something was not right, so it "
        f"was removed. The original recordings were not changed. {REPORT}",
    ),
    technical("time-range-offset-disputed"): _unsafe(
        "The parts of a recording disagree about how the real dates are hidden in it, so the "
        "part to keep cannot be placed in time."
    ),
    technical("write-failed"): _WRITE_FAILED,
    technical("io-error"): _fixed(
        "The files could not be reached",
        "A recording or the chosen folder could not be read or written. Anything the app had "
        "started was removed, and the original recordings were not changed. Check that the "
        "drive or network share is connected, then try again.",
    ),
    technical("write-conflict"): _unsafe(
        "A recording cannot be copied without risking a wrong time in the new one."
    ),
}


def join_message_for(code: str, details: dict | None, detail: str = "") -> UserMessage | None:
    """:func:`message_for` with the joiner's words."""
    words = JOIN_WORDS.get(code)
    if words is None:
        return None
    title, text = words(details or {})
    return UserMessage(title, text, detail)
