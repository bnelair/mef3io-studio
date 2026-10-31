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

from .cancel import CancelToken, stopped_by_library
from .quiet import library_warnings_caught
from .text import technical

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


@dataclass(frozen=True)
class CheckReport:
    parts_checked: int
    groups: tuple[ProblemGroup, ...] = ()
    #: Parts that could not be checked at all (e.g. damaged details).
    parts_skipped: int = 0
    technical: str = ""

    @property
    def problem_count(self) -> int:
        return sum(1 for g in self.groups if g.severity == "error")

    @property
    def ok(self) -> bool:
        return self.parts_checked > 0 and not self.problem_count and not self.parts_skipped

    def headline(self) -> str:
        if self.parts_checked == 0:
            return "Nothing could be checked"
        if self.ok and not self.groups:
            return "No problems found"
        if self.ok:
            return "No problems found, but there is something worth knowing"
        n = self.problem_count
        found = f"{n} problem{'s' if n != 1 else ''} found" if n else "Some parts were not checked"
        return found

    def short_status(self) -> str:
        """For the recordings list."""
        if self.parts_checked == 0:
            return "Could not be checked"
        if self.ok:
            return "No problems"
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


def to_report(report, location: str) -> CheckReport:
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
    )


def check_recording(
    location: str,
    password: str = "",
    cancel: CancelToken | None = None,
    progress: Callable[[int, int], object] | None = None,
) -> CheckReport:
    """Run every check mef3io has on the recording. Reads only. ``cancel``
    stops it from inside mef3io (as JobCancelled); ``progress(done, total)``
    gets mef3io's progress, in bytes."""
    import mef3io

    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    location = os.fspath(location)
    with stopped_by_library(), library_warnings_caught():
        report = mef3io.Validator(location, password=password).validate(
            cancel=cancel.for_library(), progress=progress
        )
    return to_report(report, location)
