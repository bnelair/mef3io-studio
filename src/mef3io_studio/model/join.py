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

Privacy: paths, names, passwords and times are ``repr=False`` and never
logged. Plain Python, no Qt.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from .anonymize import (
    copy_name,
    describe_copy_error,
    is_disk_full,
    is_unreachable,
    refusal_code,
    remove_unfinished,
    technical_text,
)
from .cancel import CancelToken, JobCancelled, is_stop, stopped_by_library
from .errors import UserMessage
from .progress import byte_progress
from .quiet import library_warnings_caught
from .recording import recording_name
from .refusals import NOTHING_JOINED, REPORT, join_message_for

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
class JoinRequest:
    """Everything the helper process needs. Plain data; not one field may
    be logged (paths and passwords)."""

    sources: tuple[JoinSource, ...]
    #: The new recording (a folder ending in ``.mefd``).
    destination: str = field(repr=False)
    exclude_channels: tuple[str, ...] = field(default=(), repr=False)
    #: ``(basic, full-access)`` for the new recording, or None.
    output_password: tuple[str, str] | None = field(default=None, repr=False)
    #: The temporary parts folder (:func:`parts_folder_for`).
    parts_folder: str = field(default="", repr=False)
    #: Proves a parts folder is this run's (:data:`PARTS_MARKER`).
    run_token: str = field(default="", repr=False)

    @property
    def name(self) -> str:
        return copy_name(self.destination)

    @property
    def changes(self) -> bool:
        return bool(self.exclude_channels) or self.output_password is not None

    @property
    def single(self) -> bool:
        return len(self.sources) == 1


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
) -> JoinRequest:
    return JoinRequest(
        tuple(sources),
        destination,
        tuple(exclude_channels),
        output_password,
        parts_folder_for(destination),
        secrets.token_hex(16),
    )


@dataclass(frozen=True)
class JoinProgress:
    """How far one step has got (plain data, sent from the helper).
    ``kind``: "cut" (keeping the part of recording ``index`` of the ones to
    cut), "join" or "trim" (one recording straight into the destination)."""

    kind: str
    index: int
    count: int
    step: int
    steps: int
    fraction: float

    @property
    def overall(self) -> float:
        if self.steps <= 0:
            return 0.0
        return min(1.0, (self.step + max(0.0, min(1.0, self.fraction))) / self.steps)


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
    """A refusal or failure in plain words: by mef3io's refusal code
    (``JOIN_WORDS`` has every code of transform and merge), else by the
    kind of error (the anonymizer's words, which fit any copy)."""
    detail = join_technical_text(exc, request)
    if is_disk_full(exc):
        return UserMessage(
            "The destination is full",
            "There was not enough free space to finish the new recording. Anything the app "
            "had started was removed and the original recordings were not changed. Free some "
            "space or choose another folder, then try again.",
            detail,
        )
    code = refusal_code(exc)
    if code is not None:
        message = join_message_for(code, getattr(exc, "details", None) or {}, detail)
        if message is not None:
            return message
    return replace(describe_copy_error(exc), detail=detail)


def _check_message(refusal, request: JoinRequest) -> UserMessage:
    details = {
        "channel": refusal.channel,
        "field": refusal.field,
        "inspect_code": refusal.inspect_code,
    }
    exc = RuntimeError(f"[{refusal.code}] {refusal.message}")
    detail = join_technical_text(exc, request)
    message = join_message_for(refusal.code, details, detail)
    if message is None:
        message = UserMessage(
            "The new recording could not be made",
            f"{NOTHING_JOINED} {REPORT}",
            detail,
        )
    return message


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
            found = mef3io.time_range(s.source, s.password, cancel=cancel.for_library())
            if not found.offset_known:
                # Password-protected, and the full-access password was not
                # given: its times cannot be confirmed (nor the part cut).
                findings.append(CheckFinding(s.key, join_message_for("password-required", {})))
                continue
            reader = mef3io.Reader(s.source, password=s.password, warn_declarations=False)
            channels = tuple(reader.channels)
            ends = [_last_sample_end(reader, c, found.end_uutc) for c in channels]
            ends = [e for e in ends if e is not None]
            end = max(ends) if ends else int(found.end_uutc)
            del reader
        except Exception as exc:
            if is_stop(exc):
                raise
            findings.append(CheckFinding(s.key, describe_join_error(exc, request)))
            continue
        layouts[s.key] = Layout(int(found.start_uutc), end, channels)
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


