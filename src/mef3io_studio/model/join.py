"""Join or trim recordings: keep a chosen part of each, make ONE new recording.

The user picks one or more recordings and, for each, what to keep: all of
it, the first or the last so many hours/minutes/seconds, or everything
between two times. The result is one new recording; the originals are never
changed. All the work on the files is mef3io's (1.5):

* ``mef3io.time_range`` (a few small files per channel) turns "the first
  hour" into a window and orders the recordings by time;
* ``mef3io.transform_session(..., time_range=, exact=True)`` cuts one
  recording: a single recording is trimmed straight into the destination;
* ``mef3io.merge_sessions`` joins several into one.

Each of them builds under ``<dst>.partial/``, checks what it wrote against
its sources and only then gives it its final name; a refusal, a failure or
a Stop leaves nothing behind.

How several recordings are joined (:func:`make_joined`, in a helper process):

1. A recording kept whole is given to ``merge_sessions`` as it is.
2. Any other is first cut into a temporary copy holding exactly its kept
   part. The temporary copies live in ONE folder next to the destination,
   :data:`PARTS_PREFIX` + random hex (opaque: no names; the dot hides it;
   folder scans skip the app's ``.mef3io-studio-`` names), never inside a
   recording, and the folder is ALWAYS removed afterwards: success, refusal,
   failure or Stop (in the helper's ``finally``, and again by the task when
   a helper had to be ended: :func:`tidy`).
3. ``merge_sessions`` joins the parts, in time order, into the destination,
   leaving out the chosen channels and adding the chosen password
   protection. The channel choice and the passwords therefore apply once,
   to the result.

Password safety: every temporary copy is PASSWORD-PROTECTED with two
throw-away passwords made in the helper process for this run only (never
sent back, logged or stored). A copy made without passwords would hold the
patient details and the text of notes readable on disk, beside the
destination, for as long as the join runs (or after a crash, until someone
deletes the folder): exactly what the original's password protection was
there to prevent. The same is done for unprotected originals too: one rule
is simpler to check, and costs only re-protecting a few small details
files (the signal itself is never password-protected in MEF).

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

End times: a recording's end is where its last sample ENDS (exclusive).
mef3io's ``time_range`` reports the end its headers state, which is that
for mef3io's own files but may be the last sample's own time for another
writer's (one sample period earlier). So the run takes each recording's end
from the index instead: the end of the last block of each channel
(``Reader.block_envelope``, which reads only the last part's block index
and gives ``start + samples / rate``), see :func:`read_layouts`. A window
that reaches the recording's end is also handed to mef3io one microsecond
past it (``Span.to_end``), so the last sample is kept either way.

One recording kept whole with no channel left out and no new passwords
would be an exact copy: refused as "nothing would change"
(:data:`NOTHING_TO_CHANGE`). Kept whole WITH a change (channels or
passwords), it is a copy with those changes, by ``transform_session``.

ANONYMOUS and ONE FILE (:class:`Anonymous`, ``JoinRequest.one_file``): the
whole pipeline is ONE function, :func:`run`, which the page never looks
inside. No copy that still holds the patient's details is ever written:

* every source is anonymized straight from the original, cut to its kept
  part (``anonymize_session(time_range=, exact=True, exclude_channels=)``,
  one shift and one pseudonym for all), into a hidden folder next to the
  new recording (:data:`PARTS_PREFIX`, same drive as the result, so the
  originals may sit on a slow network drive and are read once per pass);
* the anonymized parts are joined with ``merge_sessions`` (it accepts them:
  the cleared patient fields are equal; the session name comes from the
  destination), and the parts are removed at once;
* for one file the joined recording, still in the hidden folder, is packed
  with ``archive_session``, the pack is compared with it, and only then is
  it given its final name (never replacing anything).

The same stages, in the same order, are what a later single-pass mef3io
call replaces; the page only sees :class:`JoinRequest`, :func:`check_join`,
:func:`make_joined` and :class:`JoinProgress`. Room is left for a later
"notes and events: keep / leave out" option (:class:`Anonymous` is the one
place to add it).

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
    CLEAR_DESCRIPTIONS,
    CLEAR_LOCATION,
    CLEAR_PATIENT,
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
#: Written into the parts folder right after this run creates it, holding
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
    one copy). A later "notes and events: keep / leave out" option (a
    mef3io option that does not exist yet) is one more field here."""

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
    def parts_subfolder(self) -> str:
        return os.path.join(self.parts_folder, "parts")

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
    )


