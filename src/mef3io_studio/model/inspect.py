"""Read what a recording contains, without reading its signal.

A thin adapter over ``mef3io.inspect()`` (mef3io 1.3, bnelair/mef3io#67),
which describes a recording from its details files (16 kB per part, fixed
size), its notes files and, for a single-file recording, its table of
contents. It never reads the signal or the block table: the signal files
are measured by size only. tests/test_explorer_bytes.py counts the bytes the
helper process reads and pins that at well under 1% of the recording.

What this module adds on top:

* **Damaged parts don't hide the rest.** ``inspect`` runs with
  ``strict=False``: a part (or notes file) that cannot be read is listed in
  :attr:`RecordingDetails.part_problems` / ``notes_problem`` and the rest is
  shown. Only when nothing at all can be read is the recording an error.
* **Times are honest.** With only the basic password, the part of the file
  that says how times are stored stays locked; mef3io then reads the times
  assuming the common case and says so (``offset_assumed_zero``). They are
  shown, labelled unverified, never called verified.
* **Passwords.** No password on a protected recording asks for one (the
  rest of the app's flow); a wrong one says so. A password that only opened
  the recording through the rule mef3io used up to 1.2.0 (non-ASCII
  characters, bnelair/mef3io#84) sets ``legacy_password`` for a short note.
* **Stop and progress.** A :class:`CancelToken` stops ``inspect`` from
  inside (mef3io checks it before each part); progress is reported in bytes
  of details files read.

Plain Python, no Qt. Runs in a helper process (see ``workers.isolated``).
Never logs anything: the locations and values it handles identify people.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from .cancel import CancelToken, JobCancelled, is_stop, stopped_by_library
from .errors import UserMessage, describe_error
from .progress import ByteProgress, byte_progress
from .quiet import library_warnings_caught
from .recording import (
    ACCESS_BASIC,
    ACCESS_FULL,
    ACCESS_OPEN,
    OFFSET_DISPUTED,
    OFFSET_LOCKED,
    OFFSET_NONE,
    ChannelInfo,
    History,
    IdentityFact,
    InspectOutcome,
    Note,
    Part,
    PartProblem,
    PatientDetails,
    RecordingDetails,
    is_packed,
)

#: Which levels of notes this version reads (session, channel and part).
NOTE_LEVELS = ("session", "channel", "segment")

#: Identity fields mef3io reports, by what the app calls them.
_PATIENT_FIELDS = {
    "first_name": "subject_name_1",
    "last_name": "subject_name_2",
    "patient_id": "subject_id",
    "location": "recording_location",
}

#: Identity values the log filter learns (note text deliberately not: see
#: RecordingDetails.identity_values).
_LOGGED_IDENTITY = (
    *_PATIENT_FIELDS.values(),
    "session_description",
    "channel_description",
    "reference_description",
    "session_name",
    "anonymized_name",
)

NOTHING_READABLE = UserMessage(
    "This recording looks damaged",
    "None of its parts could be read, so there is nothing to show. Nothing was "
    'changed. Use "Check for problems" to see why, and keep a copy of the original.',
)

NOTES_DAMAGED = UserMessage(
    "Part of this recording looks damaged",
    "A file that holds notes and events could not be read, so some may be missing. "
    'The signal is not affected and nothing was changed. Use "Check for problems" '
    "to see which, and keep a copy of the original.",
)


def size_on_disk(location: str) -> int | None:
    """Bytes the recording takes on disk, from file sizes only (``stat``,
    which reads no file contents). None if it cannot be measured."""
    try:
        if is_packed(location):
            return os.stat(location).st_size
        total = 0
        stack = [location]
        while stack:
            folder = stack.pop()
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        return total
    except OSError:
        return None


def _strip_location(text: str, location: str) -> str:
    """Technical text without the recording's location (it can identify)."""
    for variant in {location, location.replace("\\", "/"), os.path.basename(location)}:
        if variant:
            text = text.replace(variant, "<recording>")
    return text


