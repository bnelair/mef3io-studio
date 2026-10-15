"""Read what a recording contains, without reading its signal.

A thin adapter over mef3io's public :class:`mef3io.Reader`, shaped like the
planned ``mef3io.inspect()`` (bnelair/mef3io#67) so it can be swapped for it
in one place. What each call reads (pinned by tests/test_explorer_bytes.py,
which counts the bytes the helper process reads):

* ``Reader(path)`` parses every part's details file (16 kB each, fixed size)
  and, for a single-file recording, its table of contents. It does not read
  the signal or the block table.
* ``Reader.info()`` and ``Reader.segments()`` (including each part's history,
  mef3io's provenance region) are answered from what the open already
  parsed: no further reads.
* ``Reader.records(level="all")`` reads the notes files at every level: the
  whole recording, each channel and each part.

Never called here: ``read``/``read_raw`` (the signal) and ``toc`` (the block
table, about 2% of a recording).

Notes and events at all three levels need mef3io 1.2 (bnelair/mef3io#60,
#61, #62). A notes file that cannot be read does not stop the recording
from opening: the reason is kept in ``notes_problem`` and shown instead.

Plain Python, no Qt. Runs in a helper process (see ``workers.isolated``).
"""

from __future__ import annotations

import os
from collections.abc import Callable

from .cancel import CancelToken, JobCancelled
from .errors import describe_error
from .recording import (
    ACCESS_BASIC,
    ACCESS_FULL,
    ACCESS_OPEN,
    ChannelInfo,
    History,
    InspectOutcome,
    Note,
    Part,
    PatientDetails,
    RecordingDetails,
    is_packed,
)

#: Which levels of notes this version reads (see the module docstring).
NOTE_LEVELS = ("session", "channel", "segment")


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


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
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


def _note_text(record: dict) -> str:
    for key in ("text", "annotation", "name"):
        value = record.get(key)
        if value:
            return str(value)
    return ""


def _notes(reader) -> list[Note]:
    notes = []
    for record in reader.records(level="all"):
        duration = record.get("duration")
        channel = record.get("channel")
        segment = record.get("segment")
        notes.append(
            Note(
                time=int(record["time"]),
                kind=str(record.get("type") or ""),
                text=_note_text(record),
                duration=int(duration) if duration is not None else None,
                channel=str(channel) if channel is not None else None,
                level=str(record.get("level") or "channel"),
                segment=int(segment) if segment is not None else None,
            )
        )
    notes.sort(key=lambda n: (n.time, n.channel or "", n.segment or 0))
    return notes


def _details(reader, location: str, password: str, cancel: CancelToken) -> RecordingDetails:
    channels: list[ChannelInfo] = []
    first_info: dict | None = None
    for name in reader.channels:
        cancel.raise_if_cancelled()
        info = reader.info(name)
        if first_info is None:
            first_info = info
        parts = tuple(
            Part(
                number=int(s["segment"]),
                start=int(s["start_time"]),
                end=int(s["end_time"]),
                samples=int(s["number_of_samples"]),
                blocks=int(s.get("number_of_blocks", 0)),
                history=_history(s.get("provenance")),
            )
            for s in reader.segments(name)
        )
        channels.append(
            ChannelInfo(
                name=str(name),
                sampling_frequency=float(info["sampling_frequency"]),
                units=str(info.get("units_description") or ""),
                samples=int(info["number_of_samples"]),
                start=int(info["start_time"]),
                end=int(info["end_time"]),
                parts=parts,
                description=_text(info.get("channel_description")),
            )
        )
    info = first_info or {}
    full = bool(info.get("section3_available", True))
    if not password:
        access = ACCESS_OPEN if full else ACCESS_BASIC
    else:
        access = ACCESS_FULL if full else ACCESS_BASIC
    patient = PatientDetails(
        first_name=_text(info.get("subject_name_1")) if full else None,
        last_name=_text(info.get("subject_name_2")) if full else None,
        patient_id=_text(info.get("subject_id")) if full else None,
        location=_text(info.get("recording_location")) if full else None,
        recording_description=_text(info.get("session_description")),
        names_available=full,
    )
    # Without the full-access password the offset is unknown: mef3io 1.1.4
    # reports 0 then, which is not the same as "known to be 0".
    rto = info.get("recording_time_offset") if full else None
    gmt = info.get("gmt_offset") if full else None
    notes: list[Note] = []
    notes_problem = None
    cancel.raise_if_cancelled()
    try:
        notes = _notes(reader)
    except JobCancelled:
        raise
    except Exception as exc:  # a damaged notes file must not hide the rest
        notes_problem = describe_error(exc)
    return RecordingDetails(
        location=location,
        channels=tuple(channels),
        patient=patient,
        access=access,
        recording_time_offset=int(rto) if rto is not None else None,
        gmt_offset=int(gmt) if gmt is not None else None,
        notes=tuple(notes),
        note_levels=NOTE_LEVELS,
        notes_problem=notes_problem,
        declarations_incomplete=bool(reader.declaration_issues),
    )


def inspect_recording(
    location: str, password: str = "", cancel: CancelToken | None = None
) -> InspectOutcome:
    """Everything the explorer shows about one recording. Never raises for a
    recording that cannot be opened: the reason comes back in the outcome,
    in plain language (only a stop request is raised)."""
    import mef3io

    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    location = os.fspath(location)
    size = size_on_disk(location)
    try:
        # warn_declarations=False: mef3io's warning text starts with the full
        # path, which must not reach the log; the same fact is kept as a flag.
        with mef3io.Reader(location, password=password, warn_declarations=False) as reader:
            details = _details(reader, location, password, cancel)
    except JobCancelled:
        raise
    except Exception as exc:
        if mef3io.MefError is not RuntimeError and isinstance(exc, mef3io.PasswordError):
            return InspectOutcome(
                location,
                size_bytes=size,
                needs_password=True,
                wrong_password=bool(password),
                error=describe_error(exc),
            )
        return InspectOutcome(location, size_bytes=size, error=describe_error(exc))
    return InspectOutcome(location, size_bytes=size, details=details)


def inspect_many(
    requests: list[tuple[str, str]],
    send: Callable[[InspectOutcome], None] | None = None,
) -> list[InspectOutcome]:
    """Inspect several recordings, one after another, in one helper process
    (starting a helper per recording would cost more than the opens).
    With ``send``, each outcome is passed to it as soon as it is ready (and
    the returned list is empty); without, they are returned together."""
    outcomes = []
    for location, password in requests:
        outcome = inspect_recording(location, password)
        if send is not None:
            send(outcome)  # already delivered: not sent a second time below
        else:
            outcomes.append(outcome)
    return outcomes
