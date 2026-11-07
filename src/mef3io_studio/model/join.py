"""Join or trim recordings: keep a chosen part of each, make ONE new recording.

The user picks one or more recordings and, for each, what to keep: all of
it, the first or the last so many hours/minutes/seconds, or everything
between two times. The result is one new recording; the originals are never
changed. All the work on the files is mef3io's (2.2):

* ``mef3io.time_range`` (a few small files per channel) turns "the first
  hour" into a window and orders the recordings by time;
* SEVERAL recordings are made into one by ONE call, straight from the
  originals: ``mef3io.merge_sessions(..., time_ranges=, exact=True,
  exclude_channels=, anonymize=, records=, output_password=)``. The kept part
  of each, the channels left out, the anonymizing (one shift and one
  pseudonym for all), leaving out the notes and events and the new password
  protection all happen in that one pass over the data: no temporary copy of
  any part is ever written (so none can hold the patient's details);
* ONE recording (a merge needs two) is trimmed straight into the
  destination with ``transform_session(time_range=, exact=True, records=)``,
  or, when it is made anonymous, ``anonymize_session`` with the same options.

Each of them builds under ``<dst>.partial/``, checks what it wrote against
its sources and only then gives it its final name; a refusal, a failure or
a Stop leaves nothing behind.

Only "Save as one file" still uses a hidden folder, :data:`PARTS_PREFIX` +
random hex (opaque: no names; the dot hides it; folder scans skip the app's
``.mef3io-studio-`` names), next to the destination: the recording is built
there, packed with ``archive_session``, the pack is compared with it and only
then given its final name (never replacing anything). The folder is ALWAYS
removed afterwards: success, refusal, failure or Stop (in the helper's
``finally``, and again by the task when a helper had to be ended:
:func:`tidy`).

A channel left out is left out of the result as a whole: ``merge_sessions``
takes the list over all recordings, so a recording that does not hold one of
the channels is fine (as long as some recording does), and the app no longer
works out which recording holds which.

Overlaps are refused PER CHANNEL, not worked around: two kept parts
conflict only when they overlap in time AND share a channel that is not
left out. Recordings of the same period with different channels ("side by
side") join, and the result holds the union of their channels, as
``merge_sessions`` does; leaving out the shared channels makes overlapping
parts joinable. The kept part's time window is the recording's (not each
channel's own extent), so this is stricter than ``merge_sessions``, which
decides on the recorded blocks and also allows recordings that take turns
on one channel: not what this feature is for. ``merge_sessions`` still
checks the recorded signal itself (``merge-overlap``) as the last word.

What is already known is not read again: the page hands the request what
Explore knows of each recording (``JoinRequest.layouts``: start, end,
channels, size on disk), and then neither the check nor the run opens a
recording or walks its folder (that cost tens of thousands of file-system
calls on big recordings, minutes on a network drive). Only a recording
Explore has no answer for is opened (:func:`read_layouts`). A recording's
end is then the end its headers state (Explore's): for mef3io's own files
that is where the last sample ends; for another writer's it may be the last
sample's own time (one sample period earlier). That cannot lose a sample: a
window that reaches the recording's end is handed to mef3io one microsecond
past it (``Span.to_end``), so the last sample is kept either way. The page
re-checks instead of running when a recording was touched since Explore
read it (a few ``stat``s each, none listed or opened) (:func:`facts_still_true`).

One recording kept whole with no channel left out and no new passwords
would be an exact copy: refused as "nothing would change"
(:data:`NOTHING_TO_CHANGE`). Kept whole WITH a change (channels or
passwords), it is a copy with those changes, by ``transform_session``.

NOTES AND EVENTS (``JoinRequest.records``): ``"keep"`` (default) carries
them over (merged in time order); ``"drop"`` leaves ALL of them out, at
every level, in any kind of join (anonymous or not): the result has no note
files at all, and mef3io's check expects none. With an anonymous copy that
keeps them, :class:`Anonymous` ``.text`` still says whether their text is
removed or kept.

The whole pipeline is ONE function, :func:`run`, which the page never looks
inside; the page only sees :class:`JoinRequest`, :func:`check_join`,
:func:`make_joined` and :class:`JoinProgress`.

Free space is checked before anything is written (:func:`sizing`): the
worst moment of the chosen pipeline must fit on the drive of the result.

Privacy: paths, names, passwords and times are ``repr=False`` and never
logged. Plain Python, no Qt.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field

from . import archive, pipeline
from .anonymize import (
    CLEAR_ALL,
    TEXT_DROP,
    _RefusalText,
    copy_name,
    is_unreachable,
    remove_unfinished,
    technical_text,
)
from .cancel import CancelToken, JobCancelled, is_stop, stopped_by_library
from .changes import (
    PLACEHOLDER_CODES,
    ChangeRequest,
    describe_transform_error,
    refusal_message,
    transform_copy,
    transform_refusals,
)
from .errors import UserMessage
from .progress import byte_progress
from .quiet import library_warnings_caught
from .recording import recording_name
from .refusals import NOTHING_JOINED, join_message_for, message_for
from .times import read_time

SECOND_US = 1_000_000

#: What to keep of one recording.
KEEP_WHOLE = "whole"
KEEP_FIRST = "first"
KEEP_LAST = "last"
KEEP_BETWEEN = "between"
KEEP_CHOICES = (KEEP_WHOLE, KEEP_FIRST, KEEP_LAST, KEEP_BETWEEN)

#: The temporary parts folder's name starts with this (then random hex).
PARTS_PREFIX = ".mef3io-studio-parts-"
#: Notes and events: carried over, or all left out (mef3io's ``records=``).
RECORDS_KEEP = "keep"
RECORDS_DROP = "drop"
#: Written into the hidden folder right after this run creates it, holding
#: the run's token: only a folder carrying it is ever removed, so a folder
#: that was already there (whatever it holds) is never touched.
PARTS_MARKER = ".made-by-mef3io-studio"

#: Why a choice cannot be used (codes; words in :data:`PROBLEM_WORDS`).
DURATION_NOT_POSITIVE = "duration-not-positive"
TIMES_MISSING = "times-missing"
END_NOT_AFTER_START = "end-not-after-start"
OUTSIDE_RECORDING = "outside-recording"
TIMES_UNKNOWN = "times-unknown"
OVERLAP = "overlap"
NOTHING_TO_CHANGE = "nothing-to-change"
NO_RECORDINGS = "no-recordings"
NO_CHANNELS_LEFT = "no-channels-left"
UNKNOWN_CHOICE = "unknown-choice"

PROBLEM_WORDS = {
    DURATION_NOT_POSITIVE: "Choose how much to keep (more than zero).",
    TIMES_MISSING: "Choose both times.",
    END_NOT_AFTER_START: "The end must be after the start.",
    OUTSIDE_RECORDING: "The chosen times are outside this recording.",
    TIMES_UNKNOWN: (
        "Its start and end could not be worked out. Open it in Explore with its full-access "
        "password, or use Check for problems."
    ),
    OVERLAP: (
        "The kept parts of {names} overlap in time. Keep less of one of them, so that the "
        "parts follow each other."
    ),
    #: An overlap of parts that share channels: the words name them.
    "overlap-channels": (
        "The kept parts of {names} overlap in time, and both hold {channels}. Keep less of "
        "one of them, or leave out {those} in the channel step."
    ),
    NO_CHANNELS_LEFT: "Every channel of this recording is left out.",
    NOTHING_TO_CHANGE: (
        "Nothing would change: the new recording would be an exact copy. Keep only part of "
        "the recording, leave out a channel, or add password protection."
    ),
    NO_RECORDINGS: "Choose at least one recording.",
    UNKNOWN_CHOICE: "Choose what to keep.",
}


# --------------------------------------------------------------------------
# What to keep, and where that is in time


@dataclass(frozen=True)
class Keep:
    """What to keep of one recording. ``duration_us`` for first/last;
    ``start_us``/``end_us`` (microseconds since 1970, UTC) for between."""

    how: str = KEEP_WHOLE
    duration_us: int = 0
    start_us: int | None = field(default=None, repr=False)
    end_us: int | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Span:
    """The kept part of one recording: ``[start, end)``, microseconds.
    ``to_end``: it runs to the recording's own end (the window handed to
    mef3io then reaches one microsecond past it, so the last sample is kept
    whichever way a writer stored the end time)."""

    start: int = field(repr=False)
    end: int = field(repr=False)
    whole: bool = False
    to_end: bool = False

    @property
    def time_range(self) -> tuple[int, int]:
        return (self.start, self.end + 1 if self.to_end else self.end)

    @property
    def seconds(self) -> float:
        return max(0, self.end - self.start) / SECOND_US


def resolve_keep(keep: Keep, start: int | None, end: int | None) -> Span | str:
    """The part of a recording running ``[start, end)`` that ``keep``
    keeps, or a problem code. A "first"/"last" longer than the recording, or
    a "between" covering all of it, keeps it whole; a "between" reaching
    past either end is cut back to the recording."""
    if start is None or end is None or end <= start:
        return TIMES_UNKNOWN
    if keep.how == KEEP_WHOLE:
        return Span(start, end, whole=True, to_end=True)
    if keep.how in (KEEP_FIRST, KEEP_LAST):
        d = int(keep.duration_us)
        if d <= 0:
            return DURATION_NOT_POSITIVE
        if d >= end - start:
            return Span(start, end, whole=True, to_end=True)
        if keep.how == KEEP_FIRST:
            return Span(start, start + d)
        return Span(end - d, end, to_end=True)
    if keep.how == KEEP_BETWEEN:
        if keep.start_us is None or keep.end_us is None:
            return TIMES_MISSING
        if keep.end_us <= keep.start_us:
            return END_NOT_AFTER_START
        s, e = max(int(keep.start_us), start), min(int(keep.end_us), end)
        if e <= s:
            return OUTSIDE_RECORDING
        if s == start and e == end:
            return Span(start, end, whole=True, to_end=True)
        return Span(s, e, to_end=e == end)
    return UNKNOWN_CHOICE


# --------------------------------------------------------------------------
# One window for every recording ("set on the map")


def _has_data_in(runs, start: int, end: int) -> bool:
    """Whether any stretch ``[a, b]`` of ``runs`` meets ``[start, end)``
    (a stretch that is a single moment counts when that moment is inside)."""
    return any((a < end and b > start) or (a == b and start <= a < end) for a, b in runs)


def window_keeps(
    layouts: dict,
    start: int,
    end: int,
    runs: dict | None = None,
) -> tuple[dict[str, Keep], tuple[str, ...]]:
    """One window ``[start, end)`` (microseconds since 1970, UTC) applied to
    every recording in ``layouts`` (key -> :class:`Layout`): each keeps only
    its data inside it. Returns ``(keep per recording that has data inside
    the window, keys left out)``. A recording is left out when its own
    extent does not meet the window, or, when ``runs`` (key -> its
    stretches with data) is given, when none of its stretches does: the
    window then falls in a gap of that recording. A recording whose times
    are not known is kept in (its own problem is then reported by
    :func:`plan_join`, as for any other choice). Pure."""
    keeps: dict[str, Keep] = {}
    left_out: list[str] = []
    for key, value in layouts.items():
        layout = _as_layout(value)
        if layout.start is not None and layout.end is not None and end > start:
            meets = max(start, layout.start) < min(end, layout.end)
            if meets and runs is not None and key in runs:
                meets = _has_data_in(runs[key], start, end)
            if not meets:
                left_out.append(key)
                continue
        keeps[key] = Keep(KEEP_BETWEEN, start_us=int(start), end_us=int(end))
    return keeps, tuple(left_out)


@dataclass(frozen=True)
class JoinSource:
    """One recording and what to keep of it. ``password``: its full-access
    password, when it has one (the one Explore opened it with)."""

    key: str = field(repr=False)
    source: str = field(repr=False)
    keep: Keep = Keep()
    password: str = field(default="", repr=False)

    @property
    def name(self) -> str:
        return recording_name(self.source)


@dataclass(frozen=True)
class Problem:
    code: str
    #: The recordings it concerns (their keys: paths, so not in repr).
    keys: tuple[str, ...] = field(default=(), repr=False)
    #: For an overlap: the channels both parts hold (empty when not known).
    channels: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class Layout:
    """What the plan needs of one recording: its start and end (the end is
    exclusive: where the last sample ends) and its channels (empty = not
    known, and then any overlap in time counts as a conflict)."""

    start: int | None = field(default=None, repr=False)
    end: int | None = field(default=None, repr=False)
    channels: tuple[str, ...] = field(default=(), repr=False)
    #: Bytes the whole recording takes on disk, when Explore already measured
    #: them (None = measure the folder).
    total_bytes: int | None = field(default=None, repr=False)
    #: When Explore read it (its modification time): see :func:`facts_still_true`.
    stamp: tuple | None = field(default=None, repr=False)


def _as_layout(value) -> Layout:
    if isinstance(value, Layout):
        return value
    start, end, *rest = value
    return Layout(start, end, tuple(rest[0]) if rest else ())


@dataclass(frozen=True)
class JoinPlan:
    """The kept parts, in time order, or why they cannot be used."""

    order: tuple[str, ...] = field(default=(), repr=False)
    spans: dict = field(default_factory=dict, repr=False)
    problems: tuple[Problem, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def overlapping(self) -> frozenset[str]:
        return frozenset(k for p in self.problems if p.code == OVERLAP for k in p.keys)

    def problem_of(self, key: str) -> Problem | None:
        return next((p for p in self.problems if p.code != OVERLAP and key in p.keys), None)


def plan_join(
    sources: list[JoinSource] | tuple[JoinSource, ...],
    layouts: dict,
    *,
    excluded: tuple[str, ...] | frozenset[str] = (),
    changes: bool = True,
) -> JoinPlan:
    """Resolve every recording's kept part and order them by time.
    ``layouts``: key -> :class:`Layout` (or ``(start, end)`` /
    ``(start, end, channels)``). ``excluded``: the channels left out of the
    result. Two kept parts conflict only when they overlap in time AND
    share a kept channel (:data:`OVERLAP`, naming the channels). ``changes``:
    a channel is left out or new passwords are set (a single recording kept
    whole is refused without one). Pure: no files are touched."""
    if not sources:
        return JoinPlan(problems=(Problem(NO_RECORDINGS),))
    left_out = frozenset(excluded)
    problems: list[Problem] = []
    spans: dict[str, Span] = {}
    kept: dict[str, frozenset[str] | None] = {}
    for s in sources:
        layout = _as_layout(layouts.get(s.key, (None, None)))
        found = resolve_keep(s.keep, layout.start, layout.end)
        if isinstance(found, str):
            problems.append(Problem(found, (s.key,)))
            continue
        spans[s.key] = found
        if layout.channels:
            kept[s.key] = frozenset(layout.channels) - left_out
            if not kept[s.key]:
                problems.append(Problem(NO_CHANNELS_LEFT, (s.key,)))
        else:
            kept[s.key] = None  # not known: any overlap in time conflicts
    order = tuple(sorted(spans, key=lambda k: (spans[k].start, spans[k].end, k)))
    # Any two kept parts that overlap in time, also across a third one in
    # between; a conflict only when they share a kept channel.
    for i, a in enumerate(order):
        for b in order[i + 1 :]:
            if spans[b].start >= spans[a].end:
                break
            if kept[a] is None or kept[b] is None:
                problems.append(Problem(OVERLAP, (a, b)))
                continue
            shared = kept[a] & kept[b]
            if shared:
                problems.append(Problem(OVERLAP, (a, b), tuple(sorted(shared))))
    if len(sources) == 1 and not problems and spans[order[0]].whole and not changes:
        problems.append(Problem(NOTHING_TO_CHANGE, (order[0],)))
    return JoinPlan(order, spans, tuple(problems))


def problem_text(problem: Problem, names: dict[str, str]) -> str:
    """A problem in plain words (``names``: key -> recording name)."""
    text = PROBLEM_WORDS.get(problem.code, PROBLEM_WORDS[UNKNOWN_CHOICE])
    if problem.code == OVERLAP:
        who = " and ".join(names.get(k, "") for k in problem.keys)
        if not problem.channels:
            return text.format(names=who)
        channels = list(problem.channels)
        shown = ", ".join(channels[:5])
        if len(channels) > 5:
            shown += f" and {len(channels) - 5} more"
        one = len(channels) == 1
        return PROBLEM_WORDS["overlap-channels"].format(
            names=who,
            channels=f"the channel {shown}" if one else f"the channels {shown}",
            those="that channel" if one else "those channels",
        )
    return text


# --------------------------------------------------------------------------
# One run


@dataclass(frozen=True)
class Anonymous:
    """Make the new recording anonymous (the Anonymize page's choices for
    one copy). Whether the notes and events are kept at all is
    ``JoinRequest.records``, for any join."""

    #: Added to every time (whole days, from ``shiftplan``); None = the dates
    #: are kept as they are.
    shift_us: int | None = field(default=None, repr=False)
    #: Stored as the patient's code; empty = the new recording's name.
    pseudonym: str = field(default="", repr=False)
    clear: tuple[str, ...] = CLEAR_ALL
    #: What happens to the text of notes and events (``TEXT_DROP``/``TEXT_KEEP``).
    text: str = TEXT_DROP


@dataclass(frozen=True)
class JoinRequest:
    """Everything the helper process needs. Plain data; not one field may
    be logged (paths and passwords)."""

    sources: tuple[JoinSource, ...]
    #: The new recording: a folder ending in ``.mefd``, or (``one_file``) a
    #: file ending in ``.mefd.tar``.
    destination: str = field(repr=False)
    exclude_channels: tuple[str, ...] = field(default=(), repr=False)
    #: ``(basic, full-access)`` for the new recording, or None.
    output_password: tuple[str, str] | None = field(default=None, repr=False)
    #: The temporary parts folder (:func:`parts_folder_for`).
    parts_folder: str = field(default="", repr=False)
    #: Proves a parts folder is this run's (:data:`PARTS_MARKER`).
    run_token: str = field(default="", repr=False)
    #: Anonymous copy (see :class:`Anonymous`), or None = patient details kept.
    anonymous: Anonymous | None = None
    #: Save as ONE file (``.mefd.tar``) instead of a folder.
    one_file: bool = False
    #: ``RECORDS_KEEP`` or ``RECORDS_DROP`` (leave out all notes and events).
    records: str = RECORDS_KEEP
    #: What Explore already knows of each recording (key -> :class:`Layout`:
    #: start, end, channels, size). A recording listed here is NOT opened
    #: again by the check or the run; one that is not listed is opened.
    layouts: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def name(self) -> str:
        """The new recording's name (without ``.mefd`` / ``.mefd.tar``)."""
        base = os.path.basename(os.path.normpath(self.destination))
        if base.lower().endswith(".mefd.tar"):
            return base[: -len(".mefd.tar")]
        return copy_name(self.destination)

    @property
    def changes(self) -> bool:
        return (
            bool(self.exclude_channels)
            or self.output_password is not None
            or self.anonymous is not None
            or self.one_file
            or self.records == RECORDS_DROP
        )

    @property
    def single(self) -> bool:
        return len(self.sources) == 1

    @property
    def built_path(self) -> str:
        """Where the recording (a folder) is made: the destination itself,
        or, for one file, inside the hidden folder (then packed)."""
        if self.one_file:
            return os.path.join(self.parts_folder, self.name + ".mefd")
        return self.destination

    @property
    def packed_path(self) -> str:
        return os.path.join(self.parts_folder, "packed.mefd.tar")


