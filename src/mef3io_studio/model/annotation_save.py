"""Saving the draft's new notes and events, and taking out or changing stored ones: the
only place (with ``viewer_source``) where the viewer's edit mode calls the library.

New notes are only ever APPENDED (``Writer.write_annotations`` never replaces or deletes a
stored one), so an interrupted save cannot damage what was there; the library refuses a
torn notes file until it has been fixed, and this module says so in plain words. A STORED
note is taken out, or replaced by a changed one, with ``mef3io.remove_annotations`` (mef3io
2.4; the section at the end of this module): in place, crash-safe in the library, and it
cannot be undone.

Passwords: the library encrypts a notes file only when it is given the
basic password, so a recording that has passwords is written with BOTH
(checked first, before anything is written); one without gets none. A pair is
never guessed or tried in turn.

Before anything is written for a recording its folder must still exist and
still be the recording that was opened (compared by its channels and times),
so the library can never create a new recording where one was moved.

Runs on a background task, never on the GUI thread. No paths, names or note
text in anything that can reach a log.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from . import timefmt
from .annotation_draft import SavePlan
from .errors import UserMessage, describe_error
from .text import technical

BOTH_PASSWORDS = "To add notes to a password-protected recording, both its passwords are needed."


class SaveFailed(Exception):
    """A save stopped. ``user_message`` is the plain-language text."""

    def __init__(self, user_message: UserMessage):
        super().__init__("save failed")
        self.user_message = user_message


def _added_text(data, added: dict[int, int]) -> str:
    """What was already added, by recording letter and count."""
    if not added:
        return " Nothing was added to any recording."
    parts = [
        f"recording {data.recordings[i].letter}: "
        f"{timefmt.format_count(n, 'note or event', 'notes and events')}"
        for i, n in sorted(added.items())
    ]
    return (
        " Already added (this cannot be undone in place, and these are no longer in the "
        "draft): " + "; ".join(parts) + "."
    )


def _plain(exc: BaseException, letter: str, data, added: dict[int, int]) -> SaveFailed:
    import mef3io

    text = str(exc).lower()
    done = _added_text(data, added)
    if isinstance(exc, mef3io.PasswordError) or "no password" in text:
        msg = UserMessage(
            "These notes need the full-access password",
            f"Recording {letter} is protected, and the passwords given do not allow "
            f"adding notes to it.{done}",
        )
    elif "recover_session" in text or "partial record" in text or "interrupted" in text:
        msg = UserMessage(
            "This recording's notes file was left unfinished",
            f"The notes file of recording {letter} was left unfinished, for example by a "
            "power cut, so notes cannot be added to it until it has been fixed. The app "
            "cannot fix it yet: ask someone who works with mef3io to run its fixing "
            f"function on this recording. The notes already stored were not changed.{done}",
            "mef3io.recover_session(path, apply=True)",
        )
    else:
        base = describe_error(exc)
        msg = UserMessage(base.title, f"{base.text}{done}", base.detail)
    return SaveFailed(msg)


def _refuse(
    title: str, text: str, tail: str = " Nothing was added to any recording."
) -> SaveFailed:
    return SaveFailed(UserMessage(title, text + tail))


BOTH_PASSWORDS_REMOVE = (
    "To change the notes of a password-protected recording, both its passwords are needed."
)
_NOTHING_REMOVED = " Nothing was removed."


def check_same(data, index: int, tail: str = " Nothing was added to any recording."):
    """Re-read recording ``index`` and return its summary; raises
    :class:`SaveFailed` when it is gone or is not the one that was opened."""
    import mef3io

    from .viewer_source import fingerprint_of

    letter = data.recordings[index].letter
    path = data.location_of(index)
    data.read_facts()  # once, here on the task's thread (a no-op when already read)
    opened = data.facts(index)
    if not os.path.isdir(path) or opened is None:
        raise _refuse(
            "This recording is not where it was opened",
            f"Recording {letter} cannot be found at the place it was opened from. It may "
            "have been moved, renamed or removed. Close this window and open the "
            "recording again.",
            tail,
        )
    held = data.password_of(index)
    try:
        now = mef3io.inspect(path, password=held)
    except Exception as exc:  # noqa: BLE001
        raise _refuse(
            "This recording could not be checked",
            f"Recording {letter} could not be read again before saving "
            f"({describe_error(exc).title}).",
            tail,
        ) from exc
    if fingerprint_of(now) != opened[2]:
        raise _refuse(
            "This recording has changed",
            f"Recording {letter} is not the same as when it was opened. Close this window "
            "and open it again.",
            tail,
        )
    return now


def signal_snapshot(path: str) -> dict[str, tuple[int, int]]:
    """The files that hold a recording's SIGNAL, as ``name -> (size, mtime_ns)``, from ONE
    directory walk (``os.scandir``: names and file information, no file is opened or read).

    They are found by where they sit, not by what they are called: a recording is a folder of
    channel folders, and a channel folder holds one folder per segment with the signal in it;
    the notes of the whole recording lie beside the channel folders and the notes of one channel
    inside its own folder, one level above the segments. A save appends notes only, so the
    files below the channel folders must be the same before and after it; a difference (or a
    file that appeared or went) means they may not be. Raises ``OSError`` when the folder
    cannot be walked."""
    out: dict[str, tuple[int, int]] = {}
    pending = [("", path, 0)]  # name prefix, folder, how deep below the recording folder
    while pending:
        prefix, folder, depth = pending.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                name = f"{prefix}{entry.name}"
                if entry.is_dir(follow_symlinks=False):
                    pending.append((name + "/", entry.path, depth + 1))
                elif depth >= 2:  # below the channel folders: the signal, never the notes
                    st = entry.stat(follow_symlinks=False)
                    out[name] = (int(st.st_size), int(st.st_mtime_ns))
    return out


def prepare(
    data, index: int, other_password: str = "", for_removal: bool = False
) -> tuple[str, str]:
    """Check recording ``index`` and return the ``(basic, full-access)``
    passwords to write with (``("", "")`` for a recording without any).
    Raises :class:`SaveFailed` (nothing has been written) when the recording
    moved or changed, or the passwords are missing or wrong. ``for_removal``:
    the words are those of taking notes out ("Nothing was removed")."""
    import mef3io

    letter = data.recordings[index].letter
    path = data.location_of(index)
    held = data.password_of(index)
    tail = _NOTHING_REMOVED if for_removal else " Nothing was added to any recording."
    both = BOTH_PASSWORDS_REMOVE if for_removal else BOTH_PASSWORDS
    now = check_same(data, index, tail)
    if not now.encryption["encrypted"]:
        return "", ""
    if not held or not other_password:
        raise _refuse("Both passwords are needed", f"Recording {letter}. {both}", tail)
    access = str(now.encryption["access"])
    if access == "full":
        level2, level1 = held, other_password
    elif access == "level-1":
        level1, level2 = held, other_password
    else:
        raise _refuse("Both passwords are needed", f"Recording {letter}. {both}", tail)
    try:
        ok = (
            str(mef3io.inspect(path, password=level1).encryption["access"]) == "level-1"
            and str(mef3io.inspect(path, password=level2).encryption["access"]) == "full"
        )
    except Exception:  # noqa: BLE001 - a wrong password raises
        ok = False
    if not ok:
        raise _refuse(
            "The passwords do not fit",
            f"The two passwords given do not both fit recording {letter}: one must be its "
            "basic password and the other its full-access password.",
            tail,
        )
    return level1, level2


@dataclass
class WriteResult:
    #: Indexes of the recordings that got at least one note.
    written: list[int] = field(default_factory=list)
    #: The draft items that were appended (their uids), exactly.
    uids: set[int] = field(default_factory=set)
    error: UserMessage | None = None
    stopped: bool = False
    #: Per recording that was written to: whether its signal files (see
    #: :func:`signal_snapshot`) are exactly as they were just before the write. A recording
    #: missing here (or False) is not known to be unchanged.
    signals_same: dict[int, bool] = field(default_factory=dict)


def write_plan(
    data,
    plan: SavePlan,
    extra_passwords: dict[int, str] | None = None,
    check_cancelled: Callable[[], None] = lambda: None,
    report: Callable[[int, int], None] = lambda _done, _total: None,
    register: Callable[..., None] = lambda *_values: None,
) -> WriteResult:
    """Append every recording's share of ``plan``. Every recording is checked
    first (nothing is written when any check fails). Then recording by
    recording, level by level: it stops at the first failure or stop, and the
    result lists exactly the items that were appended, so the draft can drop
    those and no others."""
    import mef3io

    from .cancel import is_stop

    result = WriteResult()
    extra = extra_passwords or {}
    # Typed passwords reach the log filter before any library call (task thread).
    # ``register`` is the app's register_identity (model/ cannot import common/).
    register(*(p for p in extra.values() if p))
    pairs: dict[int, tuple[str, str]] = {}
    try:
        for index in sorted(plan.by_recording):
            pairs[index] = prepare(data, index, extra.get(index, ""))
    except SaveFailed as exc:
        result.error = exc.user_message
        return result
    total, done = plan.count, 0
    added: dict[int, int] = {}
    for index in sorted(plan.by_recording):
        share = plan.by_recording[index]
        letter = data.recordings[index].letter
        path = data.location_of(index)
        p1, p2 = pairs[index]
        steps = []
        if share.session:
            steps.append((share.session, None))
        steps += [(share.channels[c], c) for c in sorted(share.channels)]
        try:  # it may have moved since the up-front check: look again before writing
            check_same(data, index)
        except SaveFailed as exc:
            result.error = UserMessage(
                exc.user_message.title,
                exc.user_message.text.replace(
                    " Nothing was added to any recording.", _added_text(data, added)
                ),
                exc.user_message.detail,
            )
            return result
        data.release_files()  # no signal file is held open while this recording is written to
        try:  # what the signal files are right before the write (names, sizes, times only)
            before = signal_snapshot(path)
        except OSError:
            before = None
        try:
            for items, channel in steps:
                try:
                    check_cancelled()
                    with mef3io.Writer(path, overwrite=False, password1=p1, password2=p2) as writer:
                        writer.write_annotations([i.record() for i in items], channel=channel)
                except BaseException as exc:  # noqa: BLE001 - every library error becomes a plain one
                    if is_stop(exc):
                        result.stopped = True
                        result.error = UserMessage(
                            "Stopped",
                            "The save was stopped."
                            + _added_text(data, added)
                            + " Everything not yet added remains in the draft.",
                        )
                    elif isinstance(exc, Exception):
                        result.error = _plain(exc, letter, data, added).user_message
                    else:
                        raise
                    return result
                result.uids.update(i.uid for i in items)
                added[index] = added.get(index, 0) + len(items)
                if index not in result.written:
                    result.written.append(index)
                done += len(items)
                report(done, total)
        finally:  # right after the write (or the failed one): the same files, or not
            if before is not None and index in result.written:
                try:
                    result.signals_same[index] = signal_snapshot(path) == before
                except OSError:
                    result.signals_same[index] = False
    return result


# --- taking out (and changing) a STORED note: the ONE place the library's removal is called ---
#
# ``mef3io.remove_annotations`` (mef3io 2.4) rewrites the notes files of the levels involved and
# nothing else: the signal and the details are never opened for writing. A removed note is
# gone: there is no undo. It selects by CONTENT (time, type, level, channel and every body
# field), so the stored records are read back from the open data here (on the task's thread) and
# handed over as they are. Several recordings go one after the other, each in ONE library call
# (all or nothing for that recording); the result says exactly which were done.
#
# A note stored twice (identical copies) cannot be told apart: the call names how many copies
# there are (``expect``), and "remove one" is "remove all copies and add the others back" in
# the same call, so nothing is ever half-done. Changing a stored note's text is "remove it and
# add the new one" in the same call. The library's switch that skips its check for a program
# that is still editing is never used: a note edit that another program is still making is
# the library's refusal to give, not ours to override.


@dataclass(frozen=True)
class StoredChange:
    """One stored note to take out, perhaps in favour of a new version."""

    #: The stored note (a ``viewer_marks.Mark``).
    mark: object
    #: How many identical stored copies the viewer shows (1 = a single one).
    copies: int = 1
    #: How many of them go (1..copies). ``copies`` = all of them.
    remove: int = 1
    #: The note that takes its place (an edited text), as ``DraftItem.record()`` with the
    #: channel it is stored under; None = nothing replaces it.
    replacement: dict | None = None
    #: The draft item behind the replacement (it leaves the draft when this is done).
    uid: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.remove <= max(1, self.copies):
            raise ValueError("remove must be between 1 and the number of copies")


@dataclass
class StoredResult:
    #: Indexes of the recordings whose notes were rewritten.
    written: list[int] = field(default_factory=list)
    #: Net change in the number of notes per recording (added - removed), for the read-back.
    net: dict[int, int] = field(default_factory=dict)
    removed: int = 0
    added: int = 0
    #: The draft items whose replacement was stored.
    uids: set[int] = field(default_factory=set)
    #: The stored notes (marks) that were taken out.
    marks: list = field(default_factory=list)
    error: UserMessage | None = None
    stopped: bool = False
    #: Per rewritten recording: its signal files are exactly as they were just before.
    signals_same: dict[int, bool] = field(default_factory=dict)
    #: Per rewritten recording: (added, changed, removed) that are now in its file.
    counts: dict[int, tuple[int, int, int]] = field(default_factory=dict)
    #: The file held another number of notes than asked: what it holds is to be read again.
    unsure: bool = False


#: Types the library's ``add`` can store COMPLETELY from what the app holds (time, text,
#: length): a copy that is added back (one of several identical copies removed) is then
#: identical to the one that stays. Any other type (Seiz, Epoc, Curs ...) has body fields
#: ``add`` cannot write, so it can only be removed with ALL its copies.
COPYABLE_TYPES = ("Note", "EDFA", "SyLg")
#: Types the editor can write back unchanged in kind: a note or an event with a length.
EDITABLE_TYPES = ("Note", "EDFA")
#: Said when a stored note of another kind is asked to be changed (it can still be removed).
EDIT_TYPE_REASON = (
    "This kind of note can only be removed, not changed, in this version: changing it would "
    "turn it into a plain note and lose what it holds."
)


def can_edit_stored(mark) -> str:
    """ "" when the stored note ``mark`` can be changed here, else the plain reason why not.
    A Note, or an EDFA that has a length; any other kind would lose its type or its fields."""
    if mark.code == "Note" or (mark.code == "EDFA" and mark.end is not None):
        return ""
    return EDIT_TYPE_REASON


def replacement_record(mark, item) -> dict:
    """What replaces the stored note ``mark`` when the draft's ``item`` is saved: the same
    kind of record (a Note stays a Note unless a length was given, an EDFA stays an EDFA
    unless its length was taken away: the user's own choice), at the item's time and channel.
    The text is the stored text AS IT IS when it was not edited (a trailing blank survives
    a change of time alone)."""
    from . import annotation_draft as ad

    unchanged = ad.split_text(mark.text) == (item.label, item.text)
    rec = {
        "type": item.code,
        "time": int(item.time),
        "text": mark.text if unchanged else item.stored_text,
    }
    if item.is_span:
        rec["duration"] = int(item.end - item.time)
    if item.channel:
        rec["channel"] = item.channel
    return rec


def mark_key(mark) -> tuple:
    """What makes a stored note THIS note: its recording, time, type, level, channel, text and
    length (the same note in two recordings is two notes, one of each)."""
    return (mark.recording, mark.time, mark.code, mark.level, mark.channel, mark.text, mark.end)


def record_key(rec: dict) -> tuple:
    """:func:`mark_key` of a stored record as the library returns it."""
    from . import viewer_marks as vm

    text = rec.get("text")
    return (
        int(rec.get("recording", 0)),
        int(rec["time"]),
        str(rec.get("type") or ""),
        str(rec.get("level") or ""),
        rec.get("channel"),
        text if isinstance(text, str) else "",
        vm.mark_end(rec),
    )


#: Plain words for every refusal the library can give here, by its stable ``code``:
#: ``(title, what happened and what it means, what to do)``. ``{r}`` is the recording's name
#: ("Recording A"). Never the library's own sentence (it names paths and note text): that
#: goes behind "Technical details". Each text says what became of the recording.
#: Every text carries this token where it says what became of the notes; the words are made
#: by :func:`describe_removal_error` (nothing removed, or exactly what was and was not done).
_UNCHANGED = "{outcome}"
_RESCUE = technical("python -m mef3io recover --apply <folder>")
_REOPEN = "Close this window, open the recording again and try once more."
_ASK = "Ask someone who works with mef3io to look at this recording."
_NOTE_GONE = (
    "The notes stored in {r} are not what the viewer showed, so this one was not found there. "
    f"{_UNCHANGED} Another program may have changed them. {_REOPEN}"
)
_BAD_MATCH = (
    "The note could not be matched with what {r} stores. "
    f"{_UNCHANGED} {_REOPEN} If it happens again, report it with the technical details."
)
_DAMAGED = (
    "The notes file of {r} cannot be read in full, so no note was changed. "
    f'{_UNCHANGED} Use "Check for problems" in Explore to look at it; {_ASK[0].lower()}{_ASK[1:]}'
)
_LEFT_OVER = (
    "A program was changing the notes and events of {r} and was interrupted (or is still "
    "working on it), so they cannot be changed until that is settled. "
    f"{_UNCHANGED} If no program is still working on it, it must be rescued first: this app "
    "version cannot do that yet. Ask whoever looks after the recording's files to run "
    f"mef3io's rescue on it ({_RESCUE}; without the apply option it only reports). The "
    "rescue either finishes the change or puts the earlier notes and events back, so the "
    "change may turn out not to have been made. Do not remove or move any file next to the "
    "recording first."
)
_ENCRYPTION = (
    "The new note does not fit the password protection of {r} (its notes are stored "
    f"differently from what the new one needs). {_UNCHANGED} Add it as a new note instead, "
    "or ask someone who works with mef3io to look at this recording."
)
REMOVAL_WORDS: dict[str, tuple[str, str]] = {
    "no-match": ("This note is not in the recording any more", _NOTE_GONE),
    "count-mismatch": ("The recording holds fewer copies of this note", _NOTE_GONE),
    "ambiguous": (
        "This note is stored more than once",
        "{r} holds more identical copies of this note than the viewer showed. "
        f"{_UNCHANGED} {_REOPEN}",
    ),
    "selectors-overlap": (
        "Two choices point at the same note",
        f"Two of the notes chosen in {{r}} are the same stored note. {_UNCHANGED} Choose them "
        "again, one at a time.",
    ),
    "selector-incomplete": ("This note could not be matched", _BAD_MATCH),
    "selector-unknown-field": ("This note could not be matched", _BAD_MATCH),
    "bad-selector": ("This note could not be matched", _BAD_MATCH),
    "nothing-to-do": (
        "There is nothing to change",
        f"No note was chosen, or the new note is the one already stored in {{r}}. {_UNCHANGED}",
    ),
    "unknown-channel": (
        "This note's channel is not in the recording",
        f"{{r}} has no channel of that name. {_UNCHANGED} {_REOPEN}",
    ),
    technical("unknown-segment"): (
        "This note's part of the recording is not there",
        f"{{r}} has no such part. {_UNCHANGED} {_REOPEN}",
    ),
    "add-level-missing": (
        "The new note cannot be stored there",
        "{r} has no notes of that kind yet to put the new note beside (for example for that "
        f"channel). {_UNCHANGED} Keep the new note on the channel the old one was on, or add it "
        "as a new note instead.",
    ),
    "add-time": (
        "The new time cannot be stored",
        f"The time chosen cannot be stored in {{r}}. {_UNCHANGED} Choose a time inside the "
        "recording.",
    ),
    "offset-unknown": (
        "The dates of this recording are not certain",
        "The dates of {r} are disputed or unknown, so notes cannot be changed in it safely. "
        f"{_UNCHANGED} Notes can still be added.",
    ),
    "password": (
        "The full-access password is needed",
        "{r} is password-protected and the passwords given do not allow changing its notes. "
        f"{_UNCHANGED} Give both its passwords: the basic one and the full-access one.",
    ),
    technical("record-locked"): (
        "The full-access password is needed",
        "Some notes of {r} stay locked with the passwords given. "
        f"{_UNCHANGED} Give both its passwords: the basic one and the full-access one.",
    ),
    technical("record-torn"): ("The notes of this recording are damaged", _DAMAGED),
    technical("record-damaged"): ("The notes of this recording are damaged", _DAMAGED),
    technical("record-crc"): ("The notes of this recording are damaged", _DAMAGED),
    technical("record-index-mismatch"): ("The notes of this recording are damaged", _DAMAGED),
    technical("record-index-missing"): ("The notes of this recording are damaged", _DAMAGED),
    technical("ambiguous-session-records"): ("The notes of this recording are damaged", _DAMAGED),
    "edit-unsettled": (
        "The notes and events were being changed when a program stopped",
        _LEFT_OVER,
    ),
    "edit-interrupted": (
        "The notes and events were being changed when a program stopped",
        _LEFT_OVER,
    ),
    "mid-operation": ("The notes and events were being changed when a program stopped", _LEFT_OVER),
    technical("edit-journal-inconsistent"): (
        "The notes and events were being changed when a program stopped",
        _LEFT_OVER,
    ),
    technical("records-encryption-mismatch"): (
        "The new note does not fit the password protection",
        _ENCRYPTION,
    ),
    technical("verify-failed"): (
        "The changed notes could not be checked",
        "The notes of {r} were rewritten, but the check of the result failed, so the change "
        f"was not kept. {_UNCHANGED} {_REOPEN}",
    ),
    "copy-type": (
        "Only all copies of this note can be removed",
        "This note is stored more than once in {r}, and this kind of note holds details that "
        f"cannot be written back, so one copy cannot be left. {_UNCHANGED} Remove all its "
        "copies, or leave it.",
    ),
    "edit-type": (
        "This kind of note cannot be changed",
        f"A note of {{r}}: {EDIT_TYPE_REASON[0].lower()}{EDIT_TYPE_REASON[1:]} {_UNCHANGED}",
    ),
    technical("verify-plan"): (
        "The changed notes could not be checked",
        "The planned notes of {r} did not pass the library's check before anything was "
        f"written, so nothing was changed. {_UNCHANGED} {_REOPEN}",
    ),
    "denied": (
        "The notes could not be written",
        "The notes of {r} could not be written: the system refused. Either its folder is "
        "read-only or you do not have permission to write to it, or (on Windows) another "
        f"program holds one of its files open. {_UNCHANGED} Close other programs that show "
        "this recording and check that you may change it, then try again.",
    ),
    "read-only": (
        "The recording's folder cannot be written to",
        "The notes of {r} cannot be changed: its folder is read-only, or you do not have "
        f"permission to write to it. {_UNCHANGED} Ask whoever looks after the recording's "
        "files to allow it (or open a copy that you may change), then try again.",
    ),
    "edit-in-progress": (
        "Another program is changing this recording's notes",
        "A program on this computer started a change to the notes of {r} and has not finished. "
        f"{_UNCHANGED} Wait until it has finished, then try again. If no program is running, "
        f"the change was left unfinished: {_ASK[0].lower()}{_ASK[1:]}",
    ),
    "edit-newer-format": (
        "A newer version of mef3io changed this recording",
        "The notes of {r} were last changed by a newer version of mef3io than this app uses. "
        f"{_UNCHANGED} Update the app, then try again.",
    ),
    "edit-failed": (
        "The change could not be put in place",
        "{r} was put back as it was. Another program may be holding its notes open (on "
        f"Windows this stops the change). {_UNCHANGED} Close other programs that show this "
        "recording, then try again.",
    ),
    technical("records-changed"): (
        "The notes changed while this was being done",
        "Another program changed the notes of {r} while they were being rewritten. "
        f"{_UNCHANGED} {_REOPEN}",
    ),
    "being-written": (
        "Another program is writing to this recording",
        f"{{r}} is being written to by another program. {_UNCHANGED} Wait until it has "
        "finished, then try again.",
    ),
    "tar-read-only": (
        "A single-file recording cannot be changed",
        "{r} is a single file. Notes in a single-file recording can only be read. "
        f"{_UNCHANGED} Unpack it in Explore first, then open the unpacked recording.",
    ),
}


def _outcome(name: str, done: Sequence[str]) -> str:
    """What became of the notes, said exactly: nothing removed, or which recordings were
    changed (and cannot be changed back) and that ``name`` was not."""
    if not done:
        return "Nothing was removed."
    where = ", ".join(done)
    return (
        f"{name} was not changed, and neither were the recordings after it. Already "
        f"changed, and this cannot be undone: recording {where}."
    )


def folders_writable(paths: dict[int, str]) -> dict[int, bool]:
    """Recording index -> whether its folders take writes (a task's work: it asks the file
    system once per folder, which can be slow on a network drive)."""
    return {i: not _not_writable(p) for i, p in paths.items()}


def look_at_notes(data, marks) -> dict:
    """Read the notes as the files hold them NOW (a task) and say, for each stored note in
    ``marks`` (a ``Mark``), how many identical copies there are in its recording: the window
    compares this with what it counted when the note was marked. Returns ``{mark: copies}``."""
    seen: dict[tuple, int] = {}
    for rec in data.fresh_annotations():
        k = record_key(rec)
        seen[k] = seen.get(k, 0) + 1
    return {m: seen.get(mark_key(m), 0) for m in marks}


def _not_writable(path: str | None) -> bool:
    """Whether the recording's folder, or a folder directly in it (a channel's), cannot be
    written to: asked of the folders themselves, never concluded from an error text."""
    if not path:
        return False
    try:
        folders = [path]
        with os.scandir(path) as entries:
            folders += [e.path for e in entries if e.is_dir(follow_symlinks=False)]
        return any(not os.access(f, os.W_OK) for f in folders)
    except OSError:
        return False


def _was_denied(exc: BaseException) -> bool:
    """The error says a write was refused: by number, or in the library's or the system's
    words. On Windows a file that another program holds open gives the SAME text, so this
    alone never proves that a folder is read-only."""
    import errno

    if getattr(exc, "errno", None) in (errno.EACCES, errno.EROFS):
        return True
    text = str(exc).lower()
    return any(
        s in text
        for s in (
            "read-only file system",
            "permission denied",
            "access is denied",
            "cannot open for write",  # the library's words when a folder takes no new file
        )
    )


def describe_removal_error(
    exc: BaseException, name: str, done: Sequence[str] = (), path: str | None = None
) -> UserMessage:
    """Plain words for a failed removal in ``name`` (e.g. "Recording A"). ``done`` = the
    letters of recordings already changed before this one failed. ``path`` = the recording's
    folder: a refused write is called "read-only" only when the folder itself is not
    writable (or the system says "read-only file system"); otherwise the sentence names the
    other causes too (on Windows a file held open by another program is refused with the same
    words). Every code of the library has an entry in :data:`REMOVAL_WORDS`; anything else
    gets the general words of :func:`describe_error`, made true for what was already done."""
    import errno

    code = str(getattr(exc, "code", "") or "")
    detail = f"{type(exc).__name__}: {exc}"
    outcome = _outcome(name, done)
    if code in ("", "edit-failed") and _was_denied(exc):
        if _not_writable(path) or getattr(exc, "errno", None) == errno.EROFS:
            code = "read-only"
        elif code == "":  # refused, and the folder looks writable: say both causes
            code = "denied"
    if code in REMOVAL_WORDS:
        title, text = REMOVAL_WORDS[code]
        return UserMessage(
            title, text.replace("{r}", name).replace("{outcome}", outcome), detail, code
        )
    base = describe_error(exc)
    if done:  # the general text says "not changed", which is not true here
        text = f"{base.title}. {outcome} See the technical details for what happened."
    else:
        text = f"{base.text} {outcome}"
    return UserMessage(base.title, text, base.detail or detail, code)


def _body(rec: dict) -> dict:
    """What ``add`` takes of a stored record: its time, type, text, length and place."""
    out = {"time": int(rec["time"]), "type": rec.get("type") or "Note", "text": rec.get("text")}
    if rec.get("duration") is not None:
        out["duration"] = rec["duration"]
    if rec.get("channel") is not None:
        out["channel"] = rec["channel"]
    if rec.get("segment") is not None:
        out["segment"] = rec["segment"]
    return out


def stored_index(data) -> dict[int, dict[tuple, list[dict]]]:
    """The stored notes of every recording, indexed ONCE by their exact content
    (:func:`record_key`): recording -> key -> the records that are that note. Finding a note
    and counting its copies is then one lookup, however many notes there are."""
    out: dict[int, dict[tuple, list[dict]]] = {}
    for rec in data.annotations():
        out.setdefault(rec["recording"], {}).setdefault(record_key(rec), []).append(rec)
    return out


def _selectors(data, index: int, changes: list[StoredChange], stored=None):
    """The ``remove`` and ``add`` lists for recording ``index``, or a plain reason why not
    (a code of ours: gone, unclear, copy-type, edit-type). The stored records come from the
    open data (one read of the notes, already cached), indexed once (``stored``: the index
    of :func:`stored_index`, built here when not given)."""
    stored = (stored if stored is not None else stored_index(data)).get(index, {})
    remove: list[dict] = []
    add: list[dict] = []
    seen: set[tuple] = set()
    for change in changes:
        key = mark_key(change.mark)
        if key in seen:  # the same note chosen twice: the library would refuse the overlap
            return None, None, "unclear"
        seen.add(key)
        found = stored.get(key, [])
        if len(found) != change.copies:
            return None, None, "gone"
        shape = {k: v for k, v in found[0].items() if k != "recording"}
        if any({k: v for k, v in r.items() if k != "recording"} != shape for r in found):
            return None, None, "unclear"  # same time and text, but not the same note
        kind = str(shape.get("type") or "")
        if change.remove < change.copies and kind not in COPYABLE_TYPES:
            return None, None, "copy-type"  # a copy added back would not be identical
        if change.replacement is not None and kind not in EDITABLE_TYPES:
            return None, None, "edit-type"  # an edit would change its type or drop its fields
        remove.append({**shape, "expect": change.copies})
        add.extend(_body(found[0]) for _ in range(change.copies - change.remove))
        if change.replacement is not None:
            new = dict(change.replacement)
            new.setdefault("segment", shape.get("segment"))
            if new.get("segment") is None or new.get("channel") != shape.get("channel"):
                new.pop("segment", None)
            add.append({k: v for k, v in new.items() if v is not None})
    return remove, add, ""


def remove_stored_notes(
    data,
    changes,
    passwords: dict[int, str] | None = None,
    check_cancelled: Callable[[], None] = lambda: None,
    report: Callable[[int, int], None] = lambda _done, _total: None,
    register: Callable[..., None] = lambda *_values: None,
    cancel=None,
) -> StoredResult:
    """Take the stored notes of ``changes`` (:class:`StoredChange`) out of their recordings,
    adding the replacements in the same call. Every recording is checked first (it is where it
    was opened, the passwords fit, the notes are what the viewer shows, it is not a
    single-file recording): nothing is written when any check fails. Then recording by
    recording, ONE library call each; it stops at the first failure or stop and the result
    says exactly which recordings were done. ``cancel`` is the task's ``CancelToken``.

    Runs on a task, never the GUI thread."""
    import mef3io

    from .cancel import is_stop

    result = StoredResult()
    extra = passwords or {}
    register(*(p for p in extra.values() if p))
    by_recording: dict[int, list[StoredChange]] = {}
    for change in changes:
        by_recording.setdefault(change.mark.recording, []).append(change)

    pairs: dict[int, tuple[str, str]] = {}
    plans: dict[int, tuple[list[dict], list[dict]]] = {}
    stored = stored_index(data)  # the stored notes, indexed ONCE
    for index in sorted(by_recording):
        name = f"Recording {data.recordings[index].letter}"
        if data.is_archive(index):
            result.error = describe_removal_error(_Coded("tar-read-only"), name)
            return result
        try:
            pairs[index] = prepare(data, index, extra.get(index, ""), for_removal=True)
        except SaveFailed as exc:
            result.error = exc.user_message
            return result
        remove, add, why = _selectors(data, index, by_recording[index], stored)
        if why:
            code = {"gone": "no-match", "unclear": "bad-selector"}.get(why, why)
            result.error = describe_removal_error(_Coded(code), name)
            return result
        plans[index] = (remove, add)
    total, done = len(by_recording), 0
    for index in sorted(by_recording):
        name = f"Recording {data.recordings[index].letter}"
        path = data.location_of(index)
        p1, p2 = pairs[index]
        remove, add = plans[index]
        try:
            before = signal_snapshot(path)
        except OSError:
            before = None
        try:
            check_cancelled()
            # Nothing may hold this recording's files open while its notes files are swapped.
            data.release_files()
            kwargs = {"cancel": cancel.for_library()} if cancel is not None else {}
            report_ = mef3io.remove_annotations(
                path, remove=remove, add=add, password1=p1, password2=p2, **kwargs
            )
        except BaseException as exc:  # noqa: BLE001 - every library error becomes a plain one
            if is_stop(exc):
                result.stopped = True
                result.error = UserMessage(
                    "Stopped",
                    "The removal was stopped before the notes were changed. Nothing was removed"
                    + (" from any recording." if not result.written else " from the others.")
                    + _removed_text(data, result),
                )
            elif isinstance(exc, Exception):
                result.error = describe_removal_error(
                    exc, name, [data.recordings[i].letter for i in sorted(result.written)], path
                )
            else:
                raise
            return result
        finally:
            if before is not None:
                try:
                    same = signal_snapshot(path) == before
                except OSError:
                    same = False
                result.signals_same[index] = same
        gone = sum(c.copies for c in by_recording[index])
        if report_.removed != gone:  # the library did something else than was asked: say so
            # What the file holds is not what was asked, so these changes may already be in it:
            # they leave the draft (never kept as marks that could be applied twice) and the
            # window reads the notes again.
            result.error = UserMessage(
                "The notes were not what was expected",
                f"{name} now holds a different number of notes than expected, so the changes to "
                "it may already be written. They were taken out of the window's list of changes, "
                "and the notes shown are read again from the file: check them there. The "
                "changes to the other recordings are listed separately.",
            )
            result.written.append(index)
            result.net[index] = report_.added - report_.removed
            result.uids.update(c.uid for c in by_recording[index] if c.uid is not None)
            result.marks.extend(c.mark for c in by_recording[index])
            result.counts[index] = (
                0,
                sum(1 for c in by_recording[index] if c.replacement is not None),
                sum(c.remove for c in by_recording[index] if c.replacement is None),
            )
            result.unsure = True
            return result
        result.written.append(index)
        result.removed += sum(c.remove for c in by_recording[index])
        result.added += sum(1 for c in by_recording[index] if c.replacement is not None)
        result.net[index] = report_.added - report_.removed
        result.uids.update(c.uid for c in by_recording[index] if c.uid is not None)
        result.marks.extend(c.mark for c in by_recording[index])
        result.counts[index] = (
            0,
            sum(1 for c in by_recording[index] if c.replacement is not None),
            sum(c.remove for c in by_recording[index] if c.replacement is None),
        )
        done += 1
        report(done, total)
    return result


class _Coded(Exception):
    """A refusal found here, with the library's own kind of code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _removed_text(data, result: StoredResult) -> str:
    if not result.written:
        return ""
    letters = ", ".join(data.recordings[i].letter for i in sorted(result.written))
    return f" Already done (this cannot be undone): recording {letters}."
