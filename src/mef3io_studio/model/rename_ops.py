"""Rename recordings with mef3io, safely. Runs in a helper process.

mef3io does every byte of it (``rename_session``: the folder, the notes file
of the whole recording, the name stored in every file and a description that
equals the old name, as one journaled operation that a crash cannot leave
half-done; ``fix_name_drift``; ``rename_archive``). This module decides only
where mef3io may keep its recovery notes, runs a batch, and keeps what is
needed to undo it.

* **Recovery notes** (mef3io's journal and old copies of the few bytes it
  changes) live in ONE folder of the per-user app-data folder, ``renames``,
  and never beside the recordings. The files in it have opaque names; they
  are removed by mef3io when an operation completes. mef3io finds the notes
  of an interrupted operation again by that same folder, and puts the
  recording back as it was before it does anything else.
* **Undo** is a logical inverse (``RenameReport.undo``: rename back, and put
  back only the descriptions this rename changed), never a byte restore, so
  anything written since (new data, new notes) is kept. It is a full
  operation with every check of the original, and asks for the password
  again. An archive is renamed back with ``rename_archive``.
* **Not while being written**: mef3io refuses a recording with a copy
  being made beside it or a writer's temporary file; on top of that the
  newest files of every channel are looked at (:func:`recently_written`).
* Nothing identifying is logged; messages carry no names or paths.

Plain Python, no Qt.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .cancel import CancelToken, is_stop, stopped_by_library
from .errors import UserMessage, describe_error
from .journal import data_dir
from .quiet import library_warnings_caught
from .recording import recording_name
from .rename import RECENT_SECONDS, RowState, list_folder_keys, name_key
from .rename import blocked_reason as rn_blocked
from .text import technical

BACKUP_FOLDER = "renames"


def backup_dir() -> str:
    """Where mef3io keeps the notes of a rename while it runs: one folder
    under the app's data folder (owner only), never beside a recording."""
    path = os.path.join(data_dir(), BACKUP_FOLDER)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


# -- looking before renaming ------------------------------------------------------------


def _part_order(name: str) -> tuple[int, str]:
    """Newest-part order: by the number after the last '-' (so part 10
    follows part 9, whatever the digits' width), else by name."""
    found = re.search(r"-(\d+)(?:\.[^.-]*)?$", name)
    return (int(found.group(1)) if found else -1, name)


def newest_files(location: str):
    """``(relative path with "/", absolute path)`` of the files where a
    recorder writes: the recording's top level, each channel folder's own
    files, and the files of the NEWEST part folder of each channel (highest number).
    Found by depth alone, never by file name, so it reads no recording
    format and costs a few files per channel, never the data."""
    try:
        top = sorted(os.scandir(location), key=lambda e: e.name)
    except OSError:
        return
    for entry in top:
        if entry.is_file():
            yield entry.name, entry.path
    for channel in (e for e in top if e.is_dir()):
        try:
            inside = sorted(os.scandir(channel.path), key=lambda e: e.name)
        except OSError:
            continue
        for e in inside:
            if e.is_file():
                yield f"{channel.name}/{e.name}", e.path
        parts = [e for e in inside if e.is_dir()]
        if not parts:
            continue
        last = max(parts, key=lambda e: _part_order(e.name))
        try:
            files = list(os.scandir(last.path))
        except OSError:
            continue
        for e in files:
            if e.is_file():
                yield f"{channel.name}/{last.name}/{e.name}", e.path


def longest_inside(location: str) -> int:
    """Characters of the longest path below the recording's folder (for the
    Windows path-length check): every part of a channel is named alike, so
    the files :func:`newest_files` finds give it."""
    return max((len(rel) for rel, _path in newest_files(location)), default=0)