def parts_folder_for(destination: str) -> str:
    """A new, opaque folder name next to ``destination`` for the temporary
    parts (nothing is created here)."""
    folder = os.path.dirname(os.path.abspath(destination))
    return os.path.join(folder, PARTS_PREFIX + secrets.token_hex(8))


def new_request(
    sources: list[JoinSource],
    destination: str,
    exclude_channels: tuple[str, ...] = (),
    output_password: tuple[str, str] | None = None,
    anonymous: Anonymous | None = None,
    one_file: bool = False,
    records: str = RECORDS_KEEP,
    layouts: dict | None = None,
) -> JoinRequest:
    return JoinRequest(
        tuple(sources),
        destination,
        tuple(exclude_channels),
        output_password,
        parts_folder_for(destination),
        secrets.token_hex(16),
        anonymous,
        one_file,
        records,
        dict(layouts or {}),
    )


@dataclass(frozen=True)
class JoinProgress:
    """How far one stage has got (plain data, sent from the helper).
    ``kind``: "trim" (one recording straight into the new recording), "join"
    (several, in one pass: the kept part of each, anonymous at the same time
    when asked), "pack" or "check" (the one file). ``step``/``steps``/``weight`` are stage sizes
    (about the bytes each stage handles), so the bar moves evenly."""

    kind: str
    index: int
    count: int
    step: float
    steps: float
    fraction: float
    weight: float = 1.0
    anonymous: bool = False

    @property
    def overall(self) -> float:
        if self.steps <= 0:
            return 0.0
        return min(1.0, (self.step + max(0.0, min(1.0, self.fraction)) * self.weight) / self.steps)


