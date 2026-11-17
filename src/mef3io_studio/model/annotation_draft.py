"""The signal viewer's edit mode, without Qt or the library: the draft of
notes and events, undo and redo, where each new one is saved, importing and
exporting them (docs/design/viewer.md, "Annotations", "Edit mode").

Only ADDITIONS are ever written (the app appends to a recording's notes and
never rewrites one). A change to, or removal of, a note that is already
stored stays in the draft and is shown, but :meth:`Draft.save_plan` reports
it as needing a new copy and nothing here writes it.

Privacy: the text of a note is identifying. Nothing here logs, and ``repr``
of an item leaves its text out.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, tzinfo

from . import timefmt
from . import viewer_marks as vm

#: What the app calls a plain note (stored with no label in front of the text).
PLAIN = "Note"

#: The quick types offered by default, in key order (1 = first).
DEFAULT_QUICK_TYPES = ("Note", "Seizure onset", "Seizure end", "Spike", "Artifact", "Sleep")

#: The columns of an import / export file.
IMPORT_COLUMNS = ("start", "end", "type", "text", "channel")
EXPORT_COLUMNS = ("start", "end", "type", "text", "channel", "start_us", "end_us")

#: Said in the save dialog of an export.
EXPORT_NOTE = (
    "The file holds the text of notes and real dates and times. It can identify the "
    "recordings, so store and share it with the same care."
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_ROWS = 200_000

# --- text a file can hold ------------------------------------------------------------


def control_problem(*texts: str | None) -> str:
    """A plain reason when a text holds a control character (NUL and the
    other C0 controls except tab and newline, DEL, C1 controls, the line and
    paragraph separators, or a lone surrogate), else "". Such
    characters would be stored in a note and break other tools."""
    for text in texts:
        for ch in text or "":
            o = ord(ch)
            if (
                (o < 32 and ch not in "\t\n")
                or 127 <= o <= 159
                or o in (0x2028, 0x2029)
                or 0xD800 <= o <= 0xDFFF
            ):
                return (
                    "The text holds a control character (an invisible character such as a "
                    "null or a bell). Remove it and try again."
                )
    return ""


def _refuse_controls(*texts: str | None) -> None:
    problem = control_problem(*texts)
    if problem:
        raise ValueError(problem)


# --- quick types (a preference: a list of labels) ------------------------------------


def parse_quick_types(raw: str | None) -> tuple[str, ...]:
    """The quick types from the preference text (one per line, at most 9);
    the defaults when it is empty."""
    labels: list[str] = []
    for line in (raw or "").splitlines():
        label = " ".join(line.split())
        if (
            label
            and not control_problem(label)
            and label.casefold() not in {x.casefold() for x in labels}
        ):
            labels.append(label[:40])
    return tuple(labels[:9]) or DEFAULT_QUICK_TYPES


def quick_types_text(labels: Sequence[str]) -> str:
    return "\n".join(labels)


# --- the text that is stored ---------------------------------------------------------


def compose_text(label: str, text: str) -> str:
    """The stored text: ``"Spike: sharp wave"``. A plain note has no label."""
    label = " ".join((label or PLAIN).split())
    text = (text or "").strip()
    if label.casefold() == PLAIN.casefold():
        return text
    return f"{label}: {text}" if text else f"{label}:"


def split_text(stored: str | None) -> tuple[str, str]:
    """``(label, text)`` of a stored text: the quick type it starts with, else
    :data:`PLAIN` and the whole text."""
    stored = stored or ""
    label = vm.quick_label(stored)
    if label is None:
        return PLAIN, stored
    return label, stored.split(":", 1)[1].strip()


# --- the draft --------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftItem:
    """One note or event of the draft: new, or the changed version of a stored
    one (``replaces``)."""

    uid: int
    time: int
    end: int | None
    label: str
    text: str = field(repr=False, default="")
    channel: str | None = None
    #: The recording chosen for it; None = decided by where it falls.
    recording: int | None = None
    #: The stored note this changes (None = a new one).
    replaces: object = field(default=None, compare=False, repr=False)
    imported: bool = False

    @property
    def is_span(self) -> bool:
        return self.end is not None and self.end > self.time

    @property
    def stored_text(self) -> str:
        return compose_text(self.label, self.text)

    @property
    def code(self) -> str:
        """The record type it is stored as: a point is a Note, a span an EDFA."""
        return "EDFA" if self.is_span else "Note"

    def record(self) -> dict:
        """What is handed to the library."""
        rec = {"type": self.code, "time": int(self.time), "text": self.stored_text}
        if self.is_span:
            rec["duration"] = int(self.end - self.time)
        return rec


#: Said when a note equals one that is already stored / already in the draft.
DUPLICATE_STORED = (
    "This note is already stored: the recording holds one with the same time, length, "
    "type, text and channel."
)
DUPLICATE_DRAFT = (
    "This note is already in the draft: there is one with the same time, length, type, "
    "text and channel."
)


class DuplicateNote(ValueError):
    """A note that is the same as one already stored or already in the draft."""


def note_key(time, code, end, text, channel) -> tuple:
    """What makes two notes the SAME note: time, record type, length, stored text
    and channel. ONE rule: the draft refuses a copy of a stored or drafted note
    with it, a save leaves such a copy out, and :func:`items_now_stored` finds
    what a save that ended in an unknown place has stored with it."""
    duration = int(end) - int(time) if end is not None and int(end) > int(time) else 0
    return (int(time), code, duration, text or "", channel or None)


def mark_key(mark) -> tuple:
    """:func:`note_key` of a stored note (a ``Mark``)."""
    return note_key(mark.time, mark.code, mark.end, mark.text, mark.channel)


def item_key(item: DraftItem) -> tuple:
    """:func:`note_key` of a draft item as it would be stored."""
    return note_key(item.time, item.code, item.end, item.stored_text, item.channel)


def _same_place(a: DraftItem, b: DraftItem) -> bool:
    """Whether two items could go to the same recording: not when each is chosen for another."""
    return a.recording is None or b.recording is None or a.recording == b.recording


def items_now_stored(items: Sequence[DraftItem], before, after) -> list[int]:
    """The uids of the draft's new items that the recordings hold now, though
    nothing said so (a save that ended without telling what it had written):
    the same note (:func:`note_key`), counted against what the recordings
    held ``before`` (the marks of the stored notes then) and ``after`` (now), so
    a note that was already there is not taken for ours, and equal items are
    taken only as often as the recordings gained them."""
    gained: dict[tuple, int] = {}
    for m in after:
        k = mark_key(m)
        gained[k] = gained.get(k, 0) + 1
    for m in before:
        k = mark_key(m)
        gained[k] = gained.get(k, 0) - 1
    out = []
    for it in items:
        k = item_key(it)
        if gained.get(k, 0) > 0:
            gained[k] -= 1
            out.append(it.uid)
    return out


@dataclass(frozen=True)
class _Snapshot:
    items: tuple[DraftItem, ...]
    removed: frozenset


class Draft:
    """The changes made in edit mode, kept in memory until saved. Undo and
    redo step through them. A stored note is referred to by its ``Mark``."""

    def __init__(self) -> None:
        self._items: dict[int, DraftItem] = {}
        self._removed: set = set()
        self._undo: list[_Snapshot] = []
        self._redo: list[_Snapshot] = []
        self._next = 1
        #: Bumped by every change (a view of the draft is rebuilt only when it moved).
        self.version = 0
        #: Gives the keys (:func:`mark_key`) of the notes already stored; set by
        #: the window. None = nothing is stored (as in a test).
        self.stored_keys: Callable[[], Collection[tuple]] | None = None
        #: The same notes as ``(recording index, key)`` pairs: a note chosen for ONE recording is
        #: the same as a stored note only of that recording. None = judged without the recording.
        self.stored_pairs: Callable[[], Collection[tuple]] | None = None
        #: How many rows the last :meth:`add_many` left out as copies.
        self.skipped_duplicates = 0

    # -- state ----------

    def _snap(self) -> _Snapshot:
        return _Snapshot(tuple(self._items.values()), frozenset(self._removed))

    def _restore(self, snap: _Snapshot) -> None:
        self._items = {i.uid: i for i in snap.items}
        self._removed = set(snap.removed)

    def _push(self) -> None:
        self.version += 1
        self._undo.append(self._snap())
        self._redo.clear()

    @property
    def dirty(self) -> bool:
        return bool(self._items or self._removed)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self.version += 1
        self._redo.append(self._snap())
        self._restore(self._undo.pop())
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self.version += 1
        self._undo.append(self._snap())
        self._restore(self._redo.pop())
        return True

    def written(self, uids: Iterable[int]) -> None:
        """These items were saved: they leave the draft, and so does the
        undo history (a saved addition cannot be undone in place)."""
        self.version += 1
        for uid in set(uids):
            self._items.pop(uid, None)
        self._undo.clear()
        self._redo.clear()

    def clear(self) -> None:
        """Forget everything (after a discard)."""
        self.version += 1
        self._items.clear()
        self._removed.clear()
        self._undo.clear()
        self._redo.clear()

    # -- reading ----------

    @property
    def items(self) -> list[DraftItem]:
        return sorted(self._items.values(), key=lambda i: (i.time, i.uid))

    def additions(self) -> list[DraftItem]:
        """The new notes and events (what a save can write)."""
        return [i for i in self.items if i.replaces is None]

    def changed_stored(self) -> list[tuple[object, DraftItem]]:
        """``(stored note, its new version)`` for every stored note changed."""
        return [(i.replaces, i) for i in self.items if i.replaces is not None]

    def removed_stored(self) -> list:
        return list(self._removed)

    def is_removed(self, stored) -> bool:
        return stored in self._removed

    def changed_version(self, stored) -> DraftItem | None:
        return next((i for i in self._items.values() if i.replaces == stored), None)

    def get(self, uid: int) -> DraftItem | None:
        return self._items.get(uid)

    @property
    def stored_changes(self) -> int:
        """How many stored notes are changed or removed (these need a new copy)."""
        return len(self._removed) + len(self.changed_stored())

    # -- the same note twice ----------

    def duplicate_reason(self, item: DraftItem, ignore_uid: int | None = None) -> str:
        """One sentence when ``item`` is the same note as a stored one or as
        another item of the draft (not ``ignore_uid``), else ""."""
        key = item_key(item)
        if item.recording is not None and self.stored_pairs is not None:
            if (item.recording, key) in self.stored_pairs():
                return DUPLICATE_STORED
        elif self.stored_keys is not None and key in self.stored_keys():
            return DUPLICATE_STORED
        for other in self._items.values():
            if other.uid != ignore_uid and item_key(other) == key and _same_place(item, other):
                return DUPLICATE_DRAFT
        return ""

    def _refuse_duplicate(self, item: DraftItem, ignore_uid: int | None = None) -> None:
        reason = self.duplicate_reason(item, ignore_uid)
        if reason:
            raise DuplicateNote(reason)

    # -- changing ----------

    def add(
        self,
        time: int,
        end: int | None = None,
        label: str = PLAIN,
        text: str = "",
        channel: str | None = None,
        recording: int | None = None,
        imported: bool = False,
    ) -> DraftItem:
        if end is not None and end <= time:
            end = None
        _refuse_controls(label, text, channel)
        item = DraftItem(
            self._next, int(time), None if end is None else int(end), label, text, channel,
            recording, None, imported,
        )  # fmt: skip
        self._refuse_duplicate(item)
        self._push()
        self._next += 1
        self._items[item.uid] = item
        return item

    def add_many(self, rows: Iterable[dict]) -> int:
        """Add imported rows as ONE undo step. A row that is the same note as a
        stored one, one of the draft or an earlier row is left out
        (:attr:`skipped_duplicates` says how many); returns how many were added."""
        rows = list(rows)
        self.skipped_duplicates = 0
        if not rows:
            return 0
        for r in rows:
            _refuse_controls(r.get("label"), r.get("text"), r.get("channel"))
        fresh: list[DraftItem] = []
        keys: set[tuple] = set()
        number = self._next
        for r in rows:
            end = r.get("end")
            item = DraftItem(
                number, int(r["time"]), int(end) if end and end > r["time"] else None,
                r.get("label") or PLAIN, r.get("text") or "", r.get("channel") or None,
                r.get("recording"), None, True,
            )  # fmt: skip
            key = (item.recording, item_key(item))
            if key in keys or self.duplicate_reason(item):
                self.skipped_duplicates += 1
                continue
            keys.add(key)
            fresh.append(item)
            number += 1
        if not fresh:
            return 0
        self._push()
        for item in fresh:
            self._items[item.uid] = item
        self._next = number
        return len(fresh)

    def update(self, uid: int, **changes) -> DraftItem:
        """Change a draft item (new, or the changed version of a stored one)."""
        item = self._items[uid]
        new = replace(item, **changes)
        _refuse_controls(new.label, new.text, new.channel)
        if new.end is not None and new.end <= new.time:
            new = replace(new, end=None)
        self._refuse_duplicate(new, ignore_uid=uid)
        self._push()
        self._items[uid] = new
        return new

    def edit_stored(self, stored, **changes) -> DraftItem:
        """Change a stored note: kept in the draft (the stored one is shown
        struck through) and never written in place."""
        existing = self.changed_version(stored)
        if existing is not None:
            return self.update(existing.uid, **changes)
        label, text = split_text(getattr(stored, "text", ""))
        base = dict(
            time=stored.time,
            end=stored.end,
            label=label,
            text=text,
            channel=stored.channel,
            recording=stored.recording,
        )
        base.update(changes)
        _refuse_controls(base["label"], base["text"], base["channel"])
        end = base["end"]
        if end is not None and end <= base["time"]:
            base["end"] = None
        self._push()
        item = DraftItem(self._next, replaces=stored, **base)
        self._next += 1
        self._items[item.uid] = item
        self._removed.discard(stored)
        return item

    def remove(self, target) -> None:
        """Take out a draft item (by uid), or mark a stored note (a ``Mark``)
        as removed: it stays shown, struck through, until a new copy is made."""
        if isinstance(target, int):
            if target not in self._items:
                return
            self._push()
            del self._items[target]  # a taken-out change leaves the stored note plain again
            return
        if target in self._removed:
            return
        self._push()
        for uid in [u for u, i in self._items.items() if i.replaces == target]:
            del self._items[uid]
        self._removed.add(target)

    # -- where it goes ----------

    def save_plan(
        self,
        holders: Callable[[str | None, int], list[int]],
        choices: dict[tuple[int, ...], int] | None = None,
    ) -> SavePlan:
        stored = self.stored_keys() if self.stored_keys is not None else ()
        pairs = self.stored_pairs() if self.stored_pairs is not None else None
        return make_plan(self.additions(), self.stored_changes, holders, choices, stored, pairs)


# --- routing -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Routing:
    #: "ok" | "ambiguous" | "outside"
    status: str
    recording: int | None = None
    choices: tuple[int, ...] = ()
    reason: str = ""


def route(item: DraftItem, holders: Callable[[str | None, int], list[int]]) -> Routing:
    """Which recording a new note belongs to. ``holders(channel, t)``: the
    recordings holding data (of that channel, or of any) at ``t``.

    A channel note goes to the recording holding that channel at its time. A
    whole-recording note goes to the recording holding data then; when
    several do (side by side) the caller must choose. Nothing there: refused.
    """
    found = sorted(set(holders(item.channel, item.time)))
    where = f"channel {item.channel}" if item.channel else "any channel"
    if not found:
        return Routing("outside", reason=f"No recording holds data of {where} at that time.")
    if item.is_span:  # the whole span must lie within one recording's data
        found = sorted(set(found) & set(holders(item.channel, item.end - 1)))
        if not found:
            return Routing(
                "outside",
                reason=f"No single recording holds data of {where} for the whole span.",
            )
    if item.recording is not None:
        if item.recording in found:
            return Routing("ok", item.recording)
        return Routing("outside", reason="The chosen recording holds no data at that time.")
    if len(found) == 1:
        return Routing("ok", found[0])
    return Routing("ambiguous", choices=tuple(found))


@dataclass
class RecordingPlan:
    recording: int
    #: Whole-recording notes (session level).
    session: list[DraftItem] = field(default_factory=list)
    #: Channel notes by channel.
    channels: dict[str, list[DraftItem]] = field(default_factory=dict)

    @property
    def items(self) -> list[DraftItem]:
        out = list(self.session)
        for rows in self.channels.values():
            out.extend(rows)
        return sorted(out, key=lambda i: (i.time, i.uid))

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def span(self) -> tuple[int, int]:
        items = self.items
        return min(i.time for i in items), max(i.end or i.time for i in items)


@dataclass
class SavePlan:
    by_recording: dict[int, RecordingPlan] = field(default_factory=dict)
    #: ``(item, recordings it could go to)`` waiting for a choice.
    ambiguous: list[tuple[DraftItem, tuple[int, ...]]] = field(default_factory=list)
    #: ``(item, reason)``: falls in no recording.
    refused: list[tuple[DraftItem, str]] = field(default_factory=list)
    #: Stored notes changed or removed: shown, never written (a new copy is needed).
    needs_copy: int = 0
    #: New items that are the same note as a stored one or an earlier item: left
    #: out of the write (a note is never stored twice), and they leave the draft.
    duplicates: list[DraftItem] = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum(p.count for p in self.by_recording.values())

    @property
    def can_save(self) -> bool:
        """Something to write and nothing waiting for a choice or refused."""
        return self.count > 0 and not self.ambiguous and not self.refused

    @property
    def choice_groups(self) -> list[tuple[int, ...]]:
        """The distinct sets of recordings an ambiguous note could go to."""
        return sorted({c for _i, c in self.ambiguous})


def make_plan(
    additions: Sequence[DraftItem],
    needs_copy: int,
    holders: Callable[[str | None, int], list[int]],
    choices: dict[tuple[int, ...], int] | None = None,
    stored: Collection[tuple] = (),
    stored_pairs: Collection[tuple] | None = None,
) -> SavePlan:
    """``stored``: the keys of the stored notes (judged without the recording);
    ``stored_pairs``: ``(recording, key)`` of each stored note, which, when given, replaces it:
    a note is the same as a stored or earlier one only in the recording it goes to, so two
    recordings side by side can each hold the same note."""
    plan = SavePlan(needs_copy=needs_copy)
    seen: set[tuple] = set()
    for item in additions:
        key = item_key(item)
        if stored_pairs is None and key in stored:
            plan.duplicates.append(item)
            continue
        r = route(item, holders)
        if r.status == "ambiguous" and choices and choices.get(r.choices) in r.choices:
            r = Routing("ok", choices[r.choices])
        where = r.recording if r.status == "ok" else None  # None: not decided (yet)
        if (where is not None and stored_pairs is not None and (where, key) in stored_pairs) or (
            where,
            key,
        ) in seen:
            plan.duplicates.append(item)
            continue
        seen.add((where, key))
        if r.status == "outside":
            plan.refused.append((item, r.reason))
            continue
        if r.status == "ambiguous":
            plan.ambiguous.append((item, r.choices))
            continue
        rec = plan.by_recording.setdefault(r.recording, RecordingPlan(r.recording))
        if item.channel:
            rec.channels.setdefault(item.channel, []).append(item)
        else:
            rec.session.append(item)
    return plan


# --- typing a time in the site zone ------------------------------------------------------


def format_local(us: int, zone: tzinfo) -> str:
    """ "2000-01-01 10:00:00.123": the site zone's wall clock, milliseconds."""
    moment = timefmt.to_datetime(us, zone)
    return moment.strftime("%Y-%m-%d %H:%M:%S") + f".{moment.microsecond // 1000:03d}"


