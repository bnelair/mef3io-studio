"""Pack a recording folder into one file (``.mefd.tar``) and back.

mef3io does the packing (``archive_session`` / ``extract_session``); this
module only decides where the result goes and makes it safe:

* **Always a new path.** The original is never changed, and an existing file
  or folder is never replaced: the target is refused if it exists.
* **Never half a result under the final name.** mef3io writes to a hidden
  temporary name next to the target (``.mef3io-studio-<random>…``, opaque:
  it holds no recording name). The copy is then checked: it must open, and
  list the same channels with the same parts and sample counts as the
  original. Only then is it moved to its final name, without replacing
  anything that appeared there meanwhile (a link for a file; a reserved
  empty folder for a folder). If anything fails, or the task is stopped,
  the temporary copy is removed.

Plain Python, no Qt. The mef3io calls run in a helper process.
"""

from __future__ import annotations

import errno
import os
import secrets
import shutil
import sys
from dataclasses import dataclass

from .cancel import CancelToken, stopped_by_library
from .progress import byte_progress
from .quiet import library_warnings_caught
from .recording import is_packed, recording_name
from .scan import TEMP_PREFIX


class TargetExists(FileExistsError):
    """Something with the target's name is already there."""


class NotEnoughSpace(OSError):
    def __init__(self, needed: int, free: int):
        super().__init__(errno.ENOSPC, f"needs about {needed} bytes, {free} free")
        self.needed = needed
        self.free = free


class InsideRecording(ValueError):
    """The chosen destination is inside a recording (or the source itself)."""


class CopyMismatch(RuntimeError):
    """The packed or unpacked copy does not match the original."""


@dataclass(frozen=True)
class ArchivePlan:
    source: str
    target: str
    temporary: str
    packing: bool  # True: folder -> one file; False: one file -> folder


def plan(source: str, destination_folder: str) -> ArchivePlan:
    """Where packing (or unpacking) ``source`` into ``destination_folder``
    writes. Raises :class:`TargetExists` if the result's name is taken."""
    source = os.path.normpath(os.path.abspath(os.fspath(source)))
    destination = os.path.normpath(os.path.abspath(os.fspath(destination_folder)))
    # Case-insensitive like the suffix rule (REC.MEFD is a recording too),
    # and through links, so a shortcut into a recording is caught.
    real_source = os.path.realpath(source).lower()
    real_destination = os.path.realpath(destination).lower()
    inside_source = (real_destination + os.sep).startswith(real_source + os.sep)
    parts = [*real_destination.split(os.sep), *destination.lower().split(os.sep)]
    if inside_source or any(p.endswith(".mefd") for p in parts):
        raise InsideRecording("the destination is inside a recording")
    destination_folder = destination
    packing = not is_packed(source)
    name = recording_name(source)
    suffix = ".mefd.tar" if packing else ".mefd"
    target = os.path.join(os.fspath(destination_folder), name + suffix)
    if os.path.lexists(target):
        raise TargetExists(errno.EEXIST, "the target already exists")
    temporary = os.path.join(
        os.fspath(destination_folder), f"{TEMP_PREFIX}{secrets.token_hex(16)}{suffix}"
    )
    return ArchivePlan(source, target, temporary, packing)


def check_space(plan_: ArchivePlan, size_bytes: int | None) -> None:
    """Refuse up front when the destination clearly cannot hold the copy."""
    if not size_bytes:
        return
    folder = os.path.dirname(plan_.target) or "."
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        return
    needed = int(size_bytes * 1.02) + 16 * 1024 * 1024
    if free < needed:
        raise NotEnoughSpace(needed, free)


def _temporary_paths(plan_: ArchivePlan) -> list[str]:
    """The copy under its temporary name, plus whatever mef3io writes before
    it renames its own output into place (``<name>.tmp`` for a packed file).
    Everything starting with the random temporary name is ours."""
    folder = os.path.dirname(plan_.temporary)
    base = os.path.basename(plan_.temporary)
    try:
        names = os.listdir(folder)
    except OSError:
        return [plan_.temporary]
    return [os.path.join(folder, n) for n in names if n.startswith(base)]


