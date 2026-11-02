"""A new copy of a recording WITH CHANGES: other details, other channels, a
part of the time, or new passwords. The original is never touched.

ONE call site of ``mef3io.transform_session`` for the whole app: "Edit
details" uses it directly, and "Join or trim" uses it to trim one recording
and to cut the parts of several (``model/join.py`` builds a
:class:`ChangeRequest` for each). mef3io builds the copy under
``<copy>.partial/``, checks it against the original (``verify``) and only
then gives it its final name; a refusal, a failure or a Stop leaves nothing
behind.

* :func:`check_transform` is the dry run (``mef3io.transform.check``): every
  reason not to start, nothing written. The new passwords play no part in
  it and are never passed.
* :func:`transform_copy` is the raw run and raises.
* :func:`make_changes` is the helper-process entry: errors come back as a
  :class:`ChangeResult` with a plain message, a Stop is raised.

Times are never edited: every time is copied as stored. The details that can
be changed are mef3io's ``METADATA_FIELDS``; a text that does not fit its
field is refused, never cut; an empty text clears the field; fields that are
not sent stay as stored (:func:`changed_details` sends only what changed).

Plain Python, no Qt. Nothing here logs anything.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace

from .anonymize import (
    copy_name,
    describe_copy_error,
    is_disk_full,
    is_unreachable,
    refusal_code,
    technical_text,
)
from .cancel import CancelToken, JobCancelled, is_stop, stopped_by_library
from .errors import UserMessage
from .progress import ByteProgress, byte_progress
from .quiet import library_warnings_caught
from .refusals import NOTHING_JOINED, REPORT, join_message_for

#: Refusals that only concern a destination that is not chosen yet (a
#: placeholder is checked with, e.g. for the temporary cut of a join).
PLACEHOLDER_CODES = frozenset({"dst-not-mefd", "dst-exists", "partial-exists", "dst-inside-src"})

#: The part of a recording that holds each detail, for the words only.
SESSION_FIELDS = (
    "subject_name_1",
    "subject_name_2",
    "subject_id",
    "recording_location",
    "session_description",
)
CHANNEL_FIELDS = ("channel_descriptions", "reference_descriptions")


@dataclass(frozen=True)
class ChangeRequest:
    """Everything one copy with changes needs. Plain data (a helper process
    gets it). Not one field of it may be logged: the paths, the details and
    the passwords identify or unlock."""

    source: str = field(repr=False)
    #: The new recording folder (ends in ``.mefd``); its name becomes the
    #: copy's name everywhere.
    destination: str = field(repr=False)
    #: Opens the original (its full-access password), if it has one.
    password: str = field(default="", repr=False)
    #: Keep only these channels / leave these out (not both).
    channels: tuple[str, ...] | None = field(default=None, repr=False)
    exclude_channels: tuple[str, ...] = field(default=(), repr=False)
    #: ``(basic, full-access)`` for the copy, or None for no password. The
    #: original's passwords are never carried over.
    output_password: tuple[str, str] | None = field(default=None, repr=False)
    #: Only what changes, as mef3io's ``metadata=`` takes it (see
    #: :func:`changed_details`).
    metadata: Mapping | None = field(default=None, repr=False)
    #: Keep only ``[start, end)`` (microseconds), ``exact`` = cut the edge
    #: blocks to the window.
    time_range: tuple[int, int] | None = field(default=None, repr=False)
    exact: bool = True

    @property
    def name(self) -> str:
        """The copy's name (its folder name without ``.mefd``)."""
        return copy_name(self.destination)


@dataclass(frozen=True)
class ChangeResult:
    """How one copy went. ``message`` is None when it was saved and checked."""

    destination: str = field(repr=False)
    ok: bool = False
    message: UserMessage | None = None
    channels_kept: tuple[str, ...] = field(default=(), repr=False)
    channels_excluded: tuple[str, ...] = field(default=(), repr=False)
    #: How many stored details differ from the original's (one per detail per
    #: part of the recording).
    details_changed: int = 0
    bytes_written: int = 0
    notes: tuple[str, ...] = ()
    #: The source or the destination could not be reached: worth trying again.
    retryable: bool = False