@dataclass(frozen=True)
class JoinResult:
    """How the run went. ``message`` is None when it was saved and checked."""

    destination: str = field(repr=False)
    ok: bool = False
    message: UserMessage | None = None
    channels_kept: tuple[str, ...] = field(default=(), repr=False)
    channels_excluded: tuple[str, ...] = field(default=(), repr=False)
    bytes_written: int = 0
    notes: tuple[str, ...] = ()
    retryable: bool = False
    #: The temporary parts folder could not be removed (the user is told).
    parts_left: bool = False
    #: What was made (for the result screen).
    anonymous: bool = False
    one_file: bool = False
    #: All notes and events were left out (``records="drop"``), and how many
    #: there were in what was kept (mef3io's ``records_dropped``).
    notes_left_out: bool = False
    records_dropped: int = 0


@dataclass(frozen=True)
class CheckFinding:
    """One reason not to start, for one recording (``key``) or for the
    whole run (``key`` None)."""

    key: str | None = field(repr=False)
    message: UserMessage = None


def _paths_and_stems(request: JoinRequest) -> tuple[tuple[str, ...], tuple[str, ...]]:
    paths: list[str] = [request.destination, os.path.dirname(request.destination)]
    if request.parts_folder:
        paths.append(request.parts_folder)
    stems = [request.name]
    for s in request.sources:
        paths += [s.source, os.path.dirname(s.source)]
        stems.append(s.name)
    return tuple(paths), tuple(stems)


