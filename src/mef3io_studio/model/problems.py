"""Check a recording for problems with mef3io's validator, and say what was
found in everyday words.

The checks compare what a recording *says* about itself (its stored details)
with its data. ``mef3io.Validator`` only reads; it never changes a file.
Fixing is a separate mef3io call, deliberately not offered here.

The technical wording of every finding is kept for the "Technical details"
panel. It never names the recording's location.

Plain Python, no Qt. Runs in a helper process (see ``workers.isolated``).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from .cancel import CancelToken, is_stop, stopped_by_library
from .quiet import library_warnings_caught
from .text import technical
from .timefmt import format_size

# Check id -> (title, what it means). docs/ui-language.md: no format words.
# The ids are mef3io's (technical, never shown outside "Technical details").
_DAMAGE = "Part of the recording may be damaged. Keep a copy of the original as it is."
_OTHER_PROGRAMS = (
    "This app reads the recording correctly, but some other programs rely on this "
    "and may fail to open it or stop while reading it. It can be fixed without "
    "changing the signal."
)
PLAIN_CHECKS: dict[str, tuple[str, str]] = {
    technical("crc.metadata"): (
        "Some recording details failed the built-in check",
        "The stored details of some parts do not match their check value, so they "
        "may have been changed or damaged. " + _DAMAGE,
    ),
    technical("crc.index"): (
        "The list of where the signal is stored failed the built-in check",
        "Programs use this list to find the signal. It does not match its check "
        "value, so it may be damaged. " + _DAMAGE,
    ),
    technical("index.block-offsets"): (
        "The list of where the signal is stored points past the end of the data",
        "Some of the signal it lists is missing from the disk, for example after a "
        "copy or a recording that was interrupted. " + _DAMAGE,
    ),
    technical("index.data-coverage"): (
        "Some stored signal is not listed",
        "The data holds more signal than the list of where it is stored describes, "
        "for example after a recording that was interrupted. Programs will not "
        "show the unlisted part.",
    ),
    technical("index.entry-counts"): (
        "The list of where the signal is stored is incomplete",
        "Some of its entries do not say how large they are. " + _DAMAGE,
    ),
    technical("index.block-count"): (
        "The stored number of signal pieces is wrong",
        "The details state a different number than the data holds. " + _OTHER_PROGRAMS,
    ),
    technical("index.sample-count"): (
        "The stored number of samples is wrong",
        "The details state a different number than the data holds. " + _OTHER_PROGRAMS,
    ),
    technical("index.start-sample"): (
        "The stored first sample number is wrong",
        "The details state a different first sample than the data holds. "
        "Most programs are not affected.",
    ),
    technical("sizing.block-maxima"): (
        "A stored size is too small",
        "The details understate how large the largest piece of signal is. " + _OTHER_PROGRAMS,
    ),
    technical("sizing.difference-bytes"): (
        "A stored size is missing or too small",
        "Programs use it to set aside memory before reading the signal. " + _OTHER_PROGRAMS,
    ),
    technical("sizing.contiguous"): (
        "Stored sizes do not match the data",
        "The details state the wrong length for the longest stretch without gaps. "
        + _OTHER_PROGRAMS,
    ),
    technical("times.sampling-frequency"): (
        "The sampling rate is not a usable number",
        "Times inside the recording cannot be worked out reliably. " + _DAMAGE,
    ),
    technical("times.segment-bounds"): (
        "Stored start or end times do not match the signal",
        "Some parts say they start or end at a different time than their signal "
        "does. Programs may show the wrong times. It can be fixed without changing "
        "the signal.",
    ),
    technical("times.recording-duration"): (
        "A stored length of time is wrong",
        "Some parts state a length that does not match their signal. Most programs "
        "are not affected. It can be fixed without changing the signal.",
    ),
    technical("times.block-interval"): (
        "A stored time step is missing",
        "Most programs are not affected. It can be fixed without changing the signal.",
    ),
    technical("times.discontinuities"): (
        "The stored number of gaps is wrong",
        "Some programs set aside memory from this number and may stop while reading "
        "the recording. It can be fixed without changing the signal.",
    ),
    technical("header.entry-count"): (
        "A stored count of entries is wrong",
        "Most programs are not affected. It can be fixed without changing the signal.",
    ),
    technical("header.max-entry-size"): (
        "A stored entry size is wrong",
        "This is a small inconsistency that programs normally ignore.",
    ),
    technical("records.encryption"): (
        "Some notes and events are not protected like the recording",
        "Some notes and events are stored without the recording's password "
        "protection, so anyone can read their text. Or they are protected in a "
        "recording that is not, so other programs cannot open them. This cannot be "
        "fixed in place: copy the notes and events out and save them again, with the "
        "recording's basic and full-access passwords if it has them.",
    ),
    technical("units.mixed"): (
        "A channel has different units in different parts",
        "Nothing is damaged and no value was changed, but programs label the whole "
        "channel with the units of its first part. Check that the parts really use the "
        "units you expect. Fixing problems in place does not change this.",
    ),
    technical("records.interrupted-edit"): (
        "The notes and events were being changed when a program stopped",
        "A program was changing this recording's notes and events and was interrupted "
        "(or is still working on it). Until that is settled the notes and events cannot "
        "be read and nothing can be added to the recording; the signal itself can still "
        "be read. If no program is still working on it, it must be rescued before the "
        "recording can be used fully: this app version cannot do that yet. Ask whoever "
        "looks after the recording's files to run mef3io's rescue on it ("
        + technical("python -m mef3io recover --apply <folder>")
        + "; without the apply option it only reports). The rescue either finishes the "
        "change or puts the earlier notes and events back, so the change may turn out "
        "not to have been made. Do not remove or move any file next to the recording "
        "first.",
    ),
    technical("records.edit-leftovers"): (
        "Leftover files from a change of the notes and events",
        "A change of this recording's notes and events left temporary files next to the "
        "notes' own files, and the recording no longer says that a change is under way. "
        "Some notes and events may be missing from view: the earlier copy of their file "
        "may still be among the leftovers. Nothing is deleted by this app. Keep these "
        "files and ask whoever looks after the recording's files to look at them first: "
        "if a notes file is missing, its earlier copy has to be put back by hand. When "
        "the notes' own files are whole, mef3io's rescue removes the leftovers ("
        + technical("python -m mef3io recover --apply <folder>")
        + "). The signal is not affected.",
    ),
}
_UNKNOWN = (
    "Something in the recording's details does not match its data",
    "See the technical details for what was found.",
)

#: Severity -> how serious it is, in words.
SEVERITY_WORDS = {"error": "Problem", "warning": "Worth knowing", "info": "Note"}


@dataclass(frozen=True)
class ProblemGroup:
    severity: str  # "error" | "warning" | "info"
    title: str
    explanation: str
    parts: int  # how many parts of the recording it was found in
    technical: str

    @property
    def severity_word(self) -> str:
        return SEVERITY_WORDS.get(self.severity, "Note")


#: What a check looked at. NONE of the first two reads the signal values
#: themselves (mef3io's validator reads a few bytes of each piece's header and
#: has no check of the values); only reading everything back does.
MODE_QUICK = "quick"
MODE_FULL = "full"
MODE_READ_BACK = "read-back"
MODES = (MODE_QUICK, MODE_FULL, MODE_READ_BACK)

#: How a result names the kind of check it is, once ("No problems found (quick look)").
MODE_WORDS = {
    MODE_QUICK: "quick look",
    MODE_FULL: "full check",
    MODE_READ_BACK: "read everything back",
}

#: What every result of the first two says it did not do (one short sentence).
SIGNAL_NOT_READ = "It did not read the signal values, so damage inside the signal would not show."
QUICK_WORDS = (
    "Quick look: checks the recording's details and the list of where the signal is "
    "kept. It reads a small part of the files."
)
FULL_WORDS = (
    "Full check: checks the same, and also the size stored with every piece of the "
    "signal. It makes one small read for every piece, so it is slower when a recording "
    "has very many pieces."
)
READ_BACK_WORDS = (
    "Read everything back: reads all of the signal and checks every piece of it against "
    "the check value stored with it. This is the only choice that can find damage "
    "inside the signal. It reads about as much as copying the recording, so on a large "
    "recording or a network drive it can take hours."
)
LIMIT_WORDS = (
    "Quick look and Full check do not read the signal values themselves, so damage "
    "inside the signal is not found by them."
)
CHECK_INTRO = (
    "Compares what the recording says about itself with what is stored. Nothing is "
    "changed, and you can stop at any time.\n"
    + QUICK_WORDS
    + "\n"
    + FULL_WORDS
    + "\n"
    + LIMIT_WORDS
    + "\n"
    + READ_BACK_WORDS
)
READ_BACK_DONE = (
    "Every piece of the signal was read back and matched the check value stored with it."
)


@dataclass(frozen=True)
class CheckReport:
    parts_checked: int
    groups: tuple[ProblemGroup, ...] = ()
    #: Parts that could not be checked at all (e.g. damaged details).
    parts_skipped: int = 0
    technical: str = ""
    #: One of ``MODES``.
    mode: str = MODE_FULL

    @property
    def quick(self) -> bool:
        return self.mode == MODE_QUICK

    @property
    def read_back(self) -> bool:
        return self.mode == MODE_READ_BACK

    @property
    def reads_signal(self) -> bool:
        """True only when the signal values were read and checked."""
        return self.mode == MODE_READ_BACK

    @property
    def mode_words(self) -> str:
        return MODE_WORDS.get(self.mode, "")

    @property
    def problem_count(self) -> int:
        return sum(1 for g in self.groups if g.severity == "error")

    @property
    def ok(self) -> bool:
        return self.parts_checked > 0 and not self.problem_count and not self.parts_skipped

    def headline(self) -> str:
        """The result in one line; names the kind of check ONCE."""
        if self.parts_checked == 0:
            return "Nothing could be checked"
        if self.ok and not self.groups:
            text = "No problems found"
        elif self.ok:
            text = "No problems found, but there is something worth knowing"
        else:
            n = self.problem_count
            text = (
                f"{n} problem{'s' if n != 1 else ''} found" if n else "Some parts were not checked"
            )
        return f"{text} ({self.mode_words})" if self.mode_words else text

    def short_status(self) -> str:
        """For the recordings list."""
        if self.parts_checked == 0:
            return "Could not be checked"
        if self.ok:
            return f"No problems ({self.mode_words})"
        n = self.problem_count
        if n:
            return f"{n} problem{'s' if n != 1 else ''}"
        return "Not fully checked"


def _strip_location(text: str, location: str) -> str:
    """Technical text without the recording's location (it can identify)."""
    for variant in {location, location.replace("\\", "/"), os.path.basename(location)}:
        if variant:
            text = text.replace(variant, "<recording>")
    return text


