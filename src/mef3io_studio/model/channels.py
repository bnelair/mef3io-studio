"""Which channels go into an anonymized copy: the picker's rules.

The wizard's channel step (mef3io-studio#5, the 256-channel comment): every
channel is kept by default and excluding is the deliberate act. This module
holds everything that step decides, as plain data, so it is tested without
Qt and stays fast with hundreds of channels:

* **Groups** by electrode name prefix (``LA1 … LA12`` → ``LA``), in the order
  they first appear; channels sorted naturally inside a group (``LA2`` before
  ``LA10``). Channels whose prefix nobody else shares go to one "other" group,
  so 256 channels fold into a short list.
* **Ranges:** :meth:`ChannelSelection.set_range` applies one state to every
  channel between two others, in the order the list shows them.
* **Filter:** :meth:`ChannelSelection.matching` is what a filter text shows;
  bulk actions apply to exactly that.
* **Undo / redo** of every change, inside the step.
* **Batch:** the selection is by NAME and covers every recording of the
  batch. A name that is not in every recording is flagged
  (:meth:`ChannelSelection.missing_from`), never silently ignored.

Plain Python, no Qt. Never logs anything (channel names can identify).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: The group of channels whose prefix no other channel shares.
OTHER_GROUP = ""

_PREFIX = re.compile(r"^(.*?)[\s_\-.]*\d+$")
_NATURAL = re.compile(r"(\d+)")
#: Recorder labels in front of the electrode name ("EEG LA1", "POL RH3").
_LEAD_LABEL = re.compile(r"^(?:eeg|pol|seeg|ecog)[\s_]+", re.IGNORECASE)
#: Reference labels after it ("LA1-Ref", "LA1-AVG").
_REFERENCE = re.compile(r"[\s_\-]+(?:ref|avg|ave|car)$", re.IGNORECASE)
#: A bipolar pair: the first contact, a dash, the second ("LAH1-LAH2").
_BIPOLAR = re.compile(r"^([^\W\d_][\w ]*?\d+)\s*-\s*[^\W\d_][\w ]*?\d+$")


def contact_of(name: str) -> str:
    """The electrode contact a channel name stands for, without recorder
    labels and references: ``EEG LA1-Ref`` → ``LA1``; a bipolar pair
    ``LAH1-LAH2`` → its first contact ``LAH1``."""
    text = _LEAD_LABEL.sub("", name.strip())
    text = _REFERENCE.sub("", text).strip()
    bipolar = _BIPOLAR.match(text)
    if bipolar:
        text = bipolar.group(1)
    return text


def prefix_of(name: str) -> str:
    """The electrode a channel belongs to: its contact without the trailing
    number (and the separator before it). ``LA12`` → ``LA``, ``RH-3`` →
    ``RH``, ``EEG LA1-Ref`` → ``LA``, ``LAH1-LAH2`` → ``LAH``; a name without
    a trailing number is its own prefix. Compare prefixes with
    :func:`group_key` (case does not matter)."""
    contact = contact_of(name)
    match = _PREFIX.match(contact)
    if match and match.group(1):
        return match.group(1)
    return contact


def group_key(name: str) -> str:
    """What channels are grouped by: the prefix, case-folded."""
    return prefix_of(name).casefold()


def natural_key(name: str) -> tuple:
    """Sort key that puts ``LA2`` before ``LA10``."""
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part.casefold())
        for part in _NATURAL.split(name)
        if part
    )


@dataclass(frozen=True)
class ChannelRow:
    """One channel as the picker lists it."""

    name: str
    #: Sampling rate and unit as shown (e.g. "256 Hz", "µV"); in a batch,
    #: the first recording's, or "varies".
    rate: str = ""
    unit: str = ""
    #: In how many of the batch's recordings this name exists.
    present_in: int = 1


@dataclass(frozen=True)
class ChannelGroup:
    prefix: str
    rows: tuple[ChannelRow, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.rows)


def group_channels(rows: Sequence[ChannelRow]) -> tuple[ChannelGroup, ...]:
    """Groups by prefix, in order of first appearance; prefixes with one
    channel only are pooled into one :data:`OTHER_GROUP` at the end."""
    by_prefix: dict[str, list[ChannelRow]] = {}
    shown: dict[str, str] = {}  # the first spelling seen names the group
    for row in rows:
        key = group_key(row.name)
        shown.setdefault(key, prefix_of(row.name))
        by_prefix.setdefault(key, []).append(row)
    groups, other = [], []
    for key, members in by_prefix.items():
        prefix = shown[key]
        if len(members) == 1:
            other.extend(members)
        else:
            groups.append(
                ChannelGroup(prefix, tuple(sorted(members, key=lambda r: natural_key(r.name))))
            )
    if other:
        groups.append(
            ChannelGroup(OTHER_GROUP, tuple(sorted(other, key=lambda r: natural_key(r.name))))
        )
    return tuple(groups)


class ChannelSelection:
    """The kept channels of one wizard run, with undo and redo.

    ``recordings`` maps an opaque recording key to the channel names it has.
    Every name of every recording is listed once; all are kept at the start.
    """

    def __init__(
        self,
        recordings: dict[str, Sequence[str]],
        rows: Sequence[ChannelRow] | None = None,
    ):
        self.recordings = {k: tuple(v) for k, v in recordings.items()}
        if rows is None:
            seen: dict[str, int] = {}
            for names in self.recordings.values():
                for n in names:
                    seen[n] = seen.get(n, 0) + 1
            rows = [ChannelRow(n, present_in=c) for n, c in seen.items()]
        self.groups = group_channels(rows)
        #: Every channel in the order the list shows it (group by group).
        self.order: tuple[str, ...] = tuple(n for g in self.groups for n in g.names)
        self._position = {n: i for i, n in enumerate(self.order)}
        self.rows = {r.name: r for g in self.groups for r in g.rows}
        self._kept: frozenset[str] = frozenset(self.order)
        self._undo: list[frozenset[str]] = []
        self._redo: list[frozenset[str]] = []

    # -- reading -------------------------------------------------------------
    @property
    def kept(self) -> frozenset[str]:
        return self._kept

    @property
    def excluded(self) -> tuple[str, ...]:
        """Excluded names, in list order."""
        return tuple(n for n in self.order if n not in self._kept)

    @property
    def total(self) -> int:
        return len(self.order)

    def is_kept(self, name: str) -> bool:
        return name in self._kept

    def group_state(self, group: ChannelGroup) -> bool | None:
        """True: all kept; False: none; None: some (a tri-state box)."""
        kept = sum(1 for n in group.names if n in self._kept)
        if kept == len(group.names):
            return True
        return False if kept == 0 else None

    def matching(self, text: str) -> tuple[str, ...]:
        """The channels a filter text shows: those whose name contains the
        text (ignoring case), in list order. Empty text: all."""
        needle = text.strip().casefold()
        if not needle:
            return self.order
        return tuple(n for n in self.order if needle in n.casefold())

    def missing_from(self) -> dict[str, tuple[str, ...]]:
        """For a batch: every name that is NOT in every recording, with the
        keys of the recordings that lack it."""
        out: dict[str, tuple[str, ...]] = {}
        if len(self.recordings) < 2:
            return out
        have = {k: set(v) for k, v in self.recordings.items()}
        for name in self.order:
            lacking = tuple(k for k, names in have.items() if name not in names)
            if lacking:
                out[name] = lacking
        return out

    def kept_in(self, key: str) -> tuple[str, ...]:
        """The channels one recording keeps."""
        return tuple(n for n in self.recordings[key] if n in self._kept)

    def excluded_in(self, key: str) -> tuple[str, ...]:
        """The channels one recording leaves out (what goes to mef3io's
        ``exclude_channels=``)."""
        return tuple(n for n in self.recordings[key] if n not in self._kept)

    def recordings_left_empty(self) -> tuple[str, ...]:
        """Recordings that would keep no channel at all."""
        return tuple(k for k in self.recordings if not self.kept_in(k))

    @property
    def can_continue(self) -> bool:
        return bool(self._kept) and not self.recordings_left_empty()

    # -- changing --------------------------------------------------------------
    def _apply(self, kept: Iterable[str]) -> bool:
        new = frozenset(kept) & frozenset(self.order)
        if new == self._kept:
            return False
        self._undo.append(self._kept)
        self._redo.clear()
        self._kept = new
        return True

    def set_kept(self, names: Iterable[str], keep: bool) -> bool:
        """Keep (or exclude) every name given. Returns whether anything
        changed (an unchanged click adds no undo step)."""
        names = set(names)
        return self._apply(self._kept | names if keep else self._kept - names)

    def toggle(self, name: str) -> bool:
        return self.set_kept([name], name not in self._kept)

    def set_range(
        self, anchor: str, name: str, keep: bool, visible: Sequence[str] | None = None
    ) -> bool:
        """Apply ``keep`` to every channel from ``anchor`` to ``name``
        inclusive, in the order shown (``visible``: the filtered list, so a
        range never reaches hidden channels)."""
        order = list(visible) if visible is not None else list(self.order)
        if anchor not in order or name not in order:
            return self.set_kept([name], keep)
        i, j = sorted((order.index(anchor), order.index(name)))
        return self.set_kept(order[i : j + 1], keep)

    def keep_all(self) -> bool:
        return self._apply(self.order)

    def exclude_all(self) -> bool:
        return self._apply(())

    def invert(self) -> bool:
        return self._apply(n for n in self.order if n not in self._kept)

    def replace_kept(self, names: Iterable[str]) -> bool:
        """Set the kept channels exactly (one undo step)."""
        return self._apply(names)

    def clear_history(self) -> None:
        """Forget undo and redo (a new batch starts its own history)."""
        self._undo.clear()
        self._redo.clear()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._kept)
        self._kept = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._kept)
        self._kept = self._redo.pop()
        return True

    def position(self, name: str) -> int:
        return self._position[name]