def join_technical_text(exc: BaseException, request: JoinRequest) -> str:
    """mef3io's wording for "Technical details", without any of the run's
    paths or recording names (see ``anonymize.technical_text``)."""
    paths, stems = _paths_and_stems(request)
    return technical_text(exc, paths=paths, stems=stems)


def describe_join_error(exc: BaseException, request: JoinRequest) -> UserMessage:
    """A refusal or failure in plain words (``changes.describe_transform_
    error``: ``JOIN_WORDS`` has every code of transform and merge)."""
    return describe_transform_error(exc, *_paths_and_stems(request))


def _check_message(refusal, request: JoinRequest) -> UserMessage:
    return refusal_message(refusal, *_paths_and_stems(request))


def _last_sample_end(reader, channel: str, stated_end: int) -> int | None:
    """Where the channel's last sample ends, from its last block in the
    index. Usually only the last part's block index is read (blocks from
    just before the stated end on). If a writer stated an end LATER than its
    data, no block is found there: then the channel's whole index is read
    (still no samples) and its true last block end is used."""
    for t0 in (max(0, int(stated_end) - 1), None):
        rows = reader.block_envelope(channel, t0=t0)
        ends = rows.get("end_uutc")
        if ends is not None and len(ends):
            return int(max(ends))
    return None