def to_report(report, location: str, mode: str = MODE_FULL) -> CheckReport:
    """Turn a ``mef3io.Report`` into plain-language groups."""
    groups = []
    for check_id, findings in report.by_check().items():
        title, explanation = PLAIN_CHECKS.get(check_id, _UNKNOWN)
        severity = findings[0].severity
        lines = [f"{check_id} [{severity}]"]
        for f in findings:
            where = technical(f"channel {f.channel}, segment {f.segment}")
            if f.field:
                lines.append(f"  {where}: {f.field} = {f.stored}, expected {f.expected}")
            else:
                lines.append(f"  {where}: {f.message}")
        groups.append(
            ProblemGroup(
                severity=severity,
                title=title,
                explanation=explanation,
                parts=len({(f.channel, f.segment) for f in findings}),
                technical=_strip_location("\n".join(lines), location),
            )
        )
    order = {"error": 0, "warning": 1, "info": 2}
    groups.sort(key=lambda g: order.get(g.severity, 3))
    lines = [
        technical(
            f"mef3io validation: {report.segments_checked} segment(s) checked, "
            f"{len(report.checks_run)} check(s) run, {len(report.errors)} error(s), "
            f"{len(report.warnings)} warning(s)"
        )
    ]
    lines += [g.technical for g in groups]
    if report.skipped:
        lines.append(technical(f"skipped {len(report.skipped)} segment(s):"))
        lines += [
            technical(f"  {s.channel} segment {s.segment}: {s.reason}") for s in report.skipped
        ]
    return CheckReport(
        parts_checked=int(report.segments_checked),
        groups=tuple(groups),
        parts_skipped=len(report.skipped),
        technical=_strip_location("\n".join(lines), location),
        mode=mode,
    )


