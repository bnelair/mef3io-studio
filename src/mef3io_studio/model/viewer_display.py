"""The notes the viewer shows: the stored ones with the draft's changes on top,
made fast for a recording with tens of thousands of notes (Qt-free, numpy).

What is derived from the STORED notes (their type, time, end) is made once per
read of the notes (:class:`Stored`); a change to the draft only inserts or
flags a few entries (:func:`compose`), so adding one note to 60,000 costs a
few array operations, not a walk over every note in Python.
"""

from __future__ import annotations

import bisect
import dataclasses
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

import numpy as np

from . import viewer_marks as vm


class Stored:
    """Per-note facts of a list of STORED marks, for one answer to "is this
    recording's text shown": the type of each note as a small number, its time
    and its end."""

    def __init__(self, marks: Sequence[vm.Mark], shown: Callable[[int], bool]):
        self.marks = marks
        n = len(marks)
        self.keys: list[str] = []  # code -> type key
        self.labels: list[str] = []  # code -> plain label
        table: dict[str, int] = {}
        codes = np.empty(n, dtype=np.int32)
        times = np.empty(n, dtype=np.int64)
        ends = np.empty(n, dtype=np.int64)
        shown_of: dict[int, bool] = {}
        for i, m in enumerate(marks):
            ok = shown_of.get(m.recording)
            if ok is None:
                ok = shown_of[m.recording] = bool(shown(m.recording))
            key, label = vm.kind_for(m, ok)
            code = table.get(key)
            if code is None:
                code = table[key] = len(self.keys)
                self.keys.append(key)
                self.labels.append(label)
            codes[i] = code
            times[i] = m.time
            ends[i] = max(m.end, m.time) if m.end else m.time
        self.codes, self.times, self.ends = codes, times, ends
        self._positions: dict[vm.Mark, list[int]] | None = None

    def positions(self, mark: vm.Mark) -> list[int]:
        """Where ``mark`` (or a mark equal to it) is in the list."""
        if self._positions is None:
            found: dict[vm.Mark, list[int]] = {}
            for i, m in enumerate(self.marks):
                found.setdefault(m, []).append(i)
            self._positions = found
        return self._positions.get(mark, [])


@dataclass
class Display:
    """What :func:`compose` makes."""

    #: Every note, the draft's changes on top (a taken-out or changed stored
    #: note stays, marked "removed"; a new one is added), sorted.
    marks: list[vm.Mark]
    #: The notes of the types that are not hidden, with arrays over them.
    visible: list[vm.Mark]
    keys: list[str]
    times: list[int]
    times_np: np.ndarray
    ends_np: np.ndarray
    importance_np: np.ndarray
    drafted: list[int]
    drafted_np: np.ndarray
    max_span: int
    #: ``(key, label, how many)`` of every type among ``marks``, most first.
    counts: list[tuple[str, str, int]]


def _positions(stored: Stored, inserts: Sequence[vm.Mark]) -> np.ndarray:
    """Where each new mark goes among the stored ones, which are sorted by
    ``(time, recording, channel)``: after the notes that sort before or with it."""
    times = np.fromiter((m.time for m in inserts), dtype=np.int64, count=len(inserts))
    lo = np.searchsorted(stored.times, times, side="left")
    hi = np.searchsorted(stored.times, times, side="right")
    out = hi.copy()
    for j, m in enumerate(inserts):
        if hi[j] > lo[j]:  # notes at the very same time: put it among them by the rest
            run = stored.marks
            out[j] = (
                bisect.bisect_right(
                    range(lo[j], hi[j]),
                    (m.recording, m.channel or ""),
                    key=lambda i: (run[i].recording, run[i].channel or ""),
                )
                + lo[j]
            )
    return out


