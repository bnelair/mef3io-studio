"""What "Write to file" would do, as a list a person can read (Qt-free).

Every change to the notes stays in the draft, in memory, until the file is written. The red bar
on top of the viewer says how many there are; the confirmation before the write lists every
one of them (added, changed, removed), per recording when there are several. This module turns
the draft into those rows. It is linear in the number of changes, never in the number of notes
that are stored.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from . import annotation_save as asave
from .annotation_draft import Draft

ADDED, CHANGED, REMOVED = "added", "changed", "removed"


@dataclass(frozen=True)
class ChangeRow:
    kind: str
    #: Index of the recording the note is (or will be) in; None = not decided yet (the choice
    #: is asked for when writing).
    recording: int | None
    time: int
    end: int | None
    code: str  # the record type ("Note", "EDFA" ...)
    text: str
    #: A changed note: what it was (time and text).
    old_time: int | None = None
    old_text: str = ""
    #: A removed note that is stored more than once: "1 of 2 identical copies", "all 2 ...".
    copies: str = ""


@dataclass
class ChangeSummary:
    rows: list[ChangeRow] = field(default_factory=list)
    added: int = 0
    changed: int = 0
    removed: int = 0
    #: recording -> (added, changed, removed); None = not decided yet
    per_recording: dict = field(default_factory=dict)
    #: Plain sentences about what is NOT as it was when the notes were marked (another program
    #: changed the recording): the confirmation says them before anything can be written.
    warnings: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.added + self.changed + self.removed

    def counts_text(self) -> str:
        return counts_text(self.added, self.changed, self.removed)


def counts_text(added: int, changed: int, removed: int) -> str:
    """ "3 added, 1 changed, 2 removed" (what is zero is left out)."""
    parts = [
        f"{n} {word}"
        for n, word in ((added, "added"), (changed, "changed"), (removed, "removed"))
        if n
    ]
    return ", ".join(parts)


def summarise(
    draft: Draft,
    routed: Callable[[int], int | None],
    copies_of: Callable[[object], int],
) -> ChangeSummary:
    """The draft as rows. ``routed(uid)``: the recording a new note goes to (None when it is
    chosen at the write). ``copies_of(mark)``: how many identical copies of a stored note exist."""
    out = ChangeSummary()

    def count(recording, kind: str, n: int) -> None:
        a, c, r = out.per_recording.get(recording, (0, 0, 0))
        out.per_recording[recording] = (
            (a + n, c, r) if kind == ADDED else (a, c + n, r) if kind == CHANGED else (a, c, r + n)
        )

    for item in draft.additions():
        recording = item.recording if item.recording is not None else routed(item.uid)
        out.rows.append(
            ChangeRow(ADDED, recording, item.time, item.end, item.code, item.stored_text)
        )
        out.added += 1
        count(recording, ADDED, 1)
    for stored, item in draft.changed_stored():
        record = asave.replacement_record(stored, item)
        out.rows.append(
            ChangeRow(
                CHANGED,
                stored.recording,
                item.time,
                item.end,
                record["type"],
                record["text"],
                stored.time,
                stored.text,
            )
        )
        out.changed += 1
        count(stored.recording, CHANGED, 1)
    for stored in draft.removed_stored():
        n, total = draft.removed_copies(stored), max(1, copies_of(stored))
        note = (
            ""
            if total == 1
            else (
                f"all {total} identical copies"
                if n >= total
                else f"{n} of {total} identical copies"
            )
        )
        out.rows.append(
            ChangeRow(
                REMOVED,
                stored.recording,
                stored.time,
                stored.end,
                stored.code,
                stored.text,
                copies=note,
            )
        )
        out.removed += n
        count(stored.recording, REMOVED, n)
    out.rows.sort(key=lambda r: (r.time, r.kind))
    return out