def parse_local(text: str, zone: tzinfo) -> int:
    """The inverse of :func:`format_local` (the fraction is optional, up to
    microseconds). ValueError with a plain reason."""
    text = " ".join((text or "").replace("T", " ").split())
    for pattern in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            moment = datetime.strptime(text, pattern)
        except ValueError:
            continue
        us = timefmt.from_local(moment, zone)
        if us <= 0:
            break
        return us
    raise ValueError(
        "Write the time like 2000-01-31 14:30:05.250 (year-month-day hours:minutes:seconds)."
    )


# --- import ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportRow:
    number: int
    time: int
    end: int | None
    label: str
    text: str = field(repr=False, default="")
    channel: str | None = None

    def as_row(self) -> dict:
        return {
            "time": self.time,
            "end": self.end,
            "label": self.label,
            "text": self.text,
            "channel": self.channel,
        }


@dataclass
class ImportResult:
    rows: list[ImportRow] = field(default_factory=list)
    #: ``(row number, reason)``; row 1 is the first row after the header.
    invalid: list[tuple[int, str]] = field(default_factory=list)
    #: The file itself was unusable (plain words), else "".
    fatal: str = ""


def parse_time(value, *, what: str = "start") -> int:
    """A time as written in a file: an ISO time with a zone offset (``2000-01-01T10:00:00+01:00``,
    ``Z`` too) or microseconds since 1970 (UTC). Raises ValueError with a plain reason."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"The {what} time is missing.")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"The {what} time is not a whole number of microseconds.")
        us = int(value)
    else:
        text = str(value).strip()
        if not text:
            raise ValueError(f"The {what} time is missing.")
        digits = text[1:] if text[:1] == "-" else text
        if digits.isdigit():
            us = int(text)
        else:
            try:
                moment = datetime.fromisoformat(text)
            except ValueError:
                raise ValueError(f"The {what} time is not a date and time.") from None
            if moment.tzinfo is None:
                raise ValueError(
                    f"The {what} time has no time zone. Write it with an offset, "
                    "for example +01:00 or Z."
                )
            us = (moment - _EPOCH) // timedelta(microseconds=1)
    if us <= 0:
        raise ValueError(f"The {what} time is not a valid time.")
    return us


def _unescape(cell: str) -> str:
    """Undo the apostrophe that export puts before = + - @ (spreadsheets)."""
    if len(cell) > 1 and cell[0] == "'" and cell[1] in "=+-@":
        return cell[1:]
    return cell


_RECORD_NAMES = {"note", "edfa", "event", "sylg"}


def _label_of(kind: str) -> str:
    kind = " ".join((kind or "").split())
    if not kind or kind.casefold() in _RECORD_NAMES:
        return PLAIN
    return kind[:40]


def _row_from(number: int, raw: dict, unescape: bool) -> ImportRow:
    def cell(key):
        value = raw.get(key)
        return value if value not in (None, "") else None

    start_value = cell("start")
    if start_value is None:
        start_value = cell("start_us")
    start = parse_time(start_value, what="start")
    end_value = cell("end")
    if end_value is None:
        end_value = cell("end_us")
    end = parse_time(end_value, what="end") if end_value is not None else None
    if end is not None and end < start:
        raise ValueError("The end is before the start.")
    text = str(cell("text") or "")
    if unescape:  # a CSV can carry Windows line ends inside a quoted cell
        text = text.replace("\r\n", "\n")
    channel = cell("channel")
    channel = str(channel).strip() if channel is not None else None
    if unescape:
        text = _unescape(text)
    problem = control_problem(text, str(cell("type") or ""), channel)
    if problem:
        raise ValueError(problem)
    return ImportRow(
        number, start, end if end and end > start else None,
        _label_of(str(cell("type") or "")), text, channel or None,
    )  # fmt: skip


def read_rows(text: str, kind: str) -> ImportResult:
    """Rows from a CSV or JSON file's text (``kind``: "csv" | "json")."""
    result = ImportResult()
    if kind == "json":
        try:
            data = json.loads(text)
        except ValueError:
            result.fatal = "The file is not valid JSON."
            return result
        if isinstance(data, dict):
            data = data.get("notes") or data.get("items") or []
        if not isinstance(data, list):
            result.fatal = "The file does not hold a list of notes."
            return result
        raws = [(i, r) for i, r in enumerate(data, 1)]
        unescape = False
    else:
        text = text.lstrip("﻿")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            result.fatal = "The file is empty."
            return result
        names = {n.strip().casefold() for n in reader.fieldnames if n}
        if "start" not in names and "start_us" not in names:
            result.fatal = (
                "The file has no start column. The columns are: start, end (optional), "
                "type, text and channel (optional)."
            )
            return result
        raws = []
        try:
            for i, r in enumerate(reader, 1):
                raws.append(
                    (i, {(k or "").strip().casefold(): v for k, v in r.items() if k is not None})
                )
                if i > _MAX_ROWS:
                    break
        except csv.Error:
            result.fatal = "The file could not be read as a table."
            return result
        unescape = True
    if len(raws) > _MAX_ROWS:
        result.fatal = f"The file has more than {_MAX_ROWS} rows."
        return result
    for number, raw in raws:
        if not isinstance(raw, dict):
            result.invalid.append((number, "This row is not a set of values."))
            continue
        raw = {str(k).strip().casefold(): v for k, v in raw.items()}
        try:
            result.rows.append(_row_from(number, raw, unescape))
        except ValueError as exc:
            result.invalid.append((number, str(exc)))
    return result


