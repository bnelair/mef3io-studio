"""What the explorer knows about one recording: plain, picklable data.

Filled by :mod:`.inspect` in a helper process and sent back to the app, so
everything here is a frozen dataclass of plain values. Times are integers:
microseconds since 1970, UTC, exactly as mef3io returns them.

Vocabulary (docs/ui-language.md): a *part* is what MEF calls a segment; a
*note* is a record (annotation).

Plain Python, no Qt.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .errors import UserMessage
from .text import technical

#: Recording suffixes, longest first (mef3io enforces them, case-insensitively).
SUFFIXES = (".mefd.tar", ".mefd")


def recording_name(location: str) -> str:
    """The name people know a recording by: its folder or file name without
    the ``.mefd`` / ``.mefd.tar`` suffix."""
    base = os.path.basename(os.path.normpath(str(location)))
    lowered = base.lower()
    for suffix in SUFFIXES:
        if lowered.endswith(suffix) and len(base) > len(suffix):
            return base[: -len(suffix)]
    return base


def location_key(location: str) -> str:
    """One key per recording, however its path was written (to avoid adding
    the same recording to the list twice)."""
    return os.path.normcase(os.path.abspath(os.fspath(location)))


def is_packed(location: str) -> bool:
    """True for a single-file recording (``.mefd.tar``)."""
    return os.path.normpath(str(location)).lower().endswith(".mefd.tar")


def is_recording_location(location: str) -> bool:
    lowered = os.path.normpath(str(location)).lower()
    return lowered.endswith(SUFFIXES)


#: mef3io's name for the anonymizer's operation (its history code 6).
ANONYMIZE_OPERATION = "anonymize"

#: Everyday words for what mef3io records having done to a file (the keys
#: are mef3io's operation names).
OPERATION_WORDS = {
    "create": "created",
    "append": "added to",
    technical("header-repair"): "details fixed",
    "recovery": "rescued after an interrupted write",
    technical("segment-repaired"): "an earlier part was fixed",
    "anonymize": "made by the anonymizer",
    "rename": "renamed",
    technical("transform"): "copied with changes",
}


def operation_words(name: str) -> str:
    return OPERATION_WORDS.get(name, name.replace("-", " "))


@dataclass(frozen=True)
class History:
    """What mef3io wrote into a part's files about itself: which version
    created it, which last changed it and what was done. Versions and
    operations only, never times (mef3io's provenance region)."""

    created_by: str = ""
    last_modified_by: str = ""
    last_operation: str = ""
    operations: tuple[str, ...] = ()
    modification_count: int = 0


@dataclass(frozen=True)
class Part:
    """One part of a channel (a MEF segment), from its details only."""

    number: int
    start: int
    end: int
    samples: int
    blocks: int
    #: None for files from other programs (or mef3io before 1.2).
    history: History | None = None

    @property
    def seconds(self) -> float:
        return max(0, self.end - self.start) / 1e6

    def missing_seconds(self, sampling_frequency: float) -> float:
        """Time inside the part that holds no samples (gaps inside it).

        Worked out from the part's start, end and sample count, so no signal
        and no block table is read. Below one and a half sample periods it is
        rounding, not a gap, and counts as zero."""
        if sampling_frequency <= 0:
            return 0.0
        covered = self.samples / sampling_frequency
        missing = self.seconds - covered
        return missing if missing > 1.5 / sampling_frequency else 0.0


@dataclass(frozen=True)
class ChannelInfo:
    name: str
    sampling_frequency: float
    units: str
    samples: int
    start: int
    end: int
    parts: tuple[Part, ...] = ()
    #: Bytes of every file of the channel (sizes only, never read).
    bytes: int = 0
    #: The channel's description (identifying text: masked in the UI like
    #: the other descriptions, never logged). Used by the batch's check of
    #: an existing copy, which needs it per channel.
    description: str = field(default="", repr=False, compare=False)

    @property
    def missing_seconds(self) -> float:
        return sum(p.missing_seconds(self.sampling_frequency) for p in self.parts)

    @property
    def parts_with_gaps_inside(self) -> int:
        return sum(1 for p in self.parts if p.missing_seconds(self.sampling_frequency) > 0)


@dataclass(frozen=True)
class PartProblem:
    """A part of a channel that could not be read. The rest of the
    recording is still shown. This part's times are unknown: its stretch
    shows as empty on its channel's timeline, it is never counted as time
    with data, and the recording is marked incomplete (a gap there may be
    time that was recorded)."""

    channel: str
    #: The part's number (from 0), or None when the channel has no complete
    #: part at all.
    part: int | None
    #: mef3io's reason, without the location: for "Technical details" only.
    detail: str = ""

    @property
    def where(self) -> str:
        """Which part, in everyday words."""
        if self.part is None:
            return self.channel
        return f"{self.channel}, part {self.part + 1}"


@dataclass(frozen=True)
class Note:
    """A note or event stored with the recording (a MEF record)."""

    time: int
    kind: str
    text: str = ""
    duration: int | None = None
    #: The channel it is stored with; None for the whole recording.
    channel: str | None = None
    #: "session", "channel" or "segment" (where MEF stores it).
    level: str = "channel"
    #: The part it is stored with (segment level only).
    segment: int | None = None
    #: Its text is behind the full-access password (``text`` is empty).
    locked: bool = False

    @property
    def where(self) -> str:
        """Where it is stored, in everyday words."""
        if self.level == "session" or self.channel is None:
            return "Whole recording"
        if self.level == "segment" and self.segment is not None:
            return f"{self.channel}, part {self.segment + 1}"
        return self.channel


#: Everyday names for MEF record types; anything else shows its code.
NOTE_KINDS = {
    "Note": "Note",
    "EDFA": "Note (from an EDF file)",
    "Seiz": "Seizure",
    "SyLg": "System message",
    "Epoc": "Period",
    "Curs": "Marker",
    "CSti": "Stimulation",
    "ESti": "Stimulation",
    "LNTP": "Line-noise template",
}


def note_kind_label(kind: str) -> str:
    return NOTE_KINDS.get(kind, kind or "Unknown")


@dataclass(frozen=True)
class PatientDetails:
    """Identifying text stored in the recording. Shown only after "Show"."""

    first_name: str | None = None
    last_name: str | None = None
    patient_id: str | None = None
    location: str | None = None
    recording_description: str | None = None
    #: False when the full-access password is needed to read the names.
    names_available: bool = True

    def values(self) -> list[str]:
        """Every non-empty value (to keep them out of the log)."""
        out = [
            self.first_name,
            self.last_name,
            self.patient_id,
            self.location,
            self.recording_description,
        ]
        return [v for v in out if v]


#: How far a password opened a recording.
ACCESS_OPEN = "open"  # not password-protected
ACCESS_BASIC = "basic"  # the basic (level-1) password: no patient details
ACCESS_FULL = "full"  # the full-access (level-2) password


@dataclass(frozen=True)
class IdentityFact:
    """Whether one identifying field of the recording holds anything
    (from mef3io's inspect). ``present`` is None when it could not be read
    (behind the full-access password, or in a part that could not be read).
    ``values`` are the texts themselves: masked in the UI until "Show"."""

    name: str
    present: bool | None
    values: tuple[str, ...] = ()
    #: In how many places (files, or notes for note text) it is set.
    occurrences: int = 0


#: What mef3io found about how the times are stored (its time_offset status).
OFFSET_NONE = "none"  # the common case: times as stored
OFFSET_APPLIED = "applied"  # meflib's deprecated date masking is in use
OFFSET_DISPUTED = "disputed"  # parts of the recording disagree
OFFSET_LOCKED = "locked"  # behind the full-access password: not known


@dataclass(frozen=True)
class RecordingDetails:
    location: str
    channels: tuple[ChannelInfo, ...]
    patient: PatientDetails = field(default_factory=PatientDetails)
    access: str = ACCESS_OPEN
    #: meflib's recording-time offset; None when it cannot be read (it is
    #: stored behind the full-access password).
    recording_time_offset: int | None = 0
    #: One of the ``OFFSET_*`` values above.
    time_offset_status: str = OFFSET_NONE
    #: A recording-time offset is set in at least one part, so the real date
    #: is stored in the file (meflib's deprecated date masking): true even
    #: when the parts disagree and no single offset can be given.
    date_recoverable: bool = False
    #: mef3io could not confirm how the times are stored (the full-access
    #: password is missing) and read them assuming the common case. They are
    #: right for almost every file, but not verified.
    offset_assumed_zero: bool = False
    #: The file's fixed offset from UTC in seconds (raw, technical details
    #: only; never used to show times). None when it cannot be read.
    gmt_offset: int | None = None
    notes: tuple[Note, ...] = ()
    #: Which levels of notes were read (see :mod:`.inspect`).
    note_levels: tuple[str, ...] = ("session", "channel", "segment")
    #: Why the notes could not be read (plain words), or None.
    notes_problem: UserMessage | None = None
    #: Notes whose time is unknown because a part they belong to could not
    #: be read. They are not listed.
    notes_without_times: int = 0
    declarations_incomplete: bool = False
    #: Parts that could not be read (the rest is shown without them).
    part_problems: tuple[PartProblem, ...] = ()
    #: Free text the recording system stored about the channels. It can hold
    #: anything, so it is treated as a patient detail (masked).
    channel_descriptions: tuple[str, ...] = ()
    #: The password opened the recording only through the rule mef3io used
    #: up to 1.2.0 for accented and other non-ASCII characters; other
    #: programs will not accept it for this recording.
    legacy_password: bool = False
    #: Every other identifying value the recording holds (stored names,
    #: descriptions), for the log filter only. Never shown.
    identity_extra: tuple[str, ...] = ()
    #: A stored time is at or before the recording-time offset, so it cannot
    #: be told apart from an absolute time (mef3io's "ambiguous").
    time_offset_ambiguous: bool = False
    #: The span a time shift must cover: samples AND notes (None if unknown).
    first_time: int | None = None
    last_time: int | None = None
    #: What mef3io would refuse to anonymize, as ``(code, technical text)``.
    anonymize_blockers: tuple[tuple[str, str], ...] = ()
    #: Every identifying field mef3io knows, with presence and values (the
    #: privacy check). Empty for details built before this was read.
    identity: tuple[IdentityFact, ...] = ()
    #: The daylight-saving start and end are set (a location hint); None
    #: when they could not be read.
    dst_set: bool | None = False

    @property
    def name(self) -> str:
        return recording_name(self.location)

    @property
    def password_protected(self) -> bool:
        return self.access != ACCESS_OPEN

    @property
    def start(self) -> int | None:
        starts = [c.start for c in self.channels]
        return min(starts) if starts else None

    @property
    def end(self) -> int | None:
        ends = [c.end for c in self.channels]
        return max(ends) if ends else None

    @property
    def duration_seconds(self) -> float | None:
        if self.start is None or self.end is None:
            return None
        return max(0, self.end - self.start) / 1e6

    @property
    def sampling_rates(self) -> tuple[float, ...]:
        return tuple(sorted({c.sampling_frequency for c in self.channels}))

    @property
    def part_count(self) -> int:
        """The most parts any channel has (channels are normally split alike)."""
        return max((len(c.parts) for c in self.channels), default=0)

    @property
    def times_verified(self) -> bool:
        """False when the times may not be the real ones
        (docs/design/explorer.md, "Times you can't trust"): the
        recording-time offset is unknown (it is behind the full-access
        password, so mef3io assumed 0: ``offset_assumed_zero``), the parts
        disagree about it, or it is not zero, where older tools stored times
        with the opposite sign convention to meflib and the bytes cannot tell
        which. The times are still shown as mef3io returns them, labelled."""
        return self.recording_time_offset == 0 and not self.offset_assumed_zero

    @property
    def notes_locked(self) -> int:
        """Notes whose text needs the full-access password."""
        return sum(1 for n in self.notes if n.locked)

    @property
    def uses_time_offset(self) -> bool:
        """meflib's deprecated date masking is in use: the real date can be
        worked out again, so the recording stays identifiable. Also when the
        parts disagree about the offset (no single value is known)."""
        return bool(self.recording_time_offset) or self.date_recoverable

    def gaps(self) -> list[tuple[int, int]]:
        """Stretches where no channel has data, from the parts' start and end
        times only. A stretch shorter than 1.5 sample periods is rounding."""
        spans = sorted((p.start, p.end) for c in self.channels for p in c.parts)
        if not spans:
            return []
        rates = [c.sampling_frequency for c in self.channels if c.sampling_frequency > 0]
        tolerance = int(1.5e6 / min(rates)) if rates else 0
        gaps = []
        cur_end = spans[0][1]
        for start, end in spans[1:]:
            if start > cur_end + tolerance:
                gaps.append((cur_end, start))
                cur_end = end
            else:
                cur_end = max(cur_end, end)
        return gaps

    @property
    def missing_inside_seconds(self) -> float:
        """The longest per-channel total of time missing inside parts."""
        return max((c.missing_seconds for c in self.channels), default=0.0)

    @property
    def parts_with_gaps_inside(self) -> int:
        return max((c.parts_with_gaps_inside for c in self.channels), default=0)

    def history_groups(self) -> list[tuple[History | None, int]]:
        """Parts with the same history, grouped: ``(history, parts)``,
        most common first. ``None`` = written by another program (no
        history recorded)."""
        counts: dict[History | None, int] = {}
        for channel in self.channels:
            for part in channel.parts:
                counts[part.history] = counts.get(part.history, 0) + 1
        return sorted(counts.items(), key=lambda item: (item[0] is None, -item[1]))

    def identity_fact(self, name: str) -> IdentityFact | None:
        for fact in self.identity:
            if fact.name == name:
                return fact
        return None

    @property
    def made_by_anonymizer(self) -> bool:
        """Every part's files were last written by mef3io's anonymizer (their
        history's LAST operation is "anonymize", code 6), and no part was left
        unread. The history's set of operations ever done is not enough: a
        copy inherits its source's, and data appended to a copy afterwards
        (with real dates, possibly) leaves "anonymize" in it."""
        parts = [p for c in self.channels for p in c.parts]
        if not parts or self.part_problems:
            return False
        return all(
            p.history is not None and p.history.last_operation == ANONYMIZE_OPERATION for p in parts
        )

    @property
    def changed_after_anonymizing(self) -> bool:
        """Some part was anonymized and then changed again (for example,
        data was added), or new parts were added next to anonymized ones: its
        dates can no longer be confirmed as moved."""
        parts = [p for c in self.channels for p in c.parts]
        stamped = [
            p
            for p in parts
            if p.history is not None and ANONYMIZE_OPERATION in p.history.operations
        ]
        return bool(stamped) and not self.made_by_anonymizer

    def identity_values(self) -> list[str]:
        """Every identifying string this recording holds, for the log filter."""
        values = [self.name, *self.patient.values()]
        values += [*self.channel_descriptions, *self.identity_extra]
        # Note text is NOT registered: it is masked in the UI and never
        # logged, and a recording can hold thousands of notes, which would
        # make the log filter's pattern huge (and short texts like "ok"
        # would redact inside unrelated words).
        first, last = self.patient.first_name, self.patient.last_name
        if first and last:
            # The log filter matches "First Last"; register the other order too.
            values += [f"{first} {last}", f"{last} {first}", f"{last}, {first}"]
        return values


@dataclass(frozen=True)
class InspectOutcome:
    """The answer for one recording: details, or why there are none."""

    location: str
    size_bytes: int | None = None
    details: RecordingDetails | None = None
    #: Password-protected, and no password (or a wrong one) was given.
    needs_password: bool = False
    wrong_password: bool = False
    error: UserMessage | None = None

    @property
    def name(self) -> str:
        return recording_name(self.location)

    @property
    def packed(self) -> bool:
        return is_packed(self.location)
