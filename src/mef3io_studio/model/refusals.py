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
        "Some notes cannot be cleaned safely",
        "The text of some notes and events still holds the patient's details in a form "
        f"the app cannot take out safely. {NOTHING} Go back and choose Remove for the "
        "text of notes and events.",
    ),
    technical("source-mid-operation"): _fixed(
        "This recording is in the middle of a change",
        "Renaming this recording was started and did not finish (the computer may have "
        f"stopped, or another program is still working on it). {NOTHING} Finish or undo "
        "the rename in the program that started it, then try again.",
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