@dataclass
class ImportPreview:
    rows: int = 0
    usable: list[ImportRow] = field(default_factory=list)
    outside: list[tuple[int, str]] = field(default_factory=list)
    invalid: list[tuple[int, str]] = field(default_factory=list)
    ambiguous: int = 0
    first: int | None = None
    last: int | None = None
    fatal: str = ""

    @property
    def can_add(self) -> bool:
        return bool(self.usable)


def preview_import(
    result: ImportResult, holders: Callable[[str | None, int], list[int]]
) -> ImportPreview:
    """Counts, the time span, rows outside every recording and invalid rows."""
    out = ImportPreview(
        rows=len(result.rows) + len(result.invalid),
        invalid=list(result.invalid),
        fatal=result.fatal,
    )
    for row in result.rows:
        probe = DraftItem(0, row.time, row.end, row.label, "", row.channel)
        r = route(probe, holders)
        if r.status == "outside":
            out.outside.append((row.number, r.reason))
            continue
        if r.status == "ambiguous":
            out.ambiguous += 1
        out.usable.append(row)
    if out.usable:
        out.first = min(r.time for r in out.usable)
        out.last = max(r.end or r.time for r in out.usable)
    return out


# --- export -------------------------------------------------------------------------------


def _iso(us: int, zone: tzinfo) -> str:
    return timefmt.to_datetime(us, zone).isoformat()


def export_rows(
    entries: Sequence[tuple[int, int | None, str, str, str | None]], zone: tzinfo
) -> list[dict]:
    """One row per ``(time, end, label, text, channel)``. Times are written
    in the site zone WITH the offset, and in microseconds since 1970 (UTC)."""
    rows = []
    for time, end, label, text, channel in entries:
        has_end = end is not None and end > time
        rows.append(
            {
                "start": _iso(time, zone),
                "end": _iso(end, zone) if has_end else "",
                "type": label,
                "text": text,
                "channel": channel or "",
                "start_us": time,
                "end_us": end if has_end else "",
            }
        )
    return rows


def to_csv(rows: Sequence[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=EXPORT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows({k: vm.safe_cell(v) for k, v in row.items()} for row in rows)
    return out.getvalue()


def to_json(rows: Sequence[dict]) -> str:
    return json.dumps(list(rows), indent=2, ensure_ascii=False) + "\n"