def check_join(request: JoinRequest, cancel: CancelToken | None = None) -> list[CheckFinding]:
    """Everything that would stop the run, before anything is written
    (mef3io's ``check()`` functions write nothing). Empty = it would start.

    * every recording's times, and the kept parts (empty, overlapping);
    * one recording: ``transform.check`` exactly as it will run;
    * several: ``transform.check`` for each part to cut (its window; the
      temporary copy does not exist yet, so its place is not checked), and
      ``merge.check`` over the ORIGINALS with the real destination, the
      channel choice and the passwords: sampling rates, patient details,
      date hiding, the destination. Its ``merge-overlap`` is left out when
      a part is cut (the originals may overlap where nothing is kept; the
      kept parts were checked above, and the join itself checks again).
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
        sources = _by_key(request)
        library_cancel = cancel.for_library()
        if request.single:
            s = request.sources[0]
            span = plan.spans[s.key]
            report = mef3io.transform.check(
                s.source,
                request.destination,
                exclude_channels=list(request.exclude_channels) or None,
                time_range=None if span.whole else span.time_range,
                exact=True,
                password=s.password,
                cancel=library_cancel,
            )
            return [CheckFinding(s.key, _check_message(r, request)) for r in report.refusals[:1]]
        placeholder = os.path.join(
            tempfile.gettempdir(), ".mef3io-studio-not-a-folder", "part.mefd"
        )
        cut = False
        for key in plan.order:
            span = plan.spans[key]
            if span.whole:
                continue
            cut = True
            s = sources[key]
            report = mef3io.transform.check(
                s.source,
                placeholder,
                time_range=span.time_range,
                exact=True,
                password=s.password,
                cancel=library_cancel,
            )
            refusals = [r for r in report.refusals if r.code not in _PLACEHOLDER_CODES]
            findings += [CheckFinding(key, _check_message(r, request)) for r in refusals[:1]]
        if findings:
            return findings
        ordered = [sources[k] for k in plan.order]
        report = mef3io.merge.check(
            [s.source for s in ordered],
            request.destination,
            passwords=[s.password for s in ordered],
            exclude_channels=list(request.exclude_channels) or None,
            cancel=library_cancel,
        )
        refusals = [r for r in report.refusals if not (cut and r.code == "merge-overlap")]
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


#: Refusals about the placeholder destination of a part not made yet.
_PLACEHOLDER_CODES = frozenset({"dst-not-mefd", "dst-exists", "partial-exists", "dst-inside-src"})


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
            report = _run(request, plan, send, cancel)
    except JobCancelled:
        raise
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
    )


def _run(request: JoinRequest, plan: JoinPlan, send, cancel: CancelToken):
    import mef3io

    sources = _by_key(request)
    library_cancel = cancel.for_library()

    def progress(kind: str, index: int, count: int, step: int, steps: int):
        if send is None:
            return None
        return byte_progress(
            lambda b: send(JoinProgress(kind, index, count, step, steps, b.fraction))
        )

    exclude = list(request.exclude_channels) or None
    if request.single:
        s = request.sources[0]
        span = plan.spans[s.key]
        return mef3io.transform_session(
            s.source,
            request.destination,
            exclude_channels=exclude,
            output_password=request.output_password,
            time_range=None if span.whole else span.time_range,
            exact=True,
            password=s.password,
            progress=progress("trim", 1, 1, 0, 1),
            cancel=library_cancel,
        )
    to_cut = [k for k in plan.order if not plan.spans[k].whole]
    steps = len(to_cut) + 1
    paths: list[str] = []
    passwords: list[str] = []
    temporary = None
    if to_cut:
        make_parts_folder(request)
        temporary = _throwaway_passwords()
    for key in plan.order:
        s = sources[key]
        span = plan.spans[key]
        if span.whole:
            paths.append(s.source)
            passwords.append(s.password)
            continue
        cancel.raise_if_cancelled()
        n = to_cut.index(key)
        part = os.path.join(request.parts_folder, f"part-{n + 1}.mefd")
        mef3io.transform_session(
            s.source,
            part,
            time_range=span.time_range,
            exact=True,
            password=s.password,
            # Protected: see the module's notes on password safety.
            output_password=temporary,
            progress=progress("cut", n + 1, len(to_cut), n, steps),
            cancel=library_cancel,
        )
        paths.append(part)
        passwords.append(temporary[1])
    cancel.raise_if_cancelled()
    return mef3io.merge_sessions(
        paths,
        request.destination,
        passwords=passwords,
        exclude_channels=exclude,
        output_password=request.output_password,
        progress=progress("join", 1, 1, steps - 1, steps),
        cancel=library_cancel,
    )


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
    """What the progress bar says for one step."""
    if message.kind == "cut":
        return f"Keeping the chosen part of recording {message.index} of {message.count}"
    if message.kind == "join":
        return "Joining the parts into one recording, then checking it"
    return "Saving the chosen part, then checking it"