@dataclass(frozen=True)
class Finding:
    """One reason not to start: plain words, and mef3io's stable code (data,
    not text) so a caller can tell the cases apart."""

    code: str
    message: UserMessage


# --------------------------------------------------------------------------
# The details that can be changed


#: The stored size of each detail in bytes, terminator included
#: (``mef3io.transform.METADATA_FIELDS``, which a test keeps equal): the text
#: itself may take one byte less. A constant, so the window never has to ask
#: the library.
DETAIL_LIMITS: dict[str, int] = {
    "subject_name_1": 128,
    "subject_name_2": 128,
    "subject_id": 128,
    "recording_location": 512,
    "session_description": 2048,
    "channel_descriptions": 2048,
    "reference_descriptions": 2048,
}


def fits(value: str, field_name: str) -> bool:
    """True when ``value`` fits ``field_name`` (UTF-8 bytes, terminator
    included). mef3io refuses what does not fit; it never cuts."""
    return remaining(value, field_name) >= 0


def remaining(value: str, field_name: str) -> int:
    """How many more bytes ``value`` may take (negative = too long)."""
    limit = DETAIL_LIMITS[field_name]
    return limit - 1 - len(value.encode("utf-8"))


def changed_details(
    current: Mapping[str, str | None],
    new: Mapping[str, str | None],
    current_channels: Mapping[str, str | None] | None = None,
    new_channels: Mapping[str, str | None] | None = None,
    new_references: Mapping[str, str | None] | None = None,
) -> dict:
    """mef3io's ``metadata=`` for exactly what the person changed.

    A session-wide detail is sent only when ``new`` differs from ``current``
    (``current`` None = it was never shown, so any text is a change and an
    empty one means "keep"); an empty new text clears a detail that was
    shown. Channel descriptions the same way, per channel. Reference
    descriptions are never shown, so any text given is a change."""
    out: dict = {}
    for name in SESSION_FIELDS:
        if name not in new or new[name] is None:
            continue
        before = current.get(name)
        after = new[name]
        if before is None:
            if after != "":
                out[name] = after
        elif after != before:
            out[name] = after
    channels = _per_channel(current_channels or {}, new_channels or {})
    if channels:
        out["channel_descriptions"] = channels
    references = _per_channel({}, new_references or {})
    if references:
        out["reference_descriptions"] = references
    return out


def _per_channel(current: Mapping, new: Mapping) -> dict[str, str]:
    out: dict[str, str] = {}
    for channel, after in new.items():
        if after is None:
            continue
        before = current.get(channel)
        if before is None:
            if after != "":
                out[channel] = after
        elif after != before:
            out[channel] = after
    return out


def too_long(metadata: Mapping | None) -> list[str]:
    """The names of the details (``DETAIL_LABELS`` keys) that do not fit, so
    the form can say so before anything is checked."""
    if not metadata:
        return []
    found = []
    for name, value in metadata.items():
        values = value.values() if isinstance(value, Mapping) else [value]
        if any(isinstance(v, str) and not fits(v, name) for v in values):
            found.append(name)
    return found


# --------------------------------------------------------------------------
# The calls


def _kwargs(request: ChangeRequest) -> dict:
    return dict(
        channels=list(request.channels) if request.channels else None,
        exclude_channels=list(request.exclude_channels) or None,
        metadata=dict(request.metadata) if request.metadata else None,
        time_range=request.time_range,
        exact=request.exact,
        password=request.password,
    )


def transform_copy(
    request: ChangeRequest,
    progress: Callable[[int, int], object] | None = None,
    cancel: CancelToken | None = None,
):
    """Run ``mef3io.transform_session`` (the ONE place the app does). Raises
    mef3io's refusals and errors; ``progress(done, total)`` is in bytes.
    Meant to run in a helper process, inside the caller's warning catch."""
    import mef3io

    return mef3io.transform_session(
        request.source,
        request.destination,
        output_password=request.output_password,
        progress=progress,
        cancel=cancel.for_library() if cancel is not None else None,
        **_kwargs(request),
    )


