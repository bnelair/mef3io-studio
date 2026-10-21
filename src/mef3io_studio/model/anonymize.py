"""Make anonymized copies: what the wizard hands to mef3io, and what comes back.

All the work on the files is mef3io's ``anonymize_session`` (mef3io#70): it
writes the copy under ``<copy>.partial/``, checks it with
``mef3io.anonymize.verify`` and only then gives it its final name; a failure,
a refusal or a stop leaves nothing behind. This module only:

* describes one copy as plain, picklable data (:class:`CopyRequest`), so a
  helper process can run it (:func:`make_copy`);
* checks the new name and the patient code EARLY, with mef3io's own rule
  (:func:`check_names`), so the wizard can show a refusal on the step where
  the name is typed, not at the end;
* reviews where each copy goes (:func:`review_destination`: taken, inside the
  original, free space);
* turns mef3io's refusals into plain words (:func:`describe_copy_error`);
* writes the optional date key (:func:`write_date_key`), the one file that
  maps the copies back to the originals.

Privacy: original dates, shifts, names and paths identify people. Nothing
here logs anything; the date key is written only when the user asks, to the
file the user chose, and never anywhere else.

Plain Python, no Qt.
"""

from __future__ import annotations

import csv
import errno
import io
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from .cancel import CancelToken, JobCancelled, is_stop, stopped_by_library
from .errors import UserMessage, describe_error
from .progress import ByteProgress, byte_progress
from .quiet import library_warnings_caught
from .refusals import CHECK, NOTHING, UNREACHABLE, message_for
from .text import technical

DAY_US = 86_400_000_000
HOUR_US = 3_600_000_000
MINUTE_US = 60_000_000

#: What mef3io can clear (``clear=``): patient names and ID, the recording
#: location, and the free-text descriptions.
CLEAR_PATIENT = "subject"
CLEAR_LOCATION = "location"
CLEAR_DESCRIPTIONS = "descriptions"
CLEAR_ALL = (CLEAR_PATIENT, CLEAR_LOCATION, CLEAR_DESCRIPTIONS)

#: What happens to the text of notes and events (``text=``).
TEXT_DROP = "drop"
TEXT_KEEP = "keep"

#: Characters a recording name cannot hold on every computer.
_BAD_NAME_CHARS = set('<>:"/\\|?*')
#: Names Windows reserves for devices, with or without an extension.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
#: Longest name accepted (mef3io's limit is 255 bytes; folders add a suffix).
MAX_NAME_CHARS = 200


# --------------------------------------------------------------------------
# One copy


@dataclass(frozen=True)
class CopyRequest:
    """Everything mef3io needs for one copy. Plain data (a helper process
    gets it). Not one field of it may be logged: the source path, the
    shift and the passwords identify or unlock."""

    #: The wizard's opaque key for the recording.
    key: str
    source: str = field(repr=False)
    #: The new recording folder (ends in ``.mefd``); its name becomes the
    #: copy's name everywhere.
    destination: str = field(repr=False)
    shift_us: int = field(repr=False)
    pseudonym: str = field(default="", repr=False)
    clear: tuple[str, ...] = CLEAR_ALL
    text: str = TEXT_DROP
    #: Opens the original (its full-access password), if it has one.
    password: str = field(default="", repr=False)
    #: ``(basic, full-access)`` for the copy, or None for no password.
    output_password: tuple[str, str] | None = field(default=None, repr=False)
    exclude_channels: tuple[str, ...] = field(default=(), repr=False)

    @property
    def name(self) -> str:
        """The copy's name (its folder name without ``.mefd``)."""
        return copy_name(self.destination)


@dataclass(frozen=True)
class CopyResult:
    """How one copy went. ``message`` is None when it was saved and checked."""

    key: str
    destination: str = field(repr=False)
    ok: bool = False
    message: UserMessage | None = None
    channels_kept: tuple[str, ...] = field(default=(), repr=False)
    channels_excluded: tuple[str, ...] = field(default=(), repr=False)
    #: The check's notes and mef3io's notes, as mef3io words them (they never
    #: repeat a name): for "Technical details".
    notes: tuple[str, ...] = ()
    bytes_written: int = 0
    #: Note texts the "keep" choice changed (names taken out).
    text_fields_changed: int = 0
    #: The source or the destination could not be reached (a drive or share
    #: that went away): worth trying again later.
    retryable: bool = False
    #: mef3io's report, marked so that its ORIGINAL times (the
    #: re-identification key: ``source_start_us``, ``source_end_us``,
    #: ``source_channel_times``) survive the trip from the helper process.
    #: In memory only: never persisted, logged or put in the journal.
    report: object = field(default=None, repr=False, compare=False)

    @property
    def source_start_us(self) -> int | None:
        return getattr(self.report, "source_start_us", None)

    @property
    def source_end_us(self) -> int | None:
        return getattr(self.report, "source_end_us", None)