def recently_written(
    location: str,
    now: float | None = None,
    seconds: float | None = None,
    skip: dict[str, tuple[int, int]] | None = None,
) -> bool:
    """True when a file of the recording changed in the last few minutes
    (see :func:`newest_files` for which). ``skip``: files (relative paths,
    ``/``) whose size and modification time are exactly what a rename of
    ours left, which do not count."""
    now = time.time() if now is None else now
    limit = now - (RECENT_SECONDS if seconds is None else seconds)
    skip = skip or {}

    def recent(path: str, rel: str) -> bool:
        try:
            st = os.stat(path)
        except OSError:
            return False
        if skip.get(rel) == (st.st_size, st.st_mtime_ns):
            return False
        return st.st_mtime >= limit

    if os.path.isfile(location):
        return recent(location, "")
    return any(recent(path, rel) for rel, path in newest_files(location))


@dataclass(frozen=True)
class StateRequest:
    key: str = field(repr=False)
    location: str = field(repr=False)
    packed: bool = False
    #: Files our own earlier rename left (relative path -> (size, mtime ns)).
    skip: dict = field(default_factory=dict, repr=False)


def read_state(
    requests: list[StateRequest],
    cancel: CancelToken | None = None,
    send: Callable[[tuple[int, int]], object] | None = None,
) -> tuple[dict[str, RowState], dict[str, set[str]]]:
    """For each recording: is its stored name in step with its folder, was a
    rename left unfinished, was it written to a moment ago. Reads each
    file's first 1 KB and lists folders; never data. Also returns what is
    in each folder (name keys), so the window can check new names against
    it without touching the drive itself."""
    import mef3io

    out: dict[str, RowState] = {}
    folders: dict[str, set[str]] = {}
    with library_warnings_caught(), stopped_by_library():
        for n, request in enumerate(requests):
            if cancel is not None:
                cancel.raise_if_cancelled()
            drift = unfinished = False
            unreadable = ""
            try:
                recent = recently_written(request.location, skip=request.skip)
                deepest = 0 if request.packed else longest_inside(request.location)
                if not request.packed:
                    unfinished = mef3io.operation_marker(request.location) is not None
                    if not unfinished:
                        drift = not mef3io.name_drift(request.location).consistent
            except Exception as exc:  # noqa: BLE001 - reported on the row
                if is_stop(exc):
                    raise
                recent, deepest = False, 0
                unreadable = describe_rename_error(exc, request.packed).text
            out[request.key] = RowState(
                drift=drift,
                unfinished=unfinished,
                recently_written=recent,
                unreadable=unreadable,
                deepest_inside=deepest,
            )
            folder = os.path.dirname(os.path.abspath(request.location))
            if folder not in folders:
                folders[folder] = list_folder_keys(folder)
            if send is not None:
                send((n + 1, len(requests)))
    return out, folders


# -- the words ---------------------------------------------------------------------------

NOTHING_CHANGED = "Nothing was lost."


def describe_rename_error(
    exc: BaseException, packed: bool = False, case_only: bool = False
) -> UserMessage:
    """A rename's failure in plain words (what happened, what it means for
    the recording, what to do). The library's text goes only into the
    technical details."""
    import mef3io

    detail = technical(f"{type(exc).__name__}: {exc}")
    if is_stop(exc):
        return describe_error(exc)
    if isinstance(exc, mef3io.PasswordError):
        return UserMessage(
            "The password did not work",
            "The recording is password-protected and this password did not open it, so "
            "nothing was renamed. Check the password (it is case-sensitive) and try again.",
            detail,
        )
    if isinstance(exc, mef3io.WriteConflictError) and "out of step" in str(exc):
        return UserMessage(
            "The name stored inside does not match the folder",
            "Another program renamed this recording's folder only, so the name kept inside "
            "it no longer matches. Nothing was renamed. In the Rename window, tick "
            "the box that puts the names in step first, then rename.",
            detail,
        )
    if isinstance(exc, mef3io.WriteConflictError):
        return UserMessage(
            "The recording is not ready to be renamed",
            "Something is using or changing the recording right now, a copy is being made "
            "next to it, or a rename by another program was left unfinished. Nothing was "
            "renamed. Wait until that is done and try again; if it stays, Check for "
            "problems may say more.",
            detail,
        )
    if case_only and isinstance(exc, mef3io.IoError):
        return UserMessage(
            "Only the capital letters changed",
            "This drive treats capital and small letters as the same, so it cannot change "
            "only the capitals of a name in one step. Nothing was changed. Rename the "
            "recording to a different name first, then to the one you want.",
            detail,
        )
    if isinstance(exc, mef3io.IoError):
        return UserMessage(
            "The recording could not be renamed",
            "Something with the new name is already there, a file inside is damaged, or "
            "this drive cannot rename safely without a risk of replacing something. "
            "Nothing was replaced, and if the rename had started it was put back. "
            "Choose another name, or use Check for problems.",
            detail,
        )
    if isinstance(exc, ValueError):
        return UserMessage(
            "That name cannot be used",
            "The new name is not allowed (too long, a sign that is not allowed in names, "
            "or the same as before), so nothing was renamed. Choose another name.",
            detail,
        )
    return describe_error(exc)