@dataclass(frozen=True)
class JoinProgress:
    """How far one stage has got (plain data, sent from the helper).
    ``kind``: "cut" (keeping the part of recording ``index`` of the
    ``count`` to cut; anonymous = made anonymous at the same time), "trim"
    (one recording straight into the new recording), "join", "pack" or
    "check" (the one file). ``step``/``steps``/``weight`` are stage sizes
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
    given) is reported, not planned."""
    import mef3io

    layouts: dict[str, Layout] = {}
    findings: list[CheckFinding] = []
    for s in request.sources:
        cancel.raise_if_cancelled()
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

#: merge.check fields that an anonymous copy clears in every part before the
#: parts are joined: differences there are not a problem.
_CLEARED_BY = {
    "subject_name_1": CLEAR_PATIENT,
    "subject_name_2": CLEAR_PATIENT,
    "subject_id": CLEAR_PATIENT,
    "recording_location": CLEAR_LOCATION,
    "session_description": CLEAR_DESCRIPTIONS,
    "channel_description": CLEAR_DESCRIPTIONS,
    "reference_description": CLEAR_DESCRIPTIONS,
}


def _placeholder(request: JoinRequest) -> str:
    """A path for the new recording that does not exist (checks write
    nothing; refusals about the place are left out by the caller)."""
    return os.path.join(_PLACEHOLDER_DIR, request.name + ".mefd")


