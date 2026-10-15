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

from .recording import is_packed, recording_name
from .scan import TEMP_PREFIX


class TargetExists(FileExistsError):
    """Something with the target's name is already there."""


class NotEnoughSpace(OSError):
    def __init__(self, needed: int, free: int):
        super().__init__(errno.ENOSPC, f"needs about {needed} bytes, {free} free")
        self.needed = needed
        self.free = free


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
    source = os.path.normpath(os.fspath(source))
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


def remove_temporary(plan_: ArchivePlan) -> None:
    """Remove the unfinished copy (it is ours: opaque random name)."""
    path = plan_.temporary
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def written_bytes(plan_: ArchivePlan) -> int:
    """How much of the copy exists so far (for progress)."""
    path = plan_.temporary
    try:
        if os.path.isfile(path):
            return os.stat(path).st_size
        total = 0
        for folder, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.stat(os.path.join(folder, name)).st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _shape(location: str) -> list[tuple[str, float, tuple[tuple[int, int, int, int], ...]]]:
    """Channels, rates and every part's times and sample count."""
    from .inspect import inspect_recording

    outcome = inspect_recording(location)
    if outcome.details is None:
        raise CopyMismatch("the copy could not be opened")
    return [
        (
            c.name,
            c.sampling_frequency,
            tuple((p.start, p.end, p.samples, p.blocks) for p in c.parts),
        )
        for c in outcome.details.channels
    ]


def run_in_helper(plan_: ArchivePlan, send=None) -> None:
    """mef3io writes the temporary copy, then the copy is compared with the
    original. Runs in a helper process; never touches the final name.
    ``send("checking")`` announces the second step."""
    import mef3io

    if plan_.packing:
        mef3io.archive_session(plan_.source, plan_.temporary, False)
    else:
        mef3io.extract_session(plan_.source, plan_.temporary, False)
    if send is not None:
        send("checking")
    if _shape(plan_.source) != _shape(plan_.temporary):
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
            if os.path.lexists(plan_.target):
                raise TargetExists(errno.EEXIST, "the target already exists") from None
            os.rename(plan_.temporary, plan_.target)  # Windows: never replaces
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