def check_recording(
    location: str,
    password: str = "",
    cancel: CancelToken | None = None,
    progress: Callable[[int, int], object] | None = None,
    quick: bool = False,
    read_back: bool = False,
) -> CheckReport:
    """Check the recording. Reads only. ``cancel`` stops it from inside
    mef3io (as JobCancelled); ``progress(done, total)`` gets the progress.

    Three sizes, none of which changes anything:

    * ``quick``: mef3io's validator with ``exact_difference_bytes=False``: the
      stored details and the lists of where the signal is kept.
    * the default: the same validator with ``exact_difference_bytes=True``,
      which also reads one stored size from the header of every block.
    * ``read_back``: reads every sample back through the reader, which checks
      each block against its stored check value (:func:`read_back_recording`).

    The validator has NO check of the signal values (mef3io 2.1.1,
    core/src/validate.cpp: it reads a few bytes of each block header); only
    ``read_back`` finds damage inside the signal."""
    import mef3io

    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    location = os.fspath(location)
    if read_back:
        return read_back_recording(location, password, cancel=cancel, progress=progress)
    mode = MODE_QUICK if quick else MODE_FULL
    with stopped_by_library(), library_warnings_caught():
        report = mef3io.Validator(
            location, password=password, exact_difference_bytes=not quick
        ).validate(cancel=cancel.for_library(), progress=progress)
    return to_report(report, location, mode)