def _history(value) -> History | None:
    if not value:
        return None
    return History(
        created_by=str(value.get("created_by") or ""),
        last_modified_by=str(value.get("last_modified_by") or ""),
        last_operation=str(value.get("last_operation") or ""),
        operations=tuple(str(op) for op in value.get("operations") or ()),
        modification_count=int(value.get("modification_count") or 0),
    )


def _texts(values) -> list[str]:
    return list(dict.fromkeys(t for t in (str(v).strip() for v in values or ()) if t))


def _joined(values) -> str | None:
    """One text for a field that may hold different values in different
    parts (normally one). None when it holds nothing anywhere."""
    texts = _texts(values)
    return "; ".join(texts) if texts else None


def _note_text(fields: dict | None) -> str:
    if not fields:
        return ""
    for key in ("text", "annotation", "name"):
        value = fields.get(key)
        if value:
            return str(value)
    return ""


def _notes(summary) -> tuple[list[Note], int]:
    """Notes with a known time, and how many have none (their time depends
    on a part that could not be read)."""
    notes = []
    unplaced = 0
    for record in summary.records:
        if record.time is None or not record.times_known:
            unplaced += 1
            continue
        notes.append(
            Note(
                time=int(record.time),
                kind=str(record.type or ""),
                text="" if record.body_locked else _note_text(record.fields),
                duration=int(record.duration) if record.duration is not None else None,
                channel=str(record.channel) if record.channel is not None else None,
                level=str(record.level or "channel"),
                segment=int(record.segment) if record.segment is not None else None,
                locked=bool(record.body_locked),
            )
        )
    notes.sort(key=lambda n: (n.time, n.channel or "", n.segment or 0))
    return notes, unplaced


def _channels(summary, location: str) -> tuple[list[ChannelInfo], list[PartProblem]]:
    channels: list[ChannelInfo] = []
    problems: list[PartProblem] = []
    for c in summary.channels:
        parts = []
        for s in c.segments:
            if not s.readable or s.start_time is None or s.end_time is None:
                reason = _strip_location(str(s.problem or "could not be read"), location)
                problems.append(PartProblem(str(c.name), int(s.segment), reason))
                continue
            parts.append(
                Part(
                    number=int(s.segment),
                    start=int(s.start_time),
                    end=int(s.end_time),
                    samples=int(s.number_of_samples or 0),
                    blocks=int(s.number_of_blocks or 0),
                    history=_history(s.provenance),
                )
            )
        if not parts or c.start_time is None or c.end_time is None:
            continue  # nothing of it could be read: only its problems are shown
        channels.append(
            ChannelInfo(
                name=str(c.name),
                sampling_frequency=float(c.sampling_frequency or 0.0),
                units=str(c.units_description or ""),
                samples=int(c.number_of_samples or sum(p.samples for p in parts)),
                start=int(c.start_time),
                end=int(c.end_time),
                parts=tuple(parts),
                bytes=int(getattr(c, "bytes", 0) or 0),
            )
        )
    # A channel without one complete part. If mef3io also listed its parts
    # as unreadable, those rows already say it: one entry per problem.
    listed = {p.channel for p in problems}
    for name in dict.fromkeys(str(n) for n in summary.incomplete_channels):
        if name not in listed:
            problems.append(PartProblem(name, None, "no complete part (files missing)"))
    return channels, problems


def _access(summary) -> str | None:
    """How far the password opened the recording; None = not at all."""
    encryption = summary.encryption
    if not encryption.get("encrypted"):
        return ACCESS_OPEN
    access = encryption.get("access")
    if access == "full":
        return ACCESS_FULL
    if access == "level-1":
        return ACCESS_BASIC
    return None