def remove_temporary(plan_: ArchivePlan) -> None:
    """Remove the unfinished copy (it is ours: opaque random name)."""
    for path in _temporary_paths(plan_):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


def _shape(location: str, password: str = "", cancel: CancelToken | None = None) -> list:
    """Channels, rates, every part's times and sample count, and which
    parts could not be read."""
    from .inspect import inspect_recording

    outcome = inspect_recording(location, password, cancel=cancel, detect_legacy=False)
    if outcome.details is None:
        raise CopyMismatch("the copy could not be opened")
    details = outcome.details
    return [
        (
            c.name,
            c.sampling_frequency,
            tuple((p.start, p.end, p.samples, p.blocks) for p in c.parts),
        )
        for c in details.channels
    ] + [(p.channel, p.part) for p in details.part_problems]


def run_in_helper(
    plan_: ArchivePlan, password: str = "", send=None, cancel: CancelToken | None = None
) -> None:
    """mef3io writes the temporary copy, then the copy is compared with the
    original (opened with the same password: a copy keeps its protection).
    Runs in a helper process; never touches the final name.

    ``send`` gets :class:`~.progress.ByteProgress` while mef3io copies, then
    ``"checking"`` when the second step starts. ``cancel`` stops mef3io from
    inside; it then removes what it had written itself (and the job's
    cleanup removes anything left under the temporary name)."""
    import mef3io

    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    progress = byte_progress(send) if send is not None else None
    with stopped_by_library(), library_warnings_caught():
        if plan_.packing:
            mef3io.archive_session(
                plan_.source,
                plan_.temporary,
                False,
                cancel=cancel.for_library(),
                progress=progress,
            )
        else:
            mef3io.extract_session(
                plan_.source,
                plan_.temporary,
                False,
                cancel=cancel.for_library(),
                progress=progress,
            )
    if send is not None:
        send("checking")
    original = _shape(plan_.source, password, cancel)
    if original != _shape(plan_.temporary, password, cancel):
        raise CopyMismatch("the copy does not list the same channels and parts as the original")


def finish(plan_: ArchivePlan) -> str:
    """Give the checked copy its final name, never replacing anything."""
    if plan_.packing:
        try:
            os.link(plan_.temporary, plan_.target)  # fails if the target exists
        except FileExistsError:
            raise TargetExists(errno.EEXIST, "the target already exists") from None
        except OSError:
            # File systems without links (FAT, exFAT, some network shares).
            if sys.platform == "win32":
                try:
                    os.rename(plan_.temporary, plan_.target)  # never replaces on Windows
                except FileExistsError:
                    raise TargetExists(errno.EEXIST, "the target already exists") from None
                return plan_.target
            # Claim the name atomically with an empty placeholder (fails if
            # anything is there), then move the copy over our own placeholder.
            try:
                fd = os.open(plan_.target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                raise TargetExists(errno.EEXIST, "the target already exists") from None
            os.close(fd)
            try:
                os.rename(plan_.temporary, plan_.target)
            except OSError:
                try:
                    os.unlink(plan_.target)
                except OSError:
                    pass
                raise
            return plan_.target
        os.unlink(plan_.temporary)
        return plan_.target
    # A folder: reserve the name with an empty folder (fails if it exists),
    # then move the copy onto it.
    try:
        os.mkdir(plan_.target)
    except FileExistsError:
        raise TargetExists(errno.EEXIST, "the target already exists") from None
    try:
        if sys.platform == "win32":
            os.rmdir(plan_.target)
            os.rename(plan_.temporary, plan_.target)  # never replaces on Windows
        else:
            os.rename(plan_.temporary, plan_.target)  # replaces only our empty folder
    except OSError:
        try:
            os.rmdir(plan_.target)
        except OSError:
            pass
        raise
    return plan_.target