#: Samples read in one call while reading everything back (about 20 MB).
READ_BACK_WINDOW_SAMPLES = 2_000_000


def read_back_recording(
    location: str,
    password: str = "",
    cancel: CancelToken | None = None,
    progress: Callable[[int, int], object] | None = None,
    window_samples: int = READ_BACK_WINDOW_SAMPLES,
) -> CheckReport:
    """Read every sample of every channel back, in windows (the whole
    recording is never held in memory). mef3io checks each block it decodes
    against the check value stored with it (``CrcError``), so damage inside
    the signal is found, which the validator cannot do. The samples are
    thrown away. ``progress(done, total)`` is in microseconds of recording,
    only the ratio is used. Parts that fail are listed, the rest is read on."""
    import mef3io

    cancel = cancel or CancelToken()
    location = os.fspath(location)
    token = cancel.for_library()
    damaged: list[tuple[str, int, str]] = []
    unreadable: list[tuple[str, int, str]] = []
    parts_read = 0
    with stopped_by_library(), library_warnings_caught():
        reader = mef3io.Reader(location, password=password, strict=False)
        plan: list[tuple[str, int, int, int, int]] = []
        for channel in reader.channels:
            fs = float(reader.info(channel)["sampling_frequency"] or 0.0)
            for number, seg in enumerate(reader.segments(channel), 1):
                if not seg.get("number_of_samples"):
                    continue
                plan.append((channel, number, int(seg["start_time"]), int(seg["end_time"]), fs))
        total = sum(max(1, end - start) for _c, _n, start, end, _f in plan)
        done = 0
        for channel, number, start, end, fs in plan:
            cancel.raise_if_cancelled()
            period = max(1, round(1e6 / fs)) if fs > 0 else 1
            step = max(period, int(window_samples * 1e6 / fs)) if fs > 0 else end - start + 1
            t0, failure = start, None
            while t0 < end + period and failure is None:
                t1 = min(t0 + step, end + period)
                try:
                    reader.read_raw(channel, t0, t1, cancel=token)
                except Exception as exc:  # noqa: BLE001 - classified below
                    if is_stop(exc):
                        raise
                    kind = (
                        damaged
                        if isinstance(exc, (mef3io.CrcError, mef3io.FormatError))
                        else unreadable
                    )
                    failure = (channel, number, f"{type(exc).__name__}: {exc}")
                    kind.append(failure)
                done += min(t1, end + 1) - min(t0, end + 1)
                t0 = t1
                if progress is not None:
                    progress(min(done, total), total)
            parts_read += 1
        for problem in getattr(reader, "problems", None) or ():
            unreadable.append(
                (
                    str(getattr(problem, "channel", "?")),
                    int(getattr(problem, technical("segment"), 0) or 0),
                    str(getattr(problem, "reason", problem)),
                )
            )
    if progress is not None and total:
        progress(total, total)
    groups = []
    for found, title, explanation in (
        (
            damaged,
            "Some of the signal does not match its check value",
            "Pieces of the signal do not match the check value stored with them, so "
            "they may have been changed or damaged. " + _DAMAGE,
        ),
        (
            unreadable,
            "Some of the signal could not be read",
            "Reading these parts failed. They may be damaged or missing from the disk. " + _DAMAGE,
        ),
    ):
        if found:
            lines = [technical("read-back [error]")] + [
                technical(f"  channel {c}, segment {n}: {m}") for c, n, m in found
            ]
            groups.append(
                ProblemGroup(
                    severity="error",
                    title=title,
                    explanation=explanation,
                    parts=len({(c, n) for c, n, _m in found}),
                    technical=_strip_location("\n".join(lines), location),
                )
            )
    text = technical(
        f"mef3io read back: {parts_read} segment(s) read in full, "
        f"{len(damaged)} damaged, {len(unreadable)} unreadable"
    )
    return CheckReport(
        parts_checked=parts_read,
        groups=tuple(groups),
        technical=_strip_location("\n".join([text] + [g.technical for g in groups]), location),
        mode=MODE_READ_BACK,
    )


