"""Turn exceptions into plain-language messages for people, not programmers.

The technical detail (exception type and text) is kept separately so the UI
can show it behind a "Details" toggle. It is never written to the log here.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass

from .cancel import JobCancelled


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
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, JobCancelled):
        return UserMessage("Cancelled", "The task was cancelled. Nothing was changed.", detail)

    m = _mef3io_errors()
    # Without the C++ backend mef3io aliases all of these to RuntimeError,
    # which would make every RuntimeError look like a password problem.
    if m is not None and m.MefError is not RuntimeError:
        # Order matters: the mef3io error classes share a base class.
        if isinstance(exc, m.PasswordError):
            return UserMessage(
                "Password needed",
                "This recording is protected by a password, and the password given "
                "did not unlock it. Check the password and try again.",
                detail,
            )
        if isinstance(exc, m.CrcError):
            return UserMessage(
                "Part of the recording looks damaged",
                "A check on the file contents failed, so part of this recording may be "
                "damaged. Running an integrity check will show which part.",
                detail,
            )
        if isinstance(exc, m.FormatError):
            return UserMessage(
                "Not a readable MEF 3.0 recording",
                "This does not look like a MEF 3.0 recording that can be read. "
                "Recordings are folders ending in .mefd, or single files ending in .mefd.tar.",
                detail,
            )
        if isinstance(exc, m.IoError):
            return UserMessage(
                "The recording could not be opened",
                "The recording could not be read from disk. Check that it still exists, "
                "that its name ends in .mefd or .mefd.tar, and that the drive is connected.",
                detail,
            )
    if isinstance(exc, PermissionError):
        return UserMessage(
            "Access denied",
            "Your account is not allowed to read or write this location. "
            "Ask your IT team, or choose a different folder.",
            detail,
        )
    if isinstance(exc, FileNotFoundError):
        return UserMessage(
            "Not found",
            "The file or folder could not be found. It may have been moved, renamed or "
            "deleted, or the drive may be disconnected.",
            detail,
        )
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
        return UserMessage(
            "The disk is full",
            "There is not enough free space to finish. Free some space or choose another drive.",
            detail,
        )
    if isinstance(exc, MemoryError):
        return UserMessage(
            "Not enough memory",
            "The computer ran out of memory for this task. Close other programs and try again.",
            detail,
        )
    return UserMessage(
        "Something went wrong",
        "The task could not be finished because of an unexpected problem. "
        "The technical details below can help when reporting it.",
        detail,
    )