@dataclass(frozen=True)
class Leftover:
    """An unfinished copy (``<copy>.partial``) still there after a stop or
    a crash although the app tried to remove it: the user must be told."""

    key: str
    partial: str = field(repr=False)


def copy_name(destination: str) -> str:
    base = os.path.basename(os.path.normpath(destination))
    return base[: -len(".mefd")] if base.lower().endswith(".mefd") else base


def destination_for(folder: str, name: str) -> str:
    return os.path.join(folder, f"{name}.mefd")


def _kwargs(request: CopyRequest) -> dict:
    return dict(
        pseudonym=request.pseudonym or None,
        clear=tuple(request.clear),
        text=request.text,
        password=request.password,
        output_password=request.output_password,
        exclude_channels=list(request.exclude_channels) or None,
    )


def _notes_of(report) -> tuple[str, ...]:
    notes = [str(n) for n in getattr(report, "notes", ()) or ()]
    verification = getattr(report, "verification", None)
    notes += [str(n) for n in getattr(verification, "notes", ()) or ()]
    return tuple(notes)


def make_copy(
    request: CopyRequest,
    send: Callable[[ByteProgress], object] | None = None,
    cancel: CancelToken | None = None,
) -> CopyResult:
    """Run mef3io's ``anonymize_session`` for one request (in a helper
    process). A refusal or failure comes back as a :class:`CopyResult` with
    a plain message (mef3io's exception classes do not cross processes); a
    stop is raised as :class:`JobCancelled`. ``send`` gets byte progress
    (the copy and its check)."""
    import mef3io

    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    progress = byte_progress(send) if send is not None else None
    try:
        with stopped_by_library(), library_warnings_caught():
            report = mef3io.anonymize_session(
                request.source,
                request.destination,
                int(request.shift_us),
                progress=progress,
                cancel=cancel.for_library(),
                **_kwargs(request),
            )
    except JobCancelled:
        raise
    except Exception as exc:
        if is_stop(exc):
            raise JobCancelled() from None
        return CopyResult(
            request.key,
            request.destination,
            ok=False,
            message=describe_copy_error(exc, request),
            retryable=is_unreachable(exc),
        )
    return CopyResult(
        request.key,
        request.destination,
        ok=True,
        channels_kept=tuple(report.channels_kept),
        channels_excluded=tuple(report.channels_excluded),
        notes=_notes_of(report),
        bytes_written=int(report.bytes_written),
        text_fields_changed=int(report.record_text_fields_changed),
        # Explicitly, and only here: the report crosses back to the app's
        # process WITH the original times (mef3io drops them from a pickle
        # otherwise). The app keeps them in memory for the summary and the
        # date key, never anywhere else.
        report=report.with_original_times_in_pickle(),
    )


#: The app's names for its per-user folders (as in common.logging_setup;
#: model/ imports nothing outside model/).
APP_NAME = "mef3io-studio"
APP_AUTHOR = "BNEL"

#: Where development builds before mef3io 1.4 made their name-check
#: folders (``namecheck-*``); swept once at start, then never used again.
OLD_SCRATCH_DIR_ENV = "MEF3IO_STUDIO_SCRATCH_DIR"


def sweep_old_name_check_folders() -> int:
    """Remove the throw-away folders older development builds used to check
    names (before mef3io 1.4's ``check()``). Returns how many."""
    root = os.environ.get(OLD_SCRATCH_DIR_ENV)
    if not root:
        try:
            from platformdirs import user_cache_dir
        except Exception:  # pragma: no cover - bundled
            return 0
        root = os.path.join(user_cache_dir(APP_NAME, APP_AUTHOR), "scratch")
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if name.startswith("namecheck-"):
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)
            removed += 1
    try:
        os.rmdir(root)
    except OSError:
        pass
    return removed


#: Path refusals that only concern the placeholder the name step checks
#: with (the destination is chosen later, on the review step).
_PLACEHOLDER_CODES = frozenset({"dst-not-mefd", "dst-exists", "partial-exists", "dst-inside-src"})