# -- running a batch -----------------------------------------------------------------------


@dataclass(frozen=True)
class UndoStep:
    """What is needed to undo one step. In memory only (names and paths)."""

    kind: str  # "rename" | "drift" | "archive"
    old_path: str = field(repr=False)
    new_path: str = field(repr=False)
    old_name: str = field(repr=False)
    #: mef3io's report (a rename or a drift fix); None for an archive.
    report: object = field(default=None, repr=False)


@dataclass(frozen=True)
class RenameJob:
    key: str = field(repr=False)
    location: str = field(repr=False)
    new_name: str = field(repr=False)
    password: str = field(default="", repr=False)
    packed: bool = False
    #: Bring the stored names in step with the folder first.
    fix_drift_first: bool = False
    #: Only put the recording back as it was (an unfinished rename): no new name.
    settle_only: bool = False


@dataclass(frozen=True)
class ItemResult:
    key: str = field(repr=False)
    new_location: str = field(default="", repr=False)
    steps: tuple[UndoStep, ...] = ()
    message: UserMessage | None = None
    #: An unfinished rename was put back as it was before this one ran.
    put_back: bool = False
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.message is None


def _check_still_quiet(job: RenameJob) -> None:
    """Right before the rename (the window looked minutes ago): refuse a
    recording written to since. A recording whose own rename was left
    unfinished is not refused for the files that rename touched."""
    import mef3io

    if job.settle_only:
        return
    if not job.packed and mef3io.operation_marker(job.location) is not None:
        return
    if recently_written(job.location):
        raise RecentlyWritten()


class RecentlyWritten(Exception):
    """Something wrote to the recording after the window looked."""


def _one(job: RenameJob, folder: str) -> ItemResult:
    import mef3io

    _check_still_quiet(job)
    if job.packed:
        done = mef3io.rename_archive(job.location, job.new_name)
        step = UndoStep(
            "archive",
            done.old_path,
            done.new_path,
            os.path.basename(job.location).removesuffix(".mefd.tar"),
        )
        return ItemResult(job.key, done.new_path, (step,))
    steps: list[UndoStep] = []
    put_back = False
    notes: list[str] = []
    location = job.location
    if job.fix_drift_first or job.settle_only:
        report = mef3io.fix_name_drift(location, password=job.password, backup_dir=folder)
        put_back = bool(report.rolled_back)
        notes += list(report.notes)
        if report.changed:
            steps.append(
                UndoStep("drift", report.old_path, report.new_path, report.old_name, report)
            )
        location = report.new_path
    if not job.settle_only:
        report = mef3io.rename_session(
            location, job.new_name, password=job.password, backup_dir=folder
        )
        put_back = put_back or bool(report.rolled_back)
        notes += list(report.notes)
        steps.append(UndoStep("rename", report.old_path, report.new_path, report.old_name, report))
        location = report.new_path
    return ItemResult(job.key, location, tuple(steps), None, put_back, tuple(notes))