def _anonymize_kwargs(request: JoinRequest, span: Span | None) -> dict:
    anon = request.anonymous
    return dict(
        pseudonym=anon.pseudonym or request.name,
        clear=tuple(anon.clear),
        text=anon.text,
        exclude_channels=list(request.exclude_channels) or None,
        time_range=None if span is None or span.whole else span.time_range,
        exact=True,
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


def check_join(request: JoinRequest, cancel: CancelToken | None = None) -> list[CheckFinding]:
    """Everything that would stop the run, before anything is written
    (mef3io's ``check()`` functions write nothing). Empty = it would start.

    * every recording's times, and the kept parts (empty, overlapping);
    * free space for the worst moment of the pipeline (:func:`sizing`);
    * one recording: ``transform.check`` (``anonymize.check`` when it is
      made anonymous) exactly as it will run;
    * several: ``transform.check`` / ``anonymize.check`` for each part to
      cut (its window; the temporary copy does not exist yet, so its place
      is not checked), and ``merge.check`` over the ORIGINALS with the
      channel choice and the passwords: sampling rates, patient details,
      date hiding, the destination. Its ``merge-overlap`` is left out when
      a part is cut (the originals may overlap where nothing is kept; the
      kept parts were checked above, and the join itself checks again), and
      so are differences in what an anonymous copy clears.
    The new passwords are never passed: they play no part in any of it."""
    import mef3io

    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    with stopped_by_library(), library_warnings_caught():
        layouts, findings = read_layouts(request, cancel)
        if findings:
            return findings
        plan = plan_for(request, layouts)
        if not plan.ok:
            return _plan_findings(plan, request)
        problem = space_problem(request, sizing(request, plan, layouts))
        if problem:
            return [CheckFinding(None, problem)]
        sources = _by_key(request)
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
                ),
                cancel,
                ignore=ignore,
            )
            return [CheckFinding(s.key, _check_message(r, request)) for r in refusals[:1]]
        placeholder = os.path.join(_PLACEHOLDER_DIR, "part.mefd")
        cut = anon is not None
        for key in plan.order:
            span = plan.spans[key]
            s = sources[key]
            if anon is not None:
                findings += _anonymous_refusals(request, s, span, cancel)
                continue
            if span.whole:
                continue
            cut = True
            refusals = transform_refusals(
                ChangeRequest(s.source, placeholder, s.password, time_range=span.time_range),
                cancel,
                ignore=PLACEHOLDER_CODES,
            )
            findings += [CheckFinding(key, _check_message(r, request)) for r in refusals[:1]]
        if findings:
            return findings
        ordered = [sources[k] for k in plan.order]
        report = mef3io.merge.check(
            [s.source for s in ordered],
            target,
            passwords=[s.password for s in ordered],
            exclude_channels=list(request.exclude_channels) or None,
            cancel=library_cancel,
        )
        cleared = set(anon.clear) if anon is not None else set()
        refusals = [
            r
            for r in report.refusals
            if not (cut and r.code == "merge-overlap")
            and r.code not in ignore
            and not (r.code == "merge-identity-differs" and _CLEARED_BY.get(r.field) in cleared)
        ]
        return _unique([CheckFinding(None, _check_message(r, request)) for r in refusals])


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
    drive of the new recording at any one moment."""

    #: key -> bytes of the kept part (kept channels, kept time).
    kept: dict = field(default_factory=dict, repr=False)
    total: int = 0
    #: The worst moment: parts + the joined recording, or (one file) the
    #: joined recording + the packed file.
    peak: int = 0

    @property
    def needed(self) -> int:
        return int(self.peak * pipeline.MARGIN) + pipeline.EXTRA_BYTES


def _folder_bytes(folder: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _source_bytes(path: str, excluded: frozenset[str], channels: tuple[str, ...]) -> int:
    """Bytes of a recording (every file under it, or the single file) times
    the share of channels kept. The app knows no MEF file names, so channels
    are assumed to be about equal in size."""
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
        size = _source_bytes(s.source, excluded, layout.channels)
        if span is not None and layout.start is not None and layout.end is not None:
            length = max(1, layout.end - layout.start)
            size = int(size * min(1.0, max(0, span.end - span.start) / length))
        kept[s.key] = size
    total = sum(kept.values())
    anonymous = request.anonymous is not None
    cut_total = sum(
        v for k, v in kept.items() if anonymous or (k in plan.spans and not plan.spans[k].whole)
    )
    first = total if request.single else cut_total + total
    second = 2 * total if request.one_file else 0
    return Sizing(kept, total, max(first, second))


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
        f"recording is made it needs about {format_size(size.needed)} there (the temporary "
        "copies are removed afterwards). Free some space or choose another folder. "
        f"{NOTHING_JOINED}",
    )


# --------------------------------------------------------------------------
# The run


def _throwaway_passwords() -> tuple[str, str]:
    """Two different passwords for the temporary copies of this run only:
    ASCII, 12 characters (every MEF program accepts them)."""
    basic = "b" + secrets.token_hex(6)[:11]
    full = "f" + secrets.token_hex(6)[:11]
    return basic, full


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
            plan = plan_for(request, layouts)
            if not plan.ok:
                return JoinResult(
                    request.destination, message=_plan_findings(plan, request)[0].message
                )
            size = sizing(request, plan, layouts)
            problem = space_problem(request, size)
            if problem is not None:
                return JoinResult(request.destination, message=problem)
            report = run(request, plan, size, send, cancel)
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
    )


@dataclass(frozen=True)
class _Stage:
    kind: str
    index: int
    count: int
    weight: float


def _stages(request: JoinRequest, plan: JoinPlan, size: Sizing) -> list[_Stage]:
    """The stages of this run, in order, weighted by the bytes they handle
    (the bar then moves evenly). Merging and packing read and write the
    local drive, so they count a little less than reading the originals."""
    anonymous = request.anonymous is not None
    total = max(1, size.total)
    stages: list[_Stage] = []
    if request.single:
        stages.append(_Stage("trim", 1, 1, total))
    else:
        cuts = [k for k in plan.order if anonymous or not plan.spans[k].whole]
        for n, key in enumerate(cuts):
            stages.append(_Stage("cut", n + 1, len(cuts), max(1, size.kept.get(key, 0))))
        stages.append(_Stage("join", 1, 1, max(1, int(total * 0.6))))
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
):
    """THE pipeline: every stage of one join or trim, from the originals to
    the finished recording (folder or one file). Returns a report with
    ``channels_kept``, ``channels_excluded``, ``bytes_written``, ``notes``.
    A single mef3io call that does several of these stages in one pass can
    replace the body of this function; nothing outside it depends on the
    stages."""
    import mef3io

    sources = _by_key(request)
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

    exclude = list(request.exclude_channels) or None
    if request.one_file or (not request.single and (anon is not None or _cuts(request, plan))):
        make_parts_folder(request)
    built = request.built_path

    def anonymize(s: JoinSource, dst: str, span, kind: str, output_password=None):
        return mef3io.anonymize_session(
            s.source,
            dst,
            anon.shift_us,
            password=s.password,
            output_password=output_password,
            progress=progress(kind),
            cancel=library_cancel,
            **_anonymize_kwargs(request, span),
        )

    if request.single:
        s = request.sources[0]
        span = plan.spans[s.key]
        if anon is not None:
            report = anonymize(s, built, span, "trim", request.output_password)
        else:
            report = transform_copy(
                ChangeRequest(
                    s.source,
                    built,
                    s.password,
                    exclude_channels=request.exclude_channels,
                    output_password=request.output_password,
                    time_range=None if span.whole else span.time_range,
                ),
                progress("trim"),
                cancel,
            )
    else:
        paths: list[str] = []
        passwords: list[str] = []
        temporary = None
        if anon is None and _cuts(request, plan):
            temporary = _throwaway_passwords()
        if anon is not None or temporary is not None:
            os.mkdir(request.parts_subfolder)
        n = 0
        for key in plan.order:
            s = sources[key]
            span = plan.spans[key]
            if anon is None and span.whole:
                paths.append(s.source)
                passwords.append(s.password)
                continue
            cancel.raise_if_cancelled()
            n += 1
            part = os.path.join(request.parts_subfolder, f"part-{n}.mefd")
            if anon is not None:
                # Anonymous from the start: the part holds no patient details
                # (so it needs no temporary password).
                anonymize(s, part, span, "cut")
                paths.append(part)
                passwords.append("")
            else:
                transform_copy(
                    # Protected: see the module's notes on password safety.
                    ChangeRequest(
                        s.source,
                        part,
                        s.password,
                        output_password=temporary,
                        time_range=span.time_range,
                    ),
                    progress("cut"),
                    cancel,
                )
                paths.append(part)
                passwords.append(temporary[1])
        cancel.raise_if_cancelled()
        report = mef3io.merge_sessions(
            paths,
            built,
            passwords=passwords,
            # An anonymous part was made without the left-out channels.
            exclude_channels=None if anon is not None else exclude,
            output_password=request.output_password,
            progress=progress("join"),
            cancel=library_cancel,
        )
        # The parts are no longer needed: free their room before packing.
        if os.path.isdir(request.parts_subfolder):
            shutil.rmtree(request.parts_subfolder, ignore_errors=True)
    if request.one_file:
        cancel.raise_if_cancelled()
        _pack(request, announce, progress, cancel)
    return report


def _cuts(request: JoinRequest, plan: JoinPlan) -> bool:
    return any(not plan.spans[k].whole for k in plan.order)


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
    if message.kind == "cut":
        return (
            f"Keeping the chosen part of recording {message.index} of {message.count}"
            f"{anonymous}, then checking it"
        )
    if message.kind == "join":
        return "Joining the parts into one recording, then checking it"
    if message.kind == "pack":
        return "Packing the new recording into one file"
    if message.kind == "check":
        return "Checking that the file holds the same recording"
    return f"Saving the chosen part{anonymous}, then checking it"