def read_layouts(
    request: JoinRequest, cancel: CancelToken
) -> tuple[dict[str, Layout], list[CheckFinding]]:
    """Each recording's start, end and channels. The start and whether the
    times are confirmed come from ``mef3io.time_range`` (a few small
    files); the channels from a ``Reader``; the END from the last block of
    each channel (``block_envelope``: where the last sample ends, whatever
    a writer stated in its headers; see the module notes). A recording
    whose times are not confirmed (protected, full-access password not
    given) is reported, not planned. A recording in ``request.layouts``
    (what Explore already knows) is taken as it is and not opened."""
    import mef3io

    layouts: dict[str, Layout] = {}
    findings: list[CheckFinding] = []
    for s in request.sources:
        cancel.raise_if_cancelled()
        known = request.layouts.get(s.key)
        if known is not None:
            # Explore already read it: nothing is opened again.
            layouts[s.key] = _as_layout(known)
            continue
        try:
            found = read_time(s.source, s.password, cancel)
            if not found.verified:
                # Password-protected, and the full-access password was not
                # given: its times cannot be confirmed (nor the part cut).
                findings.append(CheckFinding(s.key, join_message_for("password-required", {})))
                continue
            reader = mef3io.Reader(s.source, password=s.password, warn_declarations=False)
            channels = tuple(reader.channels)
            ends = [_last_sample_end(reader, c, found.end) for c in channels]
            ends = [e for e in ends if e is not None]
            end = max(ends) if ends else int(found.end)
            del reader
        except Exception as exc:
            if is_stop(exc):
                raise
            findings.append(CheckFinding(s.key, describe_join_error(exc, request)))
            continue
        layouts[s.key] = Layout(int(found.start), end, channels)
    return layouts, findings


def facts_still_true(request: JoinRequest) -> bool:
    """The recordings look as they did when Explore read them: ``stat`` of
    the few paths in ``Layout.stamp`` (the recording, each channel's folder,
    the largest file of every folder), each once, nothing listed or opened.
    That notices data appended to a recording (the data file grows) and a
    new part in a channel. mef3io's check reports no times, so this is the
    signal. Residual gap: a change that alters none of those (edited in
    place without changing its size) is not seen; mef3io's own check inside
    the copy stays the safety. A recording without a stamp, or that cannot
    be read, counts as changed."""
    for s in request.sources:
        known = request.layouts.get(s.key)
        stamp = getattr(known, "stamp", None)
        if not stamp:
            return False
        try:
            for path, mtime, size in stamp:
                st = os.stat(path)
                if st.st_mtime_ns != mtime or (size and st.st_size != size):
                    return False
        except OSError:
            return False
    return True


def without_facts(request: JoinRequest) -> JoinRequest:
    """The same request, but every recording is opened afresh."""
    import dataclasses

    return dataclasses.replace(request, layouts={})


def _plan_findings(plan: JoinPlan, request: JoinRequest) -> list[CheckFinding]:
    names = {s.key: s.name for s in request.sources}
    out = []
    for p in plan.problems:
        key = p.keys[0] if len(p.keys) == 1 else None
        out.append(
            CheckFinding(
                key,
                UserMessage(
                    "The parts to keep cannot be used",
                    f"{problem_text(p, names)} {NOTHING_JOINED}",
                ),
            )
        )
    return out


def plan_for(request: JoinRequest, layouts: dict) -> JoinPlan:
    return plan_join(
        request.sources,
        layouts,
        excluded=request.exclude_channels,
        changes=request.changes,
    )


def _by_key(request: JoinRequest) -> dict[str, JoinSource]:
    return {s.key: s for s in request.sources}


_PLACEHOLDER_DIR = os.path.join(tempfile.gettempdir(), ".mef3io-studio-not-a-folder")


def _placeholder(request: JoinRequest) -> str:
    """A path for the new recording that does not exist (checks write
    nothing; refusals about the place are left out by the caller)."""
    return os.path.join(_PLACEHOLDER_DIR, request.name + ".mefd")


def _anonymize_kwargs(request: JoinRequest, span: Span | None) -> dict:
    """``anonymize_session``'s options for ONE recording (the single path)."""
    anon = request.anonymous
    return dict(
        pseudonym=anon.pseudonym or request.name,
        clear=tuple(anon.clear),
        text=anon.text,
        exclude_channels=list(request.exclude_channels) or None,
        time_range=None if span is None or span.whole else span.time_range,
        exact=True,
        records=request.records,
    )


def _anonymize_spec(request: JoinRequest) -> dict | None:
    """``merge_sessions(anonymize=)``: the same options, for all recordings
    at once (one shift, one pseudonym). None = patient details kept."""
    anon = request.anonymous
    if anon is None:
        return None
    return dict(
        shift_us=anon.shift_us,
        pseudonym=anon.pseudonym or request.name,
        clear=tuple(anon.clear),
        text=anon.text,
    )


def _merge_arguments(request: JoinRequest, plan: JoinPlan) -> dict:
    """What ``merge_sessions`` / ``merge.check`` are given (the recordings in
    time order), the same for both so the check is the run's own."""
    sources = _by_key(request)
    ordered = [sources[k] for k in plan.order]
    ranges = [None if plan.spans[k].whole else plan.spans[k].time_range for k in plan.order]
    return dict(
        paths=[s.source for s in ordered],
        passwords=[s.password for s in ordered],
        exclude_channels=list(request.exclude_channels) or None,
        time_ranges=ranges if any(r is not None for r in ranges) else None,
        exact=True,
        anonymize=_anonymize_spec(request),
        records=request.records,
        n_threads=0,
    )