def check_copy(
    request: CopyRequest, cancel: CancelToken | None = None, placeholder: bool = False
) -> UserMessage | None:
    """mef3io's verdict on a copy, before anything is written
    (``mef3io.anonymize.check``, which writes nothing): None when it would
    start copying, else the first refusal in plain words. ``placeholder``:
    the destination folder is not chosen yet, so refusals about it are left
    out (only the copy's name matters there). The copy's new passwords are
    never passed: they play no part in what is checked here."""
    import mef3io

    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    kwargs = _kwargs(request)
    kwargs["output_password"] = None
    with stopped_by_library(), library_warnings_caught():
        report = mef3io.anonymize.check(
            request.source,
            request.destination,
            int(request.shift_us),
            cancel=cancel.for_library(),
            **kwargs,
        )
    for refusal in report.refusals:
        if placeholder and refusal.code in _PLACEHOLDER_CODES:
            continue
        details = {
            "channel": refusal.channel,
            "field": refusal.field,
            "inspect_code": refusal.inspect_code,
        }
        detail = technical_text(_RefusalText(refusal), request)
        message = message_for(refusal.code, details, detail)
        if message is None:
            message = UserMessage(
                "The copy could not be made",
                f"{NOTHING} Check the choices in the earlier steps and try again; if it keeps "
                "happening, report it and include the technical details.",
                detail,
            )
        return message
    return None


class _RefusalText(Exception):
    """A ``Refusal`` shaped like the exception it stands for (for the
    technical details)."""

    def __init__(self, refusal):
        super().__init__(refusal.message)
        self.code = refusal.code
        self.kind = refusal.error

    def __str__(self) -> str:
        return f"[{self.code}] {self.args[0]}"


def check_names(request: CopyRequest, cancel: CancelToken | None = None) -> UserMessage | None:
    """The name step's check: everything mef3io would refuse, except where
    the copy goes (not chosen yet). The copy's name in ``request`` sits in a
    folder that does not exist; nothing is written."""
    probe = replace(
        request,
        destination=os.path.join(
            tempfile.gettempdir(), ".mef3io-studio-not-a-folder", f"{request.name}.mefd"
        ),
    )
    return check_copy(probe, cancel, placeholder=True)


def check_names_many(
    requests: list[CopyRequest], cancel: CancelToken | None = None
) -> dict[str, UserMessage | None]:
    """:func:`check_names` for every request, in one helper process."""
    cancel = cancel or CancelToken()
    return {r.key: check_names(r, cancel) for r in requests}


def check_copies(
    requests: list[CopyRequest], cancel: CancelToken | None = None
) -> dict[str, UserMessage | None]:
    """:func:`check_copy` for every request (where they really go), in one
    helper process: the batch's up-front check."""
    cancel = cancel or CancelToken()
    return {r.key: check_copy(r, cancel) for r in requests}


def password_may_not_work_elsewhere(password: str) -> bool:
    """True when other MEF programs may not open a recording protected with
    ``password`` (mef3io's own rule: ``mef3io.password_interop_issue``)."""
    import mef3io

    return mef3io.password_interop_issue(password) is not None


# --------------------------------------------------------------------------
# Names and destinations


def name_problem(name: str) -> str | None:
    """Why a name cannot be a recording's folder name on every computer
    (plain words), or None."""
    if not name.strip():
        return "Type a name."
    if name != name.strip() or name.endswith("."):
        return "A name cannot start or end with a space, or end with a dot."
    if any(c in _BAD_NAME_CHARS or ord(c) < 32 for c in name):
        return 'A name cannot contain any of these characters: < > : " / \\ | ? *'
    if name in (".", ".."):
        return "Choose another name."
    if name.split(".")[0].strip().upper() in _RESERVED_NAMES:
        return f'"{name}" is a name Windows keeps for itself. Choose another name.'

    if len(name) > MAX_NAME_CHARS:
        return f"A name can have at most {MAX_NAME_CHARS} characters."
    return None


@dataclass(frozen=True)
class DestinationReview:
    """What the review step says about where one copy goes."""

    destination: str = field(repr=False)
    exists: bool = False
    #: A ``<copy>.partial`` folder from an unfinished earlier run is there.
    unfinished_copy_in_the_way: bool = False
    inside_source: bool = False
    estimated_bytes: int | None = None
    free_bytes: int | None = None

    @property
    def enough_space(self) -> bool:
        if self.estimated_bytes is None or self.free_bytes is None:
            return True
        return self.free_bytes >= self.estimated_bytes

    @property
    def blocked(self) -> bool:
        return (
            self.exists
            or self.unfinished_copy_in_the_way
            or self.inside_source
            or not self.enough_space
        )


