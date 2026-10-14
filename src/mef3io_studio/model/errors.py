"""Turn exceptions into plain-language messages for people, not programmers.

The technical detail (exception type and text) is kept separately so the UI
can show it behind a "Details" toggle. It is never written to the log here.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass

from .cancel import JobCancelled


class HelperCrashed(RuntimeError):
    """The helper process that reads a recording stopped without an answer
    (a native crash such as a segmentation fault, or a forced exit)."""

    def __init__(self, exitcode: int | None):
        super().__init__(f"helper process ended unexpectedly (exit code {exitcode})")
        self.exitcode = exitcode


@dataclass(frozen=True)
class UserMessage:
    title: str
    text: str
    detail: str = ""


def _mef3io_errors():
    try:
        import mef3io
    except Exception:  # pragma: no cover - the app always bundles mef3io
        return None
    return mef3io


def describe_error(exc: BaseException) -> UserMessage:
    """Every message says what happened, what it means for the user's data,
    and what to do next — in everyday words (see docs/ui-language.md). The
    technical wording goes only into ``detail``, shown behind "Technical
    details"."""
    try:
        return _describe(exc)
    except Exception:  # never let the error path itself fail
        return UserMessage(
            "Something went wrong",
            "The task stopped because of an unexpected problem. Your recordings were "
            "not changed. Try again; if it keeps happening, report it and include the "
            "technical details.",
            "",
        )


def _describe(exc: BaseException) -> UserMessage:
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, JobCancelled):
        return UserMessage(
            "Stopped",
            "The task was stopped before it finished. Your recordings were not changed, "
            "and anything it had started to write was removed.",
            detail,
        )

    if isinstance(exc, HelperCrashed):
        return UserMessage(
            "The app stopped while reading this recording",
            "The part of the app that reads the recording stopped unexpectedly. The "
            'recording may be damaged. Nothing was changed. Use "Check for problems" '
            "to look for damage, and keep a copy of the original.",
            detail,
        )

    m = _mef3io_errors()
    # Without the C++ backend mef3io aliases all of these to RuntimeError,
    # which would make every RuntimeError look like a password problem.
    if m is not None and m.MefError is not RuntimeError:
        # Order matters: the mef3io error classes share a base class.
        if isinstance(exc, m.PasswordError):
            return UserMessage(
                "This recording needs its password",
                "The recording is password-protected, and the password given did not "
                "open it. The recording was not changed. Check the password (it is "
                "case-sensitive) and try again.",
                detail,
            )
        if isinstance(exc, m.CrcError):
            return UserMessage(
                "Part of this recording looks damaged",
                "Some of the data did not pass the built-in check, so part of the "
                "recording may be damaged or incomplete. Nothing was changed. Use "
                '"Check for problems" to see which part, and keep a copy of the original.',
                detail,
            )
        if isinstance(exc, m.FormatError):
            return UserMessage(
                "This is not a recording the app can read",
                "The file or folder does not look like a MEF 3.0 recording, or it is "
                "incomplete. Nothing was changed. Choose a recording instead: a folder "
                "ending in .mefd, or a single file ending in .mefd.tar.",
                detail,
            )
        if isinstance(exc, m.IoError):
            return UserMessage(
                "The recording could not be opened",
                "The app could not read the recording from the disk. Nothing was changed. "
                "Check that it still exists, that its name ends in .mefd or .mefd.tar, "
                "and that the drive is connected.",
                detail,
            )
    if isinstance(exc, PermissionError):
        return UserMessage(
            "Access denied",
            "Your account is not allowed to use this location, so nothing was read or "
            "written there. Choose a different folder, or ask your IT team for access.",
            detail,
        )
    if isinstance(exc, FileNotFoundError):
        return UserMessage(
            "Not found",
            "The file or folder could not be found. It may have been moved, renamed or "
            "deleted, or the drive may be disconnected. Nothing was changed. Check where "
            "it is now, reconnect the drive if needed, and try again.",
            detail,
        )
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
        return UserMessage(
            "The disk is full",
            "There was not enough free space to finish. The unfinished copy was removed "
            "and the originals were not changed. Free some space or choose another drive, "
            "then try again.",
            detail,
        )
    if isinstance(exc, MemoryError):
        return UserMessage(
            "Not enough memory",
            "The computer ran out of memory for this task. Your recordings were not "
            "changed. Close other programs and try again.",
            detail,
        )
    return UserMessage(
        "Something went wrong",
        "The task stopped because of an unexpected problem. Your recordings were not "
        "changed. Try again; if it keeps happening, report it and include the "
        "technical details.",
        detail,
    )