def run_renames(
    jobs: list[RenameJob],
    folder: str,
    cancel: CancelToken | None = None,
    send: Callable[[ItemResult], object] | None = None,
) -> list[ItemResult]:
    """Rename the recordings in the order given, one after the other. A
    recording that cannot be renamed gets its message and the rest go on;
    a stop is checked between recordings (a rename in progress is finished
    unless the app is forced to stop; the next rename of that recording rolls
    it back). Each result is also passed to ``send`` as soon as it is
    known, so a stop does not lose what was already done."""
    results: list[ItemResult] = []
    with library_warnings_caught(), stopped_by_library():
        for job in jobs:
            if cancel is not None and cancel.cancelled:
                break
            try:
                results.append(_one(job, folder))
            except Exception as exc:  # noqa: BLE001 - reported on the row
                if is_stop(exc):
                    raise
                if isinstance(exc, RecentlyWritten):
                    message = UserMessage(
                        "Not renamed: it was just written to",
                        rn_blocked(RowState(recently_written=True)) + " Nothing was changed.",
                    )
                else:
                    message = describe_rename_error(
                        exc,
                        job.packed,
                        case_only=name_key(job.new_name) == name_key(recording_name(job.location)),
                    )
                results.append(ItemResult(job.key, "", (), message))
            if send is not None:
                send(results[-1])
    return results


def undo_steps(
    steps: tuple[UndoStep, ...], password: str, folder: str, cancel: CancelToken | None = None
) -> ItemResult:
    """Undo one recording's steps, last first. Returns where it is now."""
    import mef3io

    location = steps[-1].new_path if steps else ""
    left = list(steps)  # the steps still to undo, oldest first
    with library_warnings_caught(), stopped_by_library():
        try:
            while left:
                if cancel is not None:
                    cancel.raise_if_cancelled()
                step = left[-1]
                if step.kind == "archive":
                    done = mef3io.rename_archive(step.new_path, step.old_name)
                    location = done.new_path
                else:
                    report = step.report.undo(password=password, backup_dir=folder)
                    location = report.new_path
                left.pop()
        except Exception as exc:  # noqa: BLE001 - reported
            if is_stop(exc):
                raise
            packed = bool(steps) and steps[0].kind == "archive"
            # What is already undone stays undone; a retry starts at the rest.
            return ItemResult("", location, tuple(left), describe_rename_error(exc, packed))
    return ItemResult("", location, ())


@dataclass(frozen=True)
class UndoUnit:
    """One recording's steps to undo (memory only). The password is NOT
    kept after a rename: ``protected`` says it must be asked for again, and
    ``password`` is filled in only for the one call that undoes."""

    key: str = field(repr=False)
    steps: tuple[UndoStep, ...] = field(repr=False)
    password: str = field(default="", repr=False)
    protected: bool = False


def undo_many(
    units: list[UndoUnit],
    folder: str,
    cancel: CancelToken | None = None,
    send: Callable[[ItemResult], object] | None = None,
) -> list[ItemResult]:
    """Undo each recording in turn; a stop is checked between recordings."""
    out: list[ItemResult] = []
    for unit in units:
        if cancel is not None and cancel.cancelled:
            break
        done = undo_steps(unit.steps, unit.password, folder, cancel)
        out.append(
            ItemResult(unit.key, done.new_location, done.steps, done.message, notes=done.notes)
        )
        if send is not None:
            send(out[-1])
    return out


def renamed_to(job: RenameJob) -> str:
    """Where the recording is now if a forced stop came after its rename
    but before the answer reached us, else "". A recording that still
    carries an unfinished operation's marker is NOT renamed (the next rename
    rolls it back), so it gives ""."""
    import mef3io

    suffix = ".mefd.tar" if job.packed else ".mefd"
    folder = os.path.dirname(os.path.abspath(job.location))
    target = os.path.join(folder, job.new_name + suffix)
    if job.settle_only or not os.path.lexists(target) or os.path.lexists(job.location):
        return ""
    if not job.packed:
        with library_warnings_caught():
            try:
                if mef3io.operation_marker(target) is not None:
                    return ""
            except Exception:  # noqa: BLE001 - cannot tell: do not claim it
                return ""
    return target
