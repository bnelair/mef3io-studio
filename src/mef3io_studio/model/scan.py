"""Find every recording under a folder.

A recording is a folder ending in ``.mefd`` or a file ending in
``.mefd.tar`` (mef3io enforces these names). The walk never goes inside a
recording, never follows links to folders (no loops through shortcuts), and
skips folders it is not allowed to read instead of stopping. Only names and
folder entries are read here, never file contents.

Plain Python, no Qt.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from .cancel import CancelToken

#: Unfinished pack/unpack output of this app (see :mod:`.archive`).
TEMP_PREFIX = ".mef3io-studio-"

ReportFn = Callable[[float, float | None, str], None]


class NotAFolder(NotADirectoryError):
    """What was given to scan is not a folder (nor a recording)."""


@dataclass(frozen=True)
class ScanResult:
    locations: tuple[str, ...]
    folders_searched: int
    #: Folders that could not be read (no permission, disconnected).
    folders_skipped: int


def find_recordings(
    root: str,
    cancel: CancelToken | None = None,
    report: ReportFn | None = None,
) -> ScanResult:
    cancel = cancel or CancelToken()
    report = report or (lambda done, total, phase: None)
    if not os.path.isdir(root):
        raise NotAFolder("not a folder")
    found: list[str] = []
    searched = skipped = 0
    stack = [os.fspath(root)]
    while stack:
        cancel.raise_if_cancelled()
        folder = stack.pop()
        searched += 1
        if searched % 25 == 0:
            report(searched, None, f"Looking for recordings: {searched} folders searched")
        try:
            with os.scandir(folder) as entries:
                children = sorted(entries, key=lambda e: e.name)
        except OSError:
            skipped += 1
            continue
        subfolders = []
        for entry in children:
            name = entry.name
            if name.startswith(TEMP_PREFIX):
                continue  # an unfinished copy of this app's, not a recording
            lowered = name.lower()
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = not is_dir and entry.is_file()
            except OSError:
                continue
            if is_dir and lowered.endswith(".mefd"):
                found.append(entry.path)
            elif is_file and lowered.endswith(".mefd.tar"):
                found.append(entry.path)
            elif is_dir:
                subfolders.append(entry.path)
        stack.extend(reversed(subfolders))
    report(searched, searched, f"Looking for recordings: {searched} folders searched")
    return ScanResult(tuple(sorted(found)), searched, skipped)
