"""Save the times of recordings as a table (CSV) for a spreadsheet.

A **user-initiated export**: the user chose the file, after a warning that
it will hold the dates of the recordings (which can identify people). Two
layouts:

* one row per recording (the default): name, start, end, duration,
  channels, rates, time with data, gaps, overlaps with the other recordings
  in the table, and whether its times are verified;
* one row per part of each channel.

Times are written three ways: UTC in ISO 8601 with ``Z`` (the reference),
microseconds since 1970 UTC as whole numbers (exact for scripts; spreadsheet
programs keep only 15 digits of a number, so they round these unless the
column is imported as text), and, when a site time zone is chosen, the
site's local time with its offset (e.g. ``2026-10-15T09:30:00+02:00``). Recordings whose times are
not verified say so in their own columns; their times are never presented
as verified.

Patient details follow the Explorer's rule: included only for recordings
where "Show" was chosen, otherwise ``[hidden]``.

Safe for spreadsheets: UTF-8 with a byte-order mark (so Excel reads accents
correctly), every cell quoted where needed, and text cells that a
spreadsheet would run as a formula (starting with ``=``, ``+``, ``-``,
``@``, a tab or a carriage return, for example from a file name) are
prefixed with ``'``. The file is written under a temporary name in the same
folder, flushed to disk, then renamed over the chosen name, so a stopped or
failed export never leaves half a table behind. It gets the permissions a
newly saved file normally gets (or those of the file it replaces).

Plain Python, no Qt. Runs on a background thread.
"""

from __future__ import annotations

import csv
import os
import secrets
import stat
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, tzinfo

from .cancel import JobCancelled
from .coverage import (
    STATUS_LOCKED,
    STATUS_READING,
    STATUS_READY,
    STATUS_UNREADABLE,
    STATUS_WRONG_PASSWORD,
    CombinedLayout,
    RecordingCoverage,
    RecordingInput,
    combine,
)
from .recording import RecordingDetails
from .summary import HIDDEN
from .timefmt import to_datetime, zone_label

LEVEL_RECORDINGS = "recordings"
LEVEL_PARTS = "parts"

#: The ``status`` column, in everyday words.
STATUS_WORDS = {
    STATUS_READY: "opened",
    STATUS_READING: "still being read",
    STATUS_LOCKED: "password needed",
    STATUS_WRONG_PASSWORD: "wrong password",
    STATUS_UNREADABLE: "could not be opened",
}
LOCKED_DETAIL = "locked"
TOO_MANY = "several, too many to list"

#: Characters that make a spreadsheet treat a cell as a formula.
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")

ReportFn = Callable[[float, float | None, str], None]


@dataclass(frozen=True)
class ExportItem:
    """One recording to export: what the Explorer already knows."""

    recording: RecordingInput
    #: The file name as on disk (with .mefd / .mefd.tar).
    file_name: str
    #: "Show" was chosen for this recording's patient details.
    revealed: bool = False


def safe_cell(value):
    """A cell a spreadsheet will show as text, never run. Numbers are kept
    as numbers (a negative number is not a formula)."""
    if isinstance(value, str) and value.startswith(FORMULA_STARTS):
        return "'" + value
    return value


def iso_utc(us: int | None) -> str:
    """``2000-01-01T00:00:00Z``; microseconds are added only when not zero."""
    if us is None:
        return ""
    moment = to_datetime(us, UTC)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}"
    return text + "Z"


def iso_local(us: int | None, zone: tzinfo) -> str:
    """``2000-01-01T01:00:00+01:00`` in ``zone``."""
    if us is None:
        return ""
    moment = to_datetime(us, zone)
    return moment.isoformat(timespec="microseconds" if moment.microsecond else "seconds")


def seconds_text(us: int | float | None) -> str:
    """Microseconds as seconds, exact to the microsecond, no trailing zeros."""
    if us is None:
        return ""
    whole, frac = divmod(int(us), 1_000_000)
    return f"{whole}.{frac:06d}".rstrip("0").rstrip(".")


def _rates(details: RecordingDetails) -> str:
    return " ".join(f"{r:g}" for r in details.sampling_rates)


def time_note(details: RecordingDetails | None) -> str:
    """Why the times are unverified, or ""."""
    if details is None or details.times_verified:
        return ""
    if details.recording_time_offset is None:
        return "unverified: needs the full-access password to confirm the times"
    return (
        "unverified: uses an older way of hiding the date (a time offset); "
        "older programs stored such times in two ways"
    )