def _anonymous_refusals(request: JoinRequest, s: JoinSource, span: Span, cancel) -> list:
    """What ``anonymize.check`` would refuse for this source cut to its kept
    part, as findings. The new recording's NAME is checked as the name of
    the copy, because that is the name the result carries."""
    import mef3io

    with stopped_by_library():
        report = mef3io.anonymize.check(
            s.source,
            _placeholder(request),
            request.anonymous.shift_us,
            password=s.password,
            cancel=cancel.for_library(),
            **_anonymize_kwargs(request, span),
        )
    out = []
    for r in report.refusals:
        if r.code in PLACEHOLDER_CODES:
            continue
        out.append(CheckFinding(s.key, _anonymous_refusal_message(r, request)))
        break
    return out


def _anonymous_refusal_message(refusal, request: JoinRequest) -> UserMessage:
    details = {
        "channel": refusal.channel,
        "field": refusal.field,
        "inspect_code": refusal.inspect_code,
    }
    paths, stems = _paths_and_stems(request)
    detail = technical_text(_RefusalText(refusal), paths=paths, stems=stems)
    return message_for(refusal.code, details, detail) or refusal_message(refusal, paths, stems)


def _bare_name(value: str) -> str:
    """A name compared without case, spaces or a typed ``.mefd`` /
    ``.mefd.tar`` ending (however many were typed)."""
    text = value.strip().casefold()
    while True:
        for ending in (".mefd.tar", ".mefd"):
            if text.endswith(ending):
                text = text[: -len(ending)].strip()
                break
        else:
            return text


def _original_name_findings(request: JoinRequest) -> list[CheckFinding]:
    """An anonymous recording is not named like an original (the name would
    give the original away). The message does not repeat the name."""
    if request.anonymous is None:
        return []
    originals = {_bare_name(s.name) for s in request.sources}
    out = []
    for what, value in (("name", request.name), ("code", request.anonymous.pseudonym)):
        if value and _bare_name(value) in originals:
            out.append(
                CheckFinding(
                    None,
                    UserMessage(
                        f"The new {what} is the name of an original",
                        f"The new recording's {what} is the same as the name of one of the "
                        "recordings it is made from, which would give it away. Nothing was "
                        "changed. Choose another one.",
                    ),
                )
            )
    return out


def check_join(request: JoinRequest, cancel: CancelToken | None = None) -> list[CheckFinding]:
    """Everything that would stop the run, before anything is written
    (mef3io's ``check()`` functions write nothing). Empty = it would start.

    * every recording's times, and the kept parts (empty, overlapping);
    * free space for the worst moment of the pipeline (:func:`sizing`);
    * one recording: ``transform.check`` (``anonymize.check`` when it is
      made anonymous) exactly as it will run;
    * several: ONE ``merge.check`` over the ORIGINALS with exactly the
      arguments the run will give ``merge_sessions`` (a window per recording,
      the channel choice, the anonymizing, the notes choice): empty parts,
      overlap on the kept data, sampling rates, patient details, date
      hiding, the name of the new recording.
    The new passwords are never passed: they play no part in any of it."""
    import mef3io

    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    with stopped_by_library(), library_warnings_caught():
        layouts, findings = read_layouts(request, cancel)
        if findings:
            return findings
        findings = _original_name_findings(request)
        if findings:
            return findings
        plan = plan_for(request, layouts)
        if not plan.ok:
            return _plan_findings(plan, request)
        problem = space_problem(request, sizing(request, plan, layouts))
        if problem:
            return [CheckFinding(None, problem)]
        library_cancel = cancel.for_library()
        anon = request.anonymous
        # Where the library is told the recording goes: for one file it is
        # made inside the hidden folder, which does not exist yet.
        target = _placeholder(request) if request.one_file else request.destination
        ignore = PLACEHOLDER_CODES if request.one_file else frozenset()
        if request.single:
            s = request.sources[0]
            span = plan.spans[s.key]
            if anon is not None:
                return _anonymous_refusals(request, s, span, cancel)
            refusals = transform_refusals(
                ChangeRequest(
                    s.source,
                    target,
                    s.password,
                    exclude_channels=request.exclude_channels,
                    time_range=None if span.whole else span.time_range,
                    records=request.records,
                ),
                cancel,
                ignore=ignore,
            )
            return [CheckFinding(s.key, _check_message(r, request)) for r in refusals[:1]]
        arguments = _merge_arguments(request, plan)
        paths = arguments.pop("paths")
        report = mef3io.merge.check(paths, target, cancel=library_cancel, **arguments)
        words = _anonymous_refusal_message if anon is not None else _check_message
        refusals = [r for r in report.refusals if r.code not in ignore]
        return _unique(
            [CheckFinding(_named_source(r, request), words(r, request)) for r in refusals]
        )


def _named_source(refusal, request: JoinRequest) -> str | None:
    """The recording a refusal of the one merge check is about, when mef3io's
    message names exactly one of the sources (the page then shows it under
    that recording); else None = about the whole run. The message is only
    read here, never shown."""
    text = str(getattr(refusal, "message", "") or "")
    found = [s for s in request.sources if s.source and s.source in text]
    # a path inside a longer named path (X.mefd in X.mefd.tar) is not named
    named = {
        s.key
        for s in found
        if not any(o.source != s.source and s.source in o.source for o in found)
    }
    return next(iter(named)) if len(named) == 1 else None


def _unique(findings: list[CheckFinding]) -> list[CheckFinding]:
    """Each reason once (mef3io reports some per channel)."""
    seen: set = set()
    out = []
    for f in findings:
        mark = (f.key, f.message.title, f.message.text)
        if mark not in seen:
            seen.add(mark)
            out.append(f)
    return out


# --------------------------------------------------------------------------
# How much room the pipeline needs


@dataclass(frozen=True)
class Sizing:
    """About how many bytes each kept part holds and the most that is on the
    drive of the new recording at any one moment: the new recording, plus
    (one file) the packed file beside it."""

    #: key -> bytes of the kept part (kept channels, kept time).
    kept: dict = field(default_factory=dict, repr=False)
    total: int = 0
    #: The worst moment: the new recording, or (one file) it + the packed file.
    peak: int = 0

    @property
    def needed(self) -> int:
        return int(self.peak * pipeline.MARGIN) + pipeline.EXTRA_BYTES