# --- how much a check reads: an estimate from facts the Explorer already has ----------

#: Bytes of one part's own files that the validator reads: the details file
#: (16 KB), the two 1 KB file headers and the index's own header.
BYTES_PER_PART = 20_000
#: The index is read whole: 56 bytes for every block of the signal.
BYTES_PER_BLOCK_INDEX = 56
#: What the full check adds for every block: it opens the signal file and
#: reads a few bytes of the block's header; the file system hands back a
#: buffer (8 KB measured with mef3io, the same call in 2.1.1 and later).
BYTES_PER_BLOCK_FULL = 8_192


@dataclass(frozen=True)
class CheckEstimate:
    """About how much each choice reads, from the parts and blocks listed in
    the recording's details (no file is read to make it)."""

    parts: int
    blocks: int
    quick_bytes: int
    full_bytes: int
    #: The size of the recording's files; 0 when it is not known.
    read_back_bytes: int


def estimate_check(details) -> CheckEstimate:
    channels = details.channels if details is not None else ()
    parts = sum(len(c.parts) for c in channels)
    blocks = sum(p.blocks for c in channels for p in c.parts)
    quick = parts * BYTES_PER_PART + blocks * BYTES_PER_BLOCK_INDEX
    return CheckEstimate(
        parts=parts,
        blocks=blocks,
        quick_bytes=quick,
        full_bytes=quick + blocks * BYTES_PER_BLOCK_FULL,
        read_back_bytes=sum(c.bytes for c in channels),
    )


def describe_estimate(est: CheckEstimate) -> str:
    """One muted line under the buttons: about how much each choice reads
    for THIS recording."""
    if est.parts == 0:
        return ""
    back = (
        f"read everything back about {format_size(est.read_back_bytes)}"
        if est.read_back_bytes
        else "read everything back about as much as the recording holds"
    )
    return (
        f"For this recording (an estimate): quick look reads about "
        f"{format_size(est.quick_bytes)}; full check about {format_size(est.full_bytes)}, "
        f"in {est.blocks:,} small reads; {back}."
    )


def check_note(quick: bool, read_back: bool, est: CheckEstimate) -> str:
    """The sentence under a running check's bar: what it reads, and that
    Stop ends it."""
    if read_back:
        size = f" (about {format_size(est.read_back_bytes)})" if est.read_back_bytes else ""
        return (
            f"Reads all of the signal back{size} and checks every piece of it. This can "
            "take hours on a large recording. Nothing is changed; Stop ends it."
        )
    if quick:
        return (
            f"Reads the details and the list of where the signal is kept (about "
            f"{format_size(est.quick_bytes)}). {SIGNAL_NOT_READ} "
            "Nothing is changed; Stop ends it."
        )
    return (
        f"Reads the same, and a few bytes of every piece of the signal (about "
        f"{format_size(est.full_bytes)}, in {est.blocks:,} small reads). {SIGNAL_NOT_READ} "
        "Nothing is changed; Stop ends it."
    )


def done_note(report: CheckReport) -> str:
    """What a result says it did not do (or, for reading back, did)."""
    if report.reads_signal:
        return READ_BACK_DONE if report.ok and not report.groups else ""
    return f'{SIGNAL_NOT_READ} "Read everything back" is the check that reads it.'