def _patient(item: ExportItem) -> list[str]:
    details = item.recording.details
    if details is None:
        return ["", "", "", "", ""]
    if not item.revealed:
        return [HIDDEN] * 5
    p = details.patient
    if not p.names_available:
        named = [LOCKED_DETAIL] * 4
    else:
        named = [p.first_name or "", p.last_name or "", p.patient_id or "", p.location or ""]
    return [*named, p.recording_description or ""]


PATIENT_COLUMNS = [
    "patient_first_name",
    "patient_last_name",
    "patient_id",
    "recording_location",
    "recording_description",
]


def _time_columns(prefix: str, local: bool) -> list[str]:
    cols = [f"{prefix}_utc"]
    if local:
        cols.append(f"{prefix}_site_time")
    cols.append(f"{prefix}_microseconds_utc")
    return cols


def _time_values(us: int | None, zone: tzinfo, local: bool) -> list:
    values: list = [iso_utc(us)]
    if local:
        values.append(iso_local(us, zone))
    values.append("" if us is None else int(us))
    return values


def recording_table(
    items: Sequence[ExportItem],
    zone: tzinfo = UTC,
    layout: CombinedLayout | None = None,
    pause: Callable[[], None] | None = None,
) -> tuple[list[str], list[list]]:
    """One row per recording. ``pause()`` is called between rows (a task
    thread passes one that lets the window's thread run)."""
    local = zone is not UTC
    name = zone_label(zone)
    if layout is None:
        layout, _ = combine([i.recording for i in items])
    names = {i.recording.key: i.recording.shown_name for i in items}
    header = [
        "recording",
        "file_name",
        "status",
        "times_verified",
        "time_note",
        *(["site_time_zone"] if local else []),
        *_time_columns("start", local),
        *_time_columns("end", local),
        "duration_seconds",
        "channels",
        "sampling_rates_hz",
        "parts",
        "time_with_data_seconds",
        "gaps_between_parts",
        "gaps_between_parts_seconds",
        "parts_with_gaps_inside",
        "overlaps_with",
        "overlap_seconds",
        *PATIENT_COLUMNS,
    ]
    rows = []
    for item in items:
        if pause is not None:
            pause()
        rec = item.recording
        details = rec.details
        coverage: RecordingCoverage | None = layout.row(rec.key)
        ready = details is not None and rec.status == STATUS_READY
        row: list = [
            rec.name,
            item.file_name,
            STATUS_WORDS.get(rec.status, rec.status),
            ("yes" if details.times_verified else "no") if ready else "",
            time_note(details) if ready else "",
        ]
        if local:
            row.append(name)
        if not ready:
            row += [""] * (len(header) - len(row) - len(PATIENT_COLUMNS))
            row += _patient(item) if details is not None else [""] * len(PATIENT_COLUMNS)
            rows.append([safe_cell(v) for v in row])
            continue
        start, end = details.start, details.end
        row += _time_values(start, zone, local)
        row += _time_values(end, zone, local)
        row.append(seconds_text(max(0, end - start)) if start is not None else "")
        row.append(len(details.channels))
        row.append(_rates(details))
        row.append(details.part_count)
        if coverage is not None and coverage.has_data:
            row.append(seconds_text(sum(e - s for s, e in coverage.runs)))
            row.append(len(coverage.gaps))
            row.append(seconds_text(sum(e - s for s, e in coverage.gaps)))
            row.append(coverage.parts_with_gaps_inside)
        else:
            row += ["0", 0, "0", 0]
        others = layout.overlapping.get(rec.key, ())
        shared = layout.overlap_spans_of.get(rec.key, ())
        if others:
            row.append("; ".join(names.get(k, "") for k in others))
        elif shared:  # too many recordings at the same time to list in pairs
            row.append(TOO_MANY)
        else:
            row.append("")
        row.append(seconds_text(sum(e - s for s, e in shared)) if shared else "")
        row += _patient(item)
        rows.append([safe_cell(v) for v in row])
    return header, rows