def _folder_bytes(folder: str) -> int:
    """Bytes of every file under ``folder``: one listing per folder, and
    the size of a file from the listing (on Windows it carries the size; on
    other systems one ``stat`` per file, never a second one)."""
    total = 0
    stack = [folder]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def _source_bytes(
    path: str,
    excluded: frozenset[str],
    channels: tuple[str, ...],
    known: int | None = None,
) -> int:
    """Bytes of a recording (every file under it, or the single file) times
    the share of channels kept. The app knows no MEF file names, so channels
    are assumed to be about equal in size."""
    if known is not None:
        total = int(known)
    else:
        try:
            total = os.stat(path).st_size if os.path.isfile(path) else _folder_bytes(path)
        except OSError:
            return 0
    if channels:
        kept = [c for c in channels if c not in excluded]
        total = int(total * len(kept) / len(channels))
    return total


def sizing(request: JoinRequest, plan: JoinPlan, layouts: dict) -> Sizing:
    """Estimate the bytes of every kept part (file sizes of the kept
    channels times the share of time kept; no signal is read)."""
    excluded = frozenset(request.exclude_channels)
    kept: dict[str, int] = {}
    for s in request.sources:
        layout = _as_layout(layouts.get(s.key, (None, None)))
        span = plan.spans.get(s.key)
        size = _source_bytes(s.source, excluded, layout.channels, layout.total_bytes)
        if span is not None and layout.start is not None and layout.end is not None:
            length = max(1, layout.end - layout.start)
            size = int(size * min(1.0, max(0, span.end - span.start) / length))
        kept[s.key] = size
    total = sum(kept.values())
    # The result, and (one file) beside it the packed file.
    return Sizing(kept, total, 2 * total if request.one_file else total)


def space_problem(request: JoinRequest, size: Sizing) -> UserMessage | None:
    """A plain message when the drive of the new recording is short of room
    for the worst moment of the run, else None."""
    from .timefmt import format_size

    free = pipeline.free_bytes(os.path.dirname(os.path.abspath(request.destination)))
    if free is None or free >= size.needed:
        return None
    return UserMessage(
        "There is not enough free space",
        f"The drive of the chosen folder has {format_size(free)} free. While the new "
        f"recording is made it needs about {format_size(size.needed)} there"
        f"{' (the folder it is packed from is removed afterwards)' if request.one_file else ''}. "
        "Free some space or choose another folder. "
        f"{NOTHING_JOINED}",
    )


# --------------------------------------------------------------------------
# The run


def make_joined(
    request: JoinRequest,
    send: Callable[[JoinProgress], object] | None = None,
    cancel: CancelToken | None = None,
) -> JoinResult:
    """Make the new recording (in a helper process). A refusal or failure
    comes back as a :class:`JoinResult` with a plain message; a Stop is
    raised as :class:`JobCancelled`. The temporary parts folder is removed
    whatever happens."""
    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    anonymous, one_file = request.anonymous is not None, request.one_file
    try:
        with stopped_by_library(), library_warnings_caught():
            layouts, findings = read_layouts(request, cancel)
            if findings:
                return JoinResult(request.destination, message=findings[0].message)
            findings = _original_name_findings(request)
            if findings:
                return JoinResult(request.destination, message=findings[0].message)
            plan = plan_for(request, layouts)
            if not plan.ok:
                return JoinResult(
                    request.destination, message=_plan_findings(plan, request)[0].message
                )
            size = sizing(request, plan, layouts)
            problem = space_problem(request, size)
            if problem is not None:
                return JoinResult(request.destination, message=problem)
            report = run(request, plan, size, send, cancel, layouts)
    except JobCancelled:
        raise
    except archive.TargetExists:
        return JoinResult(
            request.destination,
            message=UserMessage(
                "A file with that name appeared",
                "Something with the name of the new file appeared in the chosen folder while "
                "it was being made, so nothing was saved (nothing there was replaced). Choose "
                "another name or folder and try again.",
            ),
            parts_left=not remove_parts(request),
        )
    except Exception as exc:
        if is_stop(exc):
            raise JobCancelled() from None
        return JoinResult(
            request.destination,
            message=describe_join_error(exc, request),
            retryable=is_unreachable(exc),
            parts_left=not remove_parts(request),
        )
    finally:
        remove_parts(request)
    return JoinResult(
        request.destination,
        ok=True,
        channels_kept=tuple(report.channels_kept),
        channels_excluded=tuple(report.channels_excluded),
        bytes_written=int(report.bytes_written),
        notes=tuple(str(n) for n in getattr(report, "notes", ()) or ()),
        parts_left=not remove_parts(request),
        anonymous=anonymous,
        one_file=one_file,
        notes_left_out=request.records == RECORDS_DROP,
        records_dropped=int(getattr(report, "records_dropped", 0) or 0),
    )


@dataclass(frozen=True)
class _Stage:
    kind: str
    index: int
    count: int
    weight: float


def _stages(request: JoinRequest, plan: JoinPlan, size: Sizing) -> list[_Stage]:
    """The stages of this run, in order, weighted by the bytes they handle
    (the bar then moves evenly): the one pass that makes the recording (the
    copy and mef3io's check of it), then, for one file, packing and
    comparing. Packing reads and writes the local drive, so it counts a
    little less than reading the originals."""
    total = max(1, size.total)
    stages = [_Stage("trim" if request.single else "join", 1, 1, total)]
    if request.one_file:
        stages.append(_Stage("pack", 1, 1, max(1, int(total * 0.6))))
        stages.append(_Stage("check", 1, 1, max(1, int(total * 0.05))))
    return stages


