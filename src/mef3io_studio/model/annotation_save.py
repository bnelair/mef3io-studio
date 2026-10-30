"""Saving the draft's new notes and events: the only place (with
``viewer_source``) where the viewer's edit mode calls the library.

A recording's notes are only ever APPENDED to (``Writer.write_annotations``
never replaces or deletes a stored one), so an interrupted save cannot damage
what was there; the library refuses a torn notes file until it has been
rescued, and this module says so in plain words.

Runs on a background task, never on the GUI thread. No paths, names or note
text in anything that can reach a log.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .annotation_draft import SavePlan
from .errors import UserMessage, describe_error


class SaveFailed(Exception):
    """A save stopped. ``user_message`` is the plain-language text."""

    def __init__(self, user_message: UserMessage):
        super().__init__("save failed")
        self.user_message = user_message


def _plain(exc: BaseException, letter: str, written: tuple[str, ...]) -> SaveFailed:
    import mef3io

    text = str(exc).lower()
    done = (
        f" Recording{'s' if len(written) > 1 else ''} {', '.join(written)} "
        f"already {'have' if len(written) > 1 else 'has'} the new notes."
        if written
        else " Nothing was added to any recording."
    )
    if isinstance(exc, mef3io.PasswordError) or "no password" in text:
        msg = UserMessage(
            "These notes need the full-access password",
            f"Recording {letter} is protected, and the password given does not allow "
            f"adding notes to it.{done} Open it in Explore with the full-access "
            "password and try again.",
        )
    elif "recover_session" in text or "partial record" in text or "interrupted" in text:
        msg = UserMessage(
            "This recording's notes need to be rescued first",
            f"The notes of recording {letter} end in an unfinished entry, probably from "
            f"an interrupted save or a power cut.{done} Use the rescue option in "
            "Explore, then add the notes again. The notes already stored were not changed.",
        )
    else:
        base = describe_error(exc)
        msg = UserMessage(base.title, f"{base.text}{done}", base.detail)
    return SaveFailed(msg)


def _password_pairs(pwd: str) -> list[tuple[str, str]]:
    """``(level 1, level 2)`` pairs to try for the one password the viewer
    was opened with (the full-access one). The library takes the full-access
    password alone when the notes file is new, and needs it in the first slot
    as well once the file exists; a refused write changes nothing, so trying
    the next pair is safe."""
    return [("", pwd), (pwd, pwd)] if pwd else [("", "")]


def _append(path: str, pwd: str, records: list[dict], channel: str | None) -> None:
    import mef3io

    pairs = _password_pairs(pwd)
    for n, (p1, p2) in enumerate(pairs):
        try:
            with mef3io.Writer(path, overwrite=False, password1=p1, password2=p2) as writer:
                writer.write_annotations(records, channel=channel)
            return
        except (mef3io.PasswordError, mef3io.WriteConflictError):
            if n == len(pairs) - 1:
                raise


@dataclass
class WriteResult:
    #: Indexes of the recordings that were appended to completely.
    written: list[int] = field(default_factory=list)
    error: UserMessage | None = None
    stopped: bool = False


def write_plan(
    data,
    plan: SavePlan,
    check_cancelled: Callable[[], None] = lambda: None,
    report: Callable[[int, int], None] = lambda _done, _total: None,
) -> WriteResult:
    """Append every recording's share of ``plan``, recording by recording.
    It stops at the first failure or stop: the earlier recordings keep what
    was appended (it cannot be undone in place) and are listed in the result,
    so the caller can leave exactly those out of the draft."""
    from .cancel import is_stop

    result = WriteResult()
    total, done = plan.count, 0
    for index in sorted(plan.by_recording):
        share = plan.by_recording[index]
        letter = data.recordings[index].letter
        pwd = data.password_of(index)
        try:
            check_cancelled()
            path = data.location_of(index)
            if share.session:
                _append(path, pwd, [i.record() for i in share.session], None)
            for channel in sorted(share.channels):
                _append(path, pwd, [i.record() for i in share.channels[channel]], channel)
        except BaseException as exc:  # noqa: BLE001 - every library error becomes a plain one
            if is_stop(exc):
                result.stopped = True
            elif isinstance(exc, Exception):
                letters = tuple(data.recordings[i].letter for i in result.written)
                result.error = _plain(exc, letter, letters).user_message
            else:
                raise
            return result
        result.written.append(index)
        done += share.count
        report(done, total)
    return result