def compose(
    stored: Stored,
    drafted: Sequence[vm.Mark],
    changed: Collection[vm.Mark],
    hidden: Collection[str],
    shown: Callable[[int], bool],
) -> Display:
    """The stored notes with the draft on top. ``drafted``: the marks of the
    draft's new and changed notes (state "new" / "changed"); ``changed``: the
    stored marks the draft takes out or replaces (shown struck through)."""
    marks = stored.marks
    shown_of: dict[int, bool] = {}
    keys = list(stored.keys)
    labels = list(stored.labels)
    table = {k: i for i, k in enumerate(keys)}
    display = list(marks)
    flagged: list[int] = []  # stored positions that are drafted (taken out / replaced)
    used: dict[vm.Mark, int] = {}
    for mark in changed:  # a mark given twice is two identical copies taken out or replaced
        places = stored.positions(mark)
        k = used.get(mark, 0)
        if k < len(places):
            i = places[k]
            display[i] = dataclasses.replace(marks[i], state="removed")
            flagged.append(i)
            used[mark] = k + 1
    inserts = sorted(drafted, key=lambda m: (m.time, m.recording, m.channel or ""))
    n_new = len(inserts)
    codes, times, ends = stored.codes, stored.times, stored.ends
    insert_at = np.empty(0, dtype=np.int64)
    if n_new:
        new_codes = np.empty(n_new, dtype=np.int32)
        new_times = np.empty(n_new, dtype=np.int64)
        new_ends = np.empty(n_new, dtype=np.int64)
        for j, m in enumerate(inserts):
            ok = shown_of.get(m.recording)
            if ok is None:
                ok = shown_of[m.recording] = bool(shown(m.recording))
            key, label = vm.kind_for(m, ok)
            code = table.get(key)
            if code is None:
                code = table[key] = len(keys)
                keys.append(key)
                labels.append(label)
            new_codes[j] = code
            new_times[j] = m.time
            new_ends[j] = max(m.end, m.time) if m.end else m.time
        insert_at = _positions(stored, inserts)
        codes = np.insert(codes, insert_at, new_codes)
        times = np.insert(times, insert_at, new_times)
        ends = np.insert(ends, insert_at, new_ends)
        merged: list[vm.Mark] = []
        prev = 0
        for j, at in enumerate(insert_at.tolist()):
            merged.extend(display[prev:at])
            merged.append(inserts[j])
            prev = at
        merged.extend(display[prev:])
        display = merged
    n = len(display)
    flags = np.zeros(n, dtype=bool)
    if n_new:
        flags[insert_at + np.arange(n_new)] = True
    if flagged:
        old = np.asarray(sorted(flagged), dtype=np.int64)
        flags[old + np.searchsorted(insert_at, old, side="right")] = True
    counts = np.bincount(codes, minlength=len(keys)) if n else np.zeros(len(keys), dtype=np.int64)
    ordered = sorted(
        ((keys[c], labels[c], int(counts[c])) for c in range(len(keys)) if counts[c]),
        key=lambda x: (-x[2], x[1]),
    )
    hidden_codes = [table[k] for k in hidden if k in table]
    if hidden_codes:
        idx = np.flatnonzero(~np.isin(codes, hidden_codes))
    else:
        idx = np.arange(n)
    visible = display if len(idx) == n else [display[i] for i in idx.tolist()]
    vcodes = codes[idx]
    rank_by_code = np.fromiter(
        (vm.kind_importance(k) for k in keys), dtype=np.int64, count=len(keys)
    )
    times_v = times[idx]
    ends_v = ends[idx]
    drafted_np = flags[idx]
    return Display(
        marks=display,
        visible=visible,
        keys=[keys[c] for c in vcodes.tolist()],
        times=times_v.tolist(),
        times_np=times_v,
        ends_np=ends_v,
        importance_np=rank_by_code[vcodes] if len(vcodes) else np.zeros(0, dtype=np.int64),
        drafted=np.flatnonzero(drafted_np).tolist(),
        drafted_np=drafted_np,
        max_span=int((ends_v - times_v).max()) if len(idx) else 0,
        counts=ordered,
    )