def _time_offset(summary) -> tuple[int | None, str]:
    """The recording-time offset (None when it cannot be known) and its
    status (see ``recording.OFFSET_*``)."""
    offset = summary.time_offset
    status = str(offset.get("status") or "unknown")
    if status == "none":
        return 0, OFFSET_NONE
    if status == "applied" and offset.get("value") is not None:
        return int(offset["value"]), status
    if status == "disputed":
        return None, OFFSET_DISPUTED
    return None, OFFSET_LOCKED if status == "locked" else status


def _details(summary, location: str, access: str, legacy: bool) -> RecordingDetails | None:
    channels, part_problems = _channels(summary, location)
    if not channels and (part_problems or summary.problems):
        return None
    identity = {f.name: f for f in summary.identity}

    def values(name: str):
        field = identity.get(name)
        return field.values if field is not None else ()

    names_available = int(summary.encryption.get("section3_locked") or 0) == 0
    patient = PatientDetails(
        **{
            attr: _joined(values(name)) if names_available else None
            for attr, name in _PATIENT_FIELDS.items()
        },
        recording_description=_joined(values("session_description")),
        names_available=names_available,
    )
    rto, status = _time_offset(summary)
    notes, unplaced = _notes(summary)
    record_problems = [
        _strip_location(str(p.get("reason") or ""), location)
        for p in summary.problems
        if p.get("kind") == "records"
    ]
    notes_problem = None
    if record_problems:
        notes_problem = UserMessage(
            NOTES_DAMAGED.title, NOTES_DAMAGED.text, "\n".join(record_problems)
        )
    extra: list[str] = []
    for name in _LOGGED_IDENTITY:
        extra += _texts(values(name))
    facts = tuple(
        IdentityFact(
            name=str(f.name),
            present=f.present,
            values=tuple(_texts(f.values)),
            occurrences=int(f.occurrences or 0),
        )
        for f in summary.identity
    )
    if names_available:
        dst_set: bool | None = summary.dst_start_time is not None or (
            summary.dst_end_time is not None
        )
    else:
        dst_set = None
    return RecordingDetails(
        location=location,
        channels=tuple(channels),
        patient=patient,
        access=access,
        recording_time_offset=rto,
        time_offset_status=status,
        date_recoverable=bool(summary.time_offset.get("date_recoverable")),
        offset_assumed_zero=bool(summary.offset_assumed_zero),
        gmt_offset=int(summary.gmt_offset) if summary.gmt_offset is not None else None,
        notes=tuple(notes),
        note_levels=NOTE_LEVELS,
        notes_problem=notes_problem,
        notes_without_times=unplaced,
        declarations_incomplete=bool(summary.declaration_issues),
        part_problems=tuple(part_problems),
        channel_descriptions=tuple(_texts(values("channel_description"))),
        legacy_password=legacy,
        identity_extra=tuple(dict.fromkeys(extra)),
        time_offset_ambiguous=bool(summary.time_offset.get("ambiguous")),
        first_time=_int_or_none(summary.first_time),
        last_time=_int_or_none(summary.last_time),
        anonymize_blockers=tuple(
            (str(w.code), _strip_location(str(w.message), location))
            for w in summary.warnings
            if w.blocks_anonymize
        ),
        identity=facts,
        dst_set=dst_set,
    )


def _int_or_none(value) -> int | None:
    return int(value) if value is not None else None