def _inside(path: str, folder: str) -> bool:
    a = os.path.normcase(os.path.realpath(path))
    b = os.path.normcase(os.path.realpath(folder))
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def estimate_bytes(channel_bytes: dict[str, int], kept: tuple[str, ...]) -> int:
    """About what the copy takes: the kept channels' files, plus room for
    the notes files (mef3io copies the compressed signal byte for byte)."""
    total = sum(int(channel_bytes.get(n, 0)) for n in kept)
    return int(total * 1.02) + 4 * 1024 * 1024


def review_destination(
    source: str, destination: str, estimated: int | None, *, free: int | None = None
) -> DestinationReview:
    """Look at where a copy goes (``stat`` only, nothing is read or written)."""
    folder = os.path.dirname(destination) or "."
    if free is None:
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            free = None
    return DestinationReview(
        destination=destination,
        exists=os.path.lexists(destination),
        unfinished_copy_in_the_way=os.path.lexists(destination + ".partial"),
        inside_source=_inside(destination, source) or _inside(source, destination),
        estimated_bytes=estimated,
        free_bytes=free,
    )


def remove_unfinished(destination: str) -> bool:
    """Remove ``<copy>.partial`` if a run that was ended before mef3io could
    tidy up left it (only ever called for a destination this run created:
    the review refuses to start when one is already there). Returns True
    when nothing is left there, False when it could not be removed."""
    partial = destination + ".partial"
    if os.path.isdir(partial) and not os.path.islink(partial):
        shutil.rmtree(partial, ignore_errors=True)
    return not os.path.lexists(partial)


def inside_any_recording(path: str, recordings: list[str] | tuple[str, ...] = ()) -> bool:
    """True when ``path`` is inside (or is) one of ``recordings``, or inside
    any folder named like a recording (``.mefd``): a copy there would mix
    into another recording."""
    if any(_inside(path, r) or _inside(r, path) for r in recordings):
        return True
    for part in (os.path.normpath(os.path.abspath(path)), os.path.realpath(path)):
        pieces = part.replace("\\", "/").split("/")
        if any(p.lower().endswith((".mefd", ".mefd.tar")) for p in pieces[:-1]):
            return True
    return False


def date_key_too_close(path: str, places: list[str]) -> bool:
    """True when the date key would be saved inside the output folder, a
    copy or a recording: anyone given those would get the key too."""
    folder = os.path.dirname(os.path.abspath(path))
    return any(p and _inside(folder, p) for p in places) or inside_any_recording(path)


# --------------------------------------------------------------------------
# Plain words


#: Plain words for what mef3io's inspect marks as stopping a copy (its
#: warning codes, for the Recordings step).
BLOCKER_WORDS = {
    technical("video-channels"): "It has video channels, which the app cannot copy.",
    technical("unreadable-segments"): "Some parts of it could not be read.",
    technical("incomplete-segments"): "Some parts of it are incomplete: files are missing.",
    technical("unread-record-files"): "Some of its notes and events could not be read.",
    technical("unreadable-records"): "Some of its notes and events could not be read.",
    technical("locked"): "It needs its full-access password.",
    technical("offset-assumed-zero"): "It needs its full-access password.",
    technical("time-offset-disputed"): (
        "Its parts disagree about how the real dates are hidden in it, so its times are unclear."
    ),
    technical("ambiguous-time-convention"): (
        "Some of its times are stored in an old way that cannot be read without doubt."
    ),
    technical("times-unresolved"): "Some of its times could not be worked out.",
}


def blocker_words(code: str) -> str:
    return BLOCKER_WORDS.get(code, "Something in it stops a safe copy (see the technical details).")


def _strip(text: str, *paths: str, stems: tuple[str, ...] = ()) -> str:
    # Longest first, so a folder never leaves part of a longer path behind.
    for path in sorted(paths, key=len, reverse=True):
        if not path:
            continue
        for variant in {path, path.replace("\\", "/"), os.path.basename(os.path.normpath(path))}:
            if variant:
                text = text.replace(variant, "<recording>")
    # The recording's own name is in its file names ("JaneDoe.rdat") and,
    # for a single-file recording, in the folder inside it: as a word.
    for stem in sorted({s for s in stems if s}, key=len, reverse=True):
        text = re.sub(rf"(?<![^\W_]){re.escape(stem)}(?![^\W_])", "<name>", text)
    return text