def transform_refusals(
    request: ChangeRequest,
    cancel: CancelToken | None = None,
    ignore: Iterable[str] = (),
) -> list:
    """mef3io's refusals for a copy (``mef3io.transform.check``, which writes
    nothing and reads only the details), minus the codes in ``ignore``. The
    new passwords are never passed: they play no part in what is checked."""
    import mef3io

    ignored = frozenset(ignore)
    report = mef3io.transform.check(
        request.source,
        request.destination,
        cancel=cancel.for_library() if cancel is not None else None,
        **_kwargs(request),
    )
    return [r for r in report.refusals if r.code not in ignored]


def _paths_and_stems(request: ChangeRequest) -> tuple[tuple[str, ...], tuple[str, ...]]:
    from .recording import recording_name

    paths = (
        request.source,
        request.destination,
        os.path.dirname(request.source),
        os.path.dirname(request.destination),
    )
    return paths, (recording_name(request.source), request.name)


def describe_transform_error(
    exc: BaseException, paths: tuple[str, ...], stems: tuple[str, ...]
) -> UserMessage:
    """A refusal or failure in plain words: by mef3io's refusal code
    (``JOIN_WORDS`` has every code of transform and merge), else by the kind
    of error. ``paths`` and ``stems`` are stripped from the technical
    details. Shared by "Edit details" and "Join or trim"."""
    detail = technical_text(exc, paths=paths, stems=stems)
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


def describe_change_error(exc: BaseException, request: ChangeRequest) -> UserMessage:
    return describe_transform_error(exc, *_paths_and_stems(request))


def refusal_message(refusal, paths: tuple[str, ...], stems: tuple[str, ...]) -> UserMessage:
    """One refusal of ``transform.check`` in plain words."""
    details = {
        "channel": refusal.channel,
        "field": refusal.field,
        "inspect_code": refusal.inspect_code,
    }
    exc = RuntimeError(f"[{refusal.code}] {refusal.message}")
    detail = technical_text(exc, paths=paths, stems=stems)
    message = join_message_for(refusal.code, details, detail)
    if message is None:
        message = UserMessage(
            "The new recording could not be made", f"{NOTHING_JOINED} {REPORT}", detail
        )
    return message


def check_changes(request: ChangeRequest, cancel: CancelToken | None = None) -> list[Finding]:
    """Everything that would stop the copy, in plain words, before anything
    is written (empty = it would start). Runs in a helper process."""
    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    paths, stems = _paths_and_stems(request)
    with stopped_by_library(), library_warnings_caught():
        refusals = transform_refusals(replace(request, output_password=None), cancel)
    return _unique([Finding(r.code, refusal_message(r, paths, stems)) for r in refusals])


def _unique(findings: list[Finding]) -> list[Finding]:
    """Each reason once (mef3io reports some per channel)."""
    seen: set = set()
    out = []
    for f in findings:
        mark = (f.code, f.message.title, f.message.text)
        if mark not in seen:
            seen.add(mark)
            out.append(f)
    return out


def make_changes(
    request: ChangeRequest,
    send: Callable[[ByteProgress], object] | None = None,
    cancel: CancelToken | None = None,
) -> ChangeResult:
    """Make the copy (in a helper process). A refusal or failure comes back
    as a :class:`ChangeResult` with a plain message (mef3io's exception
    classes do not cross processes); a Stop is raised as
    :class:`JobCancelled`. ``send`` gets byte progress."""
    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    progress = byte_progress(send) if send is not None else None
    try:
        with stopped_by_library(), library_warnings_caught():
            report = transform_copy(request, progress, cancel)
    except JobCancelled:
        raise
    except Exception as exc:
        if is_stop(exc):
            raise JobCancelled() from None
        return ChangeResult(
            request.destination,
            message=describe_change_error(exc, request),
            retryable=is_unreachable(exc),
        )
    return result_of(request, report)


def result_of(request: ChangeRequest, report) -> ChangeResult:
    notes = [str(n) for n in getattr(report, "notes", ()) or ()]
    verification = getattr(report, "verification", None)
    notes += [str(n) for n in getattr(verification, "notes", ()) or ()]
    return ChangeResult(
        request.destination,
        ok=True,
        channels_kept=tuple(report.channels_kept),
        channels_excluded=tuple(report.channels_excluded),
        details_changed=int(getattr(report, "metadata_fields_changed", 0)),
        bytes_written=int(report.bytes_written),
        notes=tuple(notes),
    )