def opened_with_legacy_password(location: str, password: str) -> bool:
    """True when ``password`` opens the recording only through the rule
    mef3io used up to 1.2.0 for non-ASCII characters (bnelair/mef3io#84).

    ``mef3io.inspect`` accepts such a password without saying so; only the
    Reader warns (``LegacyPasswordWarning``, whose text names the path, so it
    is caught here and never reaches the log). The two rules agree on ASCII,
    so this costs nothing for an ASCII password."""
    if not password or password.isascii():
        return False
    import mef3io

    legacy_warning = getattr(mef3io, "LegacyPasswordWarning", None)
    if legacy_warning is None:  # pragma: no cover - mef3io before 1.3
        return False
    with library_warnings_caught() as caught:
        try:
            # Lenient, like inspect: a damaged part must not hide the note.
            with mef3io.Reader(location, password=password, warn_declarations=False, strict=False):
                pass
        except Exception:
            return False
    # mef3io 1.4 warnings carry the session as data (MefWarning.path); the
    # text holds "<session>" instead. Only a warning about THIS recording
    # counts.
    target = os.path.normcase(os.path.abspath(location))

    def about_this(w) -> bool:
        path = getattr(w.message, "path", None)
        if not path:
            return True  # an older mef3io: no path to compare
        return os.path.normcase(os.path.abspath(path)) == target

    return any(issubclass(w.category, legacy_warning) and about_this(w) for w in caught)


def inspect_recording(
    location: str,
    password: str = "",
    cancel: CancelToken | None = None,
    progress: Callable[[int, int], object] | None = None,
    detect_legacy: bool = True,
) -> InspectOutcome:
    """Everything the explorer shows about one recording. Never raises for a
    recording that cannot be opened: the reason comes back in the outcome,
    in plain language (only a stop request is raised, as JobCancelled).
    ``progress(done, total)`` gets bytes of details files read.
    ``detect_legacy=False`` skips :func:`opened_with_legacy_password` (a
    second open, for non-ASCII passwords only) where the note is not shown,
    e.g. when a copy is compared with its original."""
    import mef3io

    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    location = os.fspath(location)
    size = size_on_disk(location)
    try:
        # Warnings are caught, not shown: mef3io's warning texts start with
        # the full path, which must not reach the log.
        with stopped_by_library(), library_warnings_caught():
            summary = mef3io.inspect(
                location,
                password,
                include_values=True,
                strict=False,
                cancel=cancel.for_library(),
                progress=progress,
            )
        access = _access(summary)
        if access is None:
            return InspectOutcome(
                location, size_bytes=size, needs_password=True, wrong_password=bool(password)
            )
        cancel.raise_if_cancelled()
        legacy = detect_legacy and opened_with_legacy_password(location, password)
        details = _details(summary, location, access, legacy)
    except JobCancelled:
        raise
    except Exception as exc:
        if is_stop(exc):
            raise JobCancelled() from None
        if mef3io.MefError is not RuntimeError and isinstance(exc, mef3io.PasswordError):
            return InspectOutcome(
                location,
                size_bytes=size,
                needs_password=True,
                wrong_password=bool(password),
                error=describe_error(exc),
            )
        return InspectOutcome(location, size_bytes=size, error=describe_error(exc))
    if details is None:
        reasons = [_strip_location(str(p.get("reason") or ""), location) for p in summary.problems]
        error = UserMessage(NOTHING_READABLE.title, NOTHING_READABLE.text, "\n".join(reasons))
        return InspectOutcome(location, size_bytes=size, error=error)
    return InspectOutcome(location, size_bytes=size, details=details)


def inspect_many(
    requests: list[tuple[str, str]],
    send: Callable[[InspectOutcome | ByteProgress], None] | None = None,
    cancel: CancelToken | None = None,
) -> list[InspectOutcome]:
    """Inspect several recordings, one after another, in one helper process
    (starting a helper per recording would cost more than the opens).

    With ``send``, each outcome is passed to it as soon as it is ready (and
    the returned list is empty), and so is :class:`ByteProgress` for the
    recording being read; without, the outcomes are returned together.
    ``cancel`` stops between recordings and inside mef3io."""
    cancel = cancel or CancelToken()
    outcomes = []
    for location, password in requests:
        cancel.raise_if_cancelled()
        progress = byte_progress(send) if send is not None else None
        outcome = inspect_recording(location, password, cancel=cancel, progress=progress)
        if send is not None:
            send(outcome)  # already delivered: not sent a second time below
        else:
            outcomes.append(outcome)
    return outcomes