def technical_text(
    exc: BaseException,
    request: CopyRequest | None = None,
    *,
    paths: tuple[str, ...] = (),
    stems: tuple[str, ...] = (),
) -> str:
    """mef3io's own wording, for "Technical details" only: without paths
    and without the recording's name (also inside file names and a single
    file's inner folder). ``paths`` and ``stems`` add more of both (the
    joiner has several recordings). The task layer then passes it through
    the log filter's ``scrub()`` (dates, times, raw microsecond times,
    identities it knows) before the UI gets it: ``workers.tasks.scrubbed``."""
    from .recording import recording_name

    if request is not None:
        paths = (
            *paths,
            request.source,
            request.destination,
            os.path.dirname(request.source),
            os.path.dirname(request.destination),
        )
        stems = (*stems, recording_name(request.source), request.name)
    text = _strip(f"{type(exc).__name__}: {exc}", *paths, stems=stems)
    return technical(collapse_placeholders(text))


_PLACEHOLDERS = re.compile(r"<(?:path|recording|name)>(?:[/\\]?<(?:path|recording|name)>)+")


def collapse_placeholders(text: str) -> str:
    """One ``<path>`` for a run of removed path pieces next to each other
    ("<path><recording>/<name>" becomes "<path>")."""
    return _PLACEHOLDERS.sub("<path>", text)


def refusal_code(exc: BaseException) -> str | None:
    """mef3io's stable refusal code of an exception (mef3io 1.4: ``.code``),
    or None for anything that is not a refusal."""
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else None


def _library_classes() -> dict:
    """mef3io's error classes by name (each named literally: the app may only
    use mef3io's public names)."""
    try:
        import mef3io
    except Exception:  # pragma: no cover - the app always bundles mef3io
        return {}
    found = {
        "PasswordError": getattr(mef3io, "PasswordError", None),
        "FormatError": getattr(mef3io, "FormatError", None),
        "CrcError": getattr(mef3io, "CrcError", None),
        "WriteConflictError": getattr(mef3io, "WriteConflictError", None),
        "IoError": getattr(mef3io, "IoError", None),
    }
    # Without the C++ backend these alias RuntimeError: never match on that.
    return {k: v for k, v in found.items() if isinstance(v, type) and v is not RuntimeError}


def _is(exc: BaseException, *names: str) -> bool:
    classes = _library_classes()
    return any(n in classes and isinstance(exc, classes[n]) for n in names)


def is_library_io_error(exc: BaseException) -> bool:
    return _is(exc, "IoError")


#: "No space left" and "over the disk quota": a full drive, not a share that
#: went away. Never retried as if it were; the user is told space is needed.
FULL_ERRNOS = frozenset(
    e for e in (getattr(errno, "ENOSPC", None), getattr(errno, "EDQUOT", None)) if e
)


def is_disk_full(exc: BaseException) -> bool:
    if getattr(exc, "errno", None) in FULL_ERRNOS:
        return True
    text = str(exc).lower()
    return "no space left" in text or "disk quota" in text or "disk is full" in text


def is_unreachable(exc: BaseException) -> bool:
    """The source or the destination could not be read or written (not a
    refusal about the recording or the choices): a drive or share that went
    away may work again later. A full drive is not: see :func:`is_disk_full`."""
    if is_disk_full(exc):
        return False
    code = refusal_code(exc)
    if code is not None:
        return code in UNREACHABLE
    if isinstance(exc, (FileExistsError, PermissionError, IsADirectoryError)):
        return False
    return isinstance(exc, OSError) or is_library_io_error(exc)


