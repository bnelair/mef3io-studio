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
from collections.abc import Callable
from dataclasses import dataclass, field

from . import timefmt
from .annotation_draft import SavePlan
from .errors import UserMessage, describe_error
from .text import technical

#: Why a note that is already stored in a recording cannot be taken out. ONE sentence,
#: shown beside the disabled "Remove" entry and when a key asks for it.
#: Only said while the installed mef3io is older than 2.4 (it has no ``remove_annotations``).
STORED_REMOVAL_REASON = "A stored note cannot be removed yet in this version."

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


class StoredRemovalNotSupported(Exception):
    """The installed mef3io cannot take out stored notes (before 2.4)."""

    def __init__(self) -> None:
        super().__init__("stored notes cannot be removed")
        self.user_message = UserMessage("A stored note cannot be removed", STORED_REMOVAL_REASON)


def stored_removal_reason() -> str | None:
    """The sentence that says why a stored note cannot be removed, or None when it can."""
    import mef3io

    return None if hasattr(mef3io, "remove_annotations") else STORED_REMOVAL_REASON


#: Plain words for every refusal the library can give here, by its stable ``code``:
#: ``(title, what happened and what it means, what to do)``. ``{r}`` is the recording's name
#: ("Recording A"). Never the library's own sentence (it names paths and note text): that
#: goes behind "Technical details". Each text says what became of the recording.
_UNCHANGED = "Nothing was removed."
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
    "An earlier change to the notes of {r} was left unfinished, for example by a power cut, so "
    "its notes cannot be changed until that has been settled. The app cannot do that yet: "
    f"{_ASK[0].lower()}{_ASK[1:]} The notes were not changed now."
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
    "edit-unsettled": ("An earlier change was left unfinished", _LEFT_OVER),
    "edit-interrupted": ("An earlier change was left unfinished", _LEFT_OVER),
    "mid-operation": ("An earlier change was left unfinished", _LEFT_OVER),
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


def describe_removal_error(exc: BaseException, name: str) -> UserMessage:
    """Plain words for a failed removal in ``name`` (e.g. "Recording A"). Every code of the
    library has an entry in :data:`REMOVAL_WORDS`; anything else gets the general words of
    :func:`describe_error`, with "Nothing was removed." added."""
    code = str(getattr(exc, "code", "") or "")
    detail = f"{type(exc).__name__}: {exc}"
    if code in REMOVAL_WORDS:
        title, text = REMOVAL_WORDS[code]
        return UserMessage(title, text.replace("{r}", name), detail, code)
    base = describe_error(exc)
    return UserMessage(base.title, f"{base.text} Nothing was removed.", base.detail or detail, code)


def _mark_matches(rec: dict, mark) -> bool:
    """Whether the stored record ``rec`` is the note ``mark`` shows (its time, type, level,
    channel, text and length)."""
    from . import viewer_marks as vm

    text = rec.get("text")
    return (
        int(rec["time"]) == mark.time
        and str(rec.get("type") or "") == mark.code
        and str(rec.get("level") or "") == mark.level
        and rec.get("channel") == mark.channel
        and (text if isinstance(text, str) else "") == mark.text
        and vm.mark_end(rec) == mark.end
    )


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


def _selectors(data, index: int, changes: list[StoredChange]):
    """The ``remove`` and ``add`` lists for recording ``index``, or a plain reason why not.
    The stored records come from the open data (one read of the notes, already cached)."""
    stored = [a for a in data.annotations() if a["recording"] == index]
    remove: list[dict] = []
    add: list[dict] = []
    for change in changes:
        found = [r for r in stored if _mark_matches(r, change.mark)]
        if len(found) != change.copies:
            return None, None, "gone"
        shape = {k: v for k, v in found[0].items() if k != "recording"}
        if any({k: v for k, v in r.items() if k != "recording"} != shape for r in found):
            return None, None, "unclear"  # same time and text, but not the same note
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

    if stored_removal_reason() is not None:
        raise StoredRemovalNotSupported
    result = StoredResult()
    extra = passwords or {}
    register(*(p for p in extra.values() if p))
    by_recording: dict[int, list[StoredChange]] = {}
    for change in changes:
        by_recording.setdefault(change.mark.recording, []).append(change)

    def refuse(title: str, text: str) -> StoredResult:
        result.error = UserMessage(title, text + " Nothing was removed.")
        return result

    pairs: dict[int, tuple[str, str]] = {}
    plans: dict[int, tuple[list[dict], list[dict]]] = {}
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
        remove, add, why = _selectors(data, index, by_recording[index])
        if why:
            code = "no-match" if why == "gone" else "bad-selector"
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
                result.error = describe_removal_error(exc, name)
                if result.written:
                    result.error = UserMessage(
                        result.error.title,
                        result.error.text + _removed_text(data, result),
                        result.error.detail,
                        result.error.code,
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
            result.error = UserMessage(
                "The notes were not what was expected",
                f"{name} now holds a different number of notes than expected. Close this "
                "window and open the recording again to see what it holds.",
            )
            result.written.append(index)
            result.net[index] = report_.added - report_.removed
            return result
        result.written.append(index)
        result.removed += sum(c.remove for c in by_recording[index])
        result.added += sum(1 for c in by_recording[index] if c.replacement is not None)
        result.net[index] = report_.added - report_.removed
        result.uids.update(c.uid for c in by_recording[index] if c.uid is not None)
        result.marks.extend(c.mark for c in by_recording[index])
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