def part_table(
    items: Sequence[ExportItem],
    zone: tzinfo = UTC,
    pause: Callable[[], None] | None = None,
) -> tuple[list[str], list[list]]:
    """One row per part of each channel. A recording without details gets
    one row saying why. ``pause()`` is called between rows."""
    header, rows, _count = part_rows(items, zone, pause)
    return header, list(rows)


def part_count(items: Sequence[ExportItem]) -> int:
    """How many rows :func:`part_rows` yields."""
    total = 0
    for item in items:
        rec = item.recording
        if rec.details is None or rec.status != STATUS_READY:
            total += 1
        else:
            total += sum(len(c.parts) for c in rec.details.channels)
    return total


def part_rows(
    items: Sequence[ExportItem],
    zone: tzinfo = UTC,
    pause: Callable[[], None] | None = None,
) -> tuple[list[str], Iterator[list], int]:
    """:func:`part_table` as a stream: ``(header, rows, number of rows)``.
    Written row by row, a large table never sits in memory whole (which
    also keeps Python's garbage collector from pausing the app for long)."""
    local = zone is not UTC
    name = zone_label(zone)
    header = [
        "recording",
        "file_name",
        "status",
        "times_verified",
        "time_note",
        *(["site_time_zone"] if local else []),
        "channel",
        "part",
        *_time_columns("start", local),
        *_time_columns("end", local),
        "duration_seconds",
        "samples",
        "sampling_rate_hz",
        "gaps_inside",
        "missing_inside_seconds",
    ]

    def rows() -> Iterator[list]:
        for item in items:
            rec = item.recording
            details = rec.details
            ready = details is not None and rec.status == STATUS_READY
            lead: list = [
                rec.name,
                item.file_name,
                STATUS_WORDS.get(rec.status, rec.status),
                ("yes" if details.times_verified else "no") if ready else "",
                time_note(details) if ready else "",
            ]
            if local:
                lead.append(name)
            if not ready:
                yield [safe_cell(v) for v in lead + [""] * (len(header) - len(lead))]
                continue
            lead = [safe_cell(v) for v in lead]
            for channel in details.channels:
                fs = channel.sampling_frequency
                channel_name = safe_cell(channel.name)
                rate = f"{fs:g}"
                for part in channel.parts:
                    if pause is not None:
                        pause()
                    missing = part.missing_seconds(fs)
                    yield [
                        *lead,
                        channel_name,
                        part.number + 1,
                        *_time_values(part.start, zone, local),
                        *_time_values(part.end, zone, local),
                        seconds_text(max(0, part.end - part.start)),
                        part.samples,
                        rate,
                        "yes" if missing else "no",
                        seconds_text(round(missing * 1e6)) if missing else "0",
                    ]

    return header, rows(), part_count(items)


def _new_file_mode(path: str) -> int:
    """The permissions to give the table: those of the file it replaces,
    else what a newly created file gets here (0o666 less the umask)."""
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        pass
    folder = os.path.dirname(path) or "."
    probe = os.path.join(folder, f".mef3io-studio-{secrets.token_hex(8)}.probe")
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        os.close(fd)
        return stat.S_IMODE(os.stat(probe).st_mode)
    except OSError:
        return 0o644
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def write_csv(
    path: str,
    header: list[str],
    rows: Iterable[Sequence],
    report: ReportFn | None = None,
    should_stop: Callable[[], bool] | None = None,
    pause: Callable[[], None] | None = None,
    total: int | None = None,
) -> int:
    """Write the table to ``path`` safely (see the module docstring).
    Replaces ``path`` only once the whole table is on disk; the caller has
    already asked the user whether replacing it is fine."""
    # A link chosen in the save dialog: write where it points, keep the link.
    path = os.path.realpath(path)
    folder = os.path.dirname(path) or "."
    mode = _new_file_mode(path)
    fd, temp = tempfile.mkstemp(prefix=".mef3io-studio-", suffix=".tmp", dir=folder)
    try:
        try:
            os.chmod(temp, mode)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
            writer.writerow([safe_cell(h) for h in header])
            if total is None and isinstance(rows, Sequence):
                total = len(rows)
            n = 0
            for n, row in enumerate(rows, 1):
                if n % 2000 == 1:  # the first row, then every 2000
                    if should_stop is not None and should_stop():
                        raise JobCancelled
                    if report is not None:
                        report(n, total, "Saving the table")
                if pause is not None:
                    pause()
                writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        return n
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
