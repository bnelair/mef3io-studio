"""Saving the draft's new notes and events: the only place (with
``viewer_source``) where the viewer's edit mode calls the library.

A recording's notes are only ever APPENDED to (``Writer.write_annotations``
never replaces or deletes a stored one), so an interrupted save cannot damage
what was there; the library refuses a torn notes file until it has been
fixed, and this module says so in plain words.

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


def _refuse(title: str, text: str) -> SaveFailed:
    return SaveFailed(UserMessage(title, text + " Nothing was added to any recording."))


def prepare(data, index: int, other_password: str = "") -> tuple[str, str]:
    """Check recording ``index`` and return the ``(basic, full-access)``
    passwords to write with (``("", "")`` for a recording without any).
    Raises :class:`SaveFailed` (nothing has been written) when the recording
    moved or changed, or the passwords are missing or wrong."""
    import mef3io

    from .viewer_source import fingerprint_of

    letter = data.recordings[index].letter
    path = data.location_of(index)
    opened = data.facts(index)
    if not os.path.isdir(path) or opened is None:
        raise _refuse(
            "This recording is not where it was opened",
            f"Recording {letter} cannot be found at the place it was opened from. It may "
            "have been moved, renamed or removed. Close this window and open the "
            "recording again.",
        )
    held = data.password_of(index)
    try:
        now = mef3io.inspect(path, password=held)
    except Exception as exc:  # noqa: BLE001
        raise _refuse(
            "This recording could not be checked",
            f"Recording {letter} could not be read again before saving "
            f"({describe_error(exc).title}).",
        ) from exc
    if fingerprint_of(now) != opened[2]:
        raise _refuse(
            "This recording has changed",
            f"Recording {letter} is not the same as when it was opened. Close this window "
            "and open it again.",
        )
    if not now.encryption["encrypted"]:
        return "", ""
    if not held or not other_password:
        raise _refuse("Both passwords are needed", f"Recording {letter}. {BOTH_PASSWORDS}")
    access = str(now.encryption["access"])
    if access == "full":
        level2, level1 = held, other_password
    elif access == "level-1":
        level1, level2 = held, other_password
    else:
        raise _refuse("Both passwords are needed", f"Recording {letter}. {BOTH_PASSWORDS}")
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


def write_plan(
    data,
    plan: SavePlan,
    extra_passwords: dict[int, str] | None = None,
    check_cancelled: Callable[[], None] = lambda: None,
    report: Callable[[int, int], None] = lambda _done, _total: None,
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
        for items, channel in steps:
            try:
                check_cancelled()
                with mef3io.Writer(path, overwrite=False, password1=p1, password2=p2) as writer:
                    writer.write_annotations([i.record() for i in items], channel=channel)
            except BaseException as exc:  # noqa: BLE001 - every library error becomes a plain one
                if is_stop(exc):
                    result.stopped = True
                    result.error = UserMessage(
                        "Stopped", "The save was stopped." + _added_text(data, added)
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
    return result