def run(
    request: JoinRequest,
    plan: JoinPlan,
    size: Sizing,
    send: Callable[[JoinProgress], object] | None,
    cancel: CancelToken,
    layouts: dict | None = None,
):
    """THE pipeline of one join or trim, from the originals to the finished
    recording (folder or one file): ONE mef3io call (``merge_sessions``, or
    for a single recording ``transform_session`` / ``anonymize_session``),
    then, for one file, packing. Returns a report with ``channels_kept``,
    ``channels_excluded``, ``bytes_written``, ``notes``, ``records_dropped``."""
    import mef3io

    library_cancel = cancel.for_library()
    anon = request.anonymous
    stages = _stages(request, plan, size)
    steps = float(sum(st.weight for st in stages))
    position = {"i": 0}

    def progress(kind: str):
        """Byte progress of the current stage (the next of this kind)."""
        if send is None:
            return None
        i = next(n for n, st in enumerate(stages) if st.kind == kind and n >= position["i"])
        position["i"] = i + 1
        st = stages[i]
        before = float(sum(x.weight for x in stages[:i]))
        return byte_progress(
            lambda b: send(
                JoinProgress(
                    st.kind,
                    st.index,
                    st.count,
                    before,
                    steps,
                    b.fraction,
                    st.weight,
                    anon is not None,
                )
            )
        )

    def announce(kind: str) -> None:
        """A stage with no byte progress of its own: show it started."""
        callback = progress(kind)
        if callback is not None:
            callback(0, 1)

    if request.one_file:
        make_parts_folder(request)
    built = request.built_path
    if request.single:
        s = request.sources[0]
        span = plan.spans[s.key]
        if anon is not None:
            report = mef3io.anonymize_session(
                s.source,
                built,
                anon.shift_us,
                password=s.password,
                output_password=request.output_password,
                progress=progress("trim"),
                cancel=library_cancel,
                **_anonymize_kwargs(request, span),
            )
        else:
            report = transform_copy(
                ChangeRequest(
                    s.source,
                    built,
                    s.password,
                    exclude_channels=request.exclude_channels,
                    output_password=request.output_password,
                    time_range=None if span.whole else span.time_range,
                    records=request.records,
                ),
                progress("trim"),
                cancel,
            )
    else:
        arguments = _merge_arguments(request, plan)
        paths = arguments.pop("paths")
        # ONE call, from the originals: the kept part of each, the channels
        # left out, anonymous, with or without the notes, and the new
        # passwords, in one pass over the data.
        report = mef3io.merge_sessions(
            paths,
            built,
            output_password=request.output_password,
            progress=progress("join"),
            cancel=library_cancel,
            **arguments,
        )
    if request.one_file:
        cancel.raise_if_cancelled()
        _pack(request, announce, progress, cancel)
    return report


def _pack(request: JoinRequest, announce, progress, cancel: CancelToken) -> None:
    """Pack the finished recording (still in the hidden folder) into ONE
    file, compare the file with it, and only then give it its final name,
    never replacing anything (``model.archive``)."""
    packing = archive.ArchivePlan(
        request.built_path, request.destination, request.packed_path, True
    )
    if os.path.lexists(request.destination):
        raise archive.TargetExists()
    output = request.output_password
    pack_progress = progress("pack")

    def got(message) -> None:
        if message == "checking":
            announce("check")
        elif pack_progress is not None and hasattr(message, "done"):
            pack_progress(message.done, message.total)

    archive.run_in_helper(packing, output[1] if output else "", got, cancel)
    cancel.raise_if_cancelled()
    archive.finish(packing)


def make_parts_folder(request: JoinRequest) -> None:
    """Create the parts folder and, at once, the marker that proves it is
    this run's. Never an existing folder (``mkdir`` refuses one: it is not
    ours to fill or remove). If the marker cannot be written, the folder,
    still empty, is removed again before the error is raised."""
    os.mkdir(request.parts_folder)
    try:
        with open(os.path.join(request.parts_folder, PARTS_MARKER), "x", encoding="ascii") as f:
            f.write(request.run_token)
    except BaseException:
        # The marker may exist half-written (e.g. a full disk): remove it,
        # then the folder this call just made, so nothing is left behind.
        for remove in (
            lambda: os.unlink(os.path.join(request.parts_folder, PARTS_MARKER)),
            lambda: os.rmdir(request.parts_folder),
        ):
            try:
                remove()
            except OSError:
                pass
        raise


def _is_ours(request: JoinRequest) -> bool:
    folder = request.parts_folder
    if not request.run_token or not os.path.basename(folder).startswith(PARTS_PREFIX):
        return False
    if not os.path.isdir(folder) or os.path.islink(folder):
        return False
    try:
        with open(os.path.join(folder, PARTS_MARKER), encoding="ascii") as f:
            return f.read() == request.run_token
    except (OSError, UnicodeDecodeError):
        return False


def remove_parts(request: JoinRequest) -> bool:
    """Remove the temporary parts folder, ONLY when it carries this run's
    marker (:func:`make_parts_folder`): a folder that was already there is
    never touched. True when nothing of this run is left."""
    if not _is_ours(request):
        return True
    shutil.rmtree(request.parts_folder, ignore_errors=True)
    return not os.path.lexists(request.parts_folder)


def tidy(request: JoinRequest, partial_was_there: bool = False) -> bool:
    """After a helper that had to be ended: remove the temporary parts and
    an unfinished ``<destination>.partial`` this run made. True when
    nothing is left."""
    parts = remove_parts(request)
    partial = True if partial_was_there else remove_unfinished(request.destination)
    return parts and partial


def progress_phase(message: JoinProgress) -> str:
    """What the progress bar says for one stage (plain words)."""
    anonymous = " and making it anonymous" if message.anonymous else ""
    if message.kind == "join":
        return (
            f"Joining the chosen parts of the recordings into one{anonymous}, "
            "then checking the new recording"
        )
    if message.kind == "pack":
        return "Packing the new recording into one file"
    if message.kind == "check":
        return "Checking that the file holds the same recording"
    return f"Saving the chosen part{anonymous}, then checking it"