def describe_copy_error(exc: BaseException, request: CopyRequest | None = None) -> UserMessage:
    """mef3io's refusal or failure in plain words: what happened, what it
    means for the data (nothing was written; the original is untouched), and
    what to do next. By the refusal's stable code (every code of
    ``mef3io.anonymize.REFUSAL_CODES`` has words in ``model.refusals``);
    without one, by the kind of error. mef3io's own text goes into
    ``detail``, without paths or names (:func:`technical_text`)."""
    detail = technical_text(exc, request)
    if is_disk_full(exc):
        return UserMessage(
            "The destination or the work folder is full",
            "There was not enough free space to finish the copy. The unfinished copy was "
            "removed and the original was not changed. Free some space (or choose another "
            "folder, or another work folder in Settings), then try again.",
            detail,
        )
    code = refusal_code(exc)
    if code is not None:
        message = message_for(code, getattr(exc, "details", None) or {}, detail)
        if message is not None:
            return message
    if _is(exc, "PasswordError"):
        return UserMessage(
            "This recording needs its full-access password",
            f"The copy needs the full-access password of the original. {NOTHING} Open the "
            "recording in Explore with its full-access password, then try again.",
            detail,
        )
    if _is(exc, "FormatError", "CrcError"):
        return UserMessage(
            "Part of the recording is damaged",
            "Part of the recording is damaged, so it cannot be copied safely; nothing was "
            f"written and the original was not changed. {CHECK}",
            detail,
        )
    if _is(exc, "WriteConflictError"):
        return UserMessage(
            "This recording cannot be copied safely",
            f"It cannot be copied without risking a wrong date in the copy. {NOTHING} {CHECK}",
            detail,
        )
    plain_os = isinstance(exc, OSError) and not isinstance(
        exc, (PermissionError, FileNotFoundError)
    )
    if (_is(exc, "IoError") or plain_os) and getattr(exc, "errno", None) != errno.ENOSPC:
        return UserMessage(
            "The copy could not be written to the chosen folder",
            "The app could not write to the chosen folder. Anything it had started was "
            "removed, and the original was not changed. Check that the folder still exists, "
            "that you may write there and that the drive is connected, then try again.",
            detail,
        )
    if isinstance(exc, (ValueError, TypeError)):
        return UserMessage(
            "The copy could not be made",
            f"{NOTHING} Check the choices in the earlier steps and try again; if it keeps "
            "happening, report it and include the technical details.",
            detail,
        )
    message = describe_error(exc)
    return UserMessage(message.title, message.text, detail)


def shift_words(shift_us: int, time_of_day: str = "", keep_weekday: bool = False) -> str:
    """The shift in plain words: "Moved 1,886 days later; time of day kept"."""
    sign = "later" if shift_us >= 0 else "earlier"
    rest = abs(int(shift_us))
    days, rest = divmod(rest, DAY_US)
    hours, rest = divmod(rest, HOUR_US)
    minutes = rest // MINUTE_US
    parts = [f"{days:,} day{'s' if days != 1 else ''}"]
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    text = f"Moved {' '.join(parts) if len(parts) == 1 else ', '.join(parts)} {sign}"
    extra = []
    if time_of_day == "keep_local":
        extra.append("local time of day kept")
    elif time_of_day == "keep_utc":
        extra.append("time of day (UTC) kept")
    elif time_of_day == "set":
        extra.append("time of day changed too")
    if keep_weekday:
        extra.append("weekday kept")
    return text + ("; " + "; ".join(extra) if extra else "")


# --------------------------------------------------------------------------
# The date key


@dataclass(frozen=True)
class DateKeyRow:
    """One line of the date key: how a copy maps back to its original."""

    original_name: str
    copy_name: str
    original_start: int
    original_end: int
    shift_us: int


DATE_KEY_COLUMNS = (
    "original recording",
    "anonymized copy",
    "original start (UTC)",
    "original end (UTC)",
    "new start (UTC)",
    "new end (UTC)",
    "moved by (microseconds)",
)


def _iso(us: int) -> str:
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(us))
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


def date_key_text(rows: list[DateKeyRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(DATE_KEY_COLUMNS)
    for r in rows:
        writer.writerow(
            [
                r.original_name,
                r.copy_name,
                _iso(r.original_start),
                _iso(r.original_end),
                _iso(r.original_start + r.shift_us),
                _iso(r.original_end + r.shift_us),
                str(int(r.shift_us)),
            ]
        )
    return buffer.getvalue()


def write_date_key(path: str, rows: list[DateKeyRow]) -> None:
    """Write the date key to ``path`` (chosen by the user): a temporary file
    next to it, then renamed, so a half-written key never exists under the
    chosen name. Never logged, never kept anywhere else."""
    folder = os.path.dirname(os.path.abspath(path)) or "."
    temporary = os.path.join(folder, f".mef3io-studio-{secrets.token_hex(8)}.tmp")
    try:
        # Readable by its owner only (POSIX; Windows ignores the mode bits).
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(date_key_text(rows))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
