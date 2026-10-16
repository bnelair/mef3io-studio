"""Job functions: plain functions taking a :class:`JobContext` first, run by
:class:`JobManager`. They call into ``model`` / mef3io and never touch widgets.

Every call that opens a recording goes through the recording source, which
runs it in a helper process while mef3io holds the GIL during opens
(bnelair/mef3io#79). Log lines carry counts and job ids only: never a
location, a name or anything read from a recording.
"""

from __future__ import annotations

import logging
import time

from ..common.privacy import register_identity
from ..model import archive
from ..model.coverage import Stopped, combine
from ..model.errors import HelperCrashed, describe_error
from ..model.export import LEVEL_PARTS, ExportItem, part_rows, recording_table, write_csv
from ..model.problems import CheckReport
from ..model.progress import ByteProgress
from ..model.recording import InspectOutcome, location_key, recording_name
from ..model.scan import ScanResult, find_recordings
from ..model.source import RecordingSource, RecordingSummary
from ..selftest import SelfTestResult, run_self_test
from .isolated import STOP_GRACE_SECONDS
from .jobs import JobContext

log = logging.getLogger(__name__)


def summarize_recording(
    ctx: JobContext, source: RecordingSource, location: str, password: str = ""
) -> RecordingSummary:
    ctx.report(0, None, "Opening the recording")
    if source.isolate:
        summary = ctx.run_isolated(
            source.summarize, location, password, stop_grace=STOP_GRACE_SECONDS
        )
    else:
        summary = source.summarize(location, password=password, cancel=ctx.cancel_token)
    if summary.declarations_incomplete:
        # Fixed wording: never the path or the channel names.
        log.info(
            "job %d: the recording leaves some size declarations unset; reading is unaffected",
            ctx.job_id,
        )
    return summary


def _inspect(ctx: JobContext, source: RecordingSource, requests: list[tuple[str, str]]) -> int:
    """Inspect each recording, publishing every outcome as soon as it is
    ready (``JobHandle.published``). Returns how many were read.

    All recordings share one helper process (starting one per recording
    costs more than the opens). If the helper crashes on one recording, that
    recording gets the plain "stopped while reading" message, and a new
    helper carries on with the rest."""
    total = len(requests)
    delivered: list[str] = []
    phase = "Reading recording details"
    ctx.report(0, total, phase)
    # The log filter learns names and passwords here, on this worker thread:
    # registering rebuilds the filter's pattern, which must never stall the
    # window.
    register_identity(
        *(recording_name(location) for location, _password in requests),
        *(password for _location, password in requests),
    )

    def got(outcome: InspectOutcome | ByteProgress) -> None:
        if isinstance(outcome, ByteProgress):
            # How far into the recording being read now (bytes of details).
            ctx.report(len(delivered) + outcome.fraction, total, phase)
            return
        register_identity(outcome.name)
        if outcome.details is not None:
            register_identity(*outcome.details.identity_values())
        delivered.append(outcome.location)
        ctx.publish(outcome)
        ctx.report(len(delivered), total, phase)

    if not requests:
        return 0
    if source.isolate:
        remaining = list(requests)
        empty_crashes = 0
        while remaining:
            try:
                ctx.run_isolated(
                    source.inspect_many,
                    remaining,
                    on_message=got,
                    stop_grace=STOP_GRACE_SECONDS,
                )
                break
            except HelperCrashed as exc:
                done = len(delivered) - (total - len(remaining))
                if done >= len(remaining):
                    break  # every answer arrived; it stopped while exiting
                empty_crashes = empty_crashes + 1 if done == 0 else 0
                crashed, remaining = remaining[done], remaining[done + 1 :]
                log.warning("job %d: the helper stopped on one recording", ctx.job_id)
                got(InspectOutcome(crashed[0], error=describe_error(exc)))
                if empty_crashes >= 2:
                    # Helpers die before reading anything: the installation,
                    # not the recordings. Don't start one per recording.
                    for location, _password in remaining:
                        got(InspectOutcome(location, error=describe_error(exc)))
                    break
    else:
        for request in requests:
            ctx.check_cancelled()
            source.inspect_many([request], send=got, cancel=ctx.cancel_token)
    log.info("job %d: read the details of %d recording(s)", ctx.job_id, len(delivered))
    return len(delivered)


def open_recordings(
    ctx: JobContext, source: RecordingSource, requests: list[tuple[str, str]]
) -> int:
    """Open recordings given as ``(location, password)`` pairs."""
    return _inspect(ctx, source, list(requests))


def scan_and_open(
    ctx: JobContext, source: RecordingSource, root: str, known: frozenset[str] = frozenset()
) -> ScanResult:
    """Find every recording under ``root``, then open the ones whose
    :func:`location_key` is not in ``known`` (already in the list)."""
    ctx.report(0, None, "Looking for recordings")
    found = find_recordings(root, ctx.cancel_token, ctx.report)
    log.info(
        "job %d: found %d recording(s) in %d folder(s), %d folder(s) unreadable",
        ctx.job_id,
        len(found.locations),
        found.folders_searched,
        found.folders_skipped,
    )
    new = [(loc, "") for loc in found.locations if location_key(loc) not in known]
    _inspect(ctx, source, new)
    return found


def _pacer(work: float = 0.002, rest: float = 0.001):
    """A pause function for pure-Python work on a task thread: after every
    ``work`` seconds it sleeps ``rest`` seconds, so the window's thread gets
    the interpreter lock at once instead of waiting for it."""
    last = [time.perf_counter()]

    def pause() -> None:
        now = time.perf_counter()
        if now - last[0] >= work:
            time.sleep(rest)
            last[0] = time.perf_counter()

    return pause


def export_times(ctx: JobContext, items: list[ExportItem], zone, level: str, path: str) -> int:
    """Save the times of recordings as a CSV table at ``path`` (chosen by
    the user). Returns the number of rows. Reads no file: everything comes
    from what the Explorer already knows. Never logs the path."""
    ctx.report(0, None, "Working out the times")
    # Pure-Python work on this thread: pause often so the window never
    # waits for the interpreter lock (see _pacer).
    pause = _pacer()
    if level == LEVEL_PARTS:
        # Streamed: one row at a time from the details to the file.
        header, rows, total = part_rows(items, zone, pause)
    else:
        try:
            layout, _cache = combine(
                [i.recording for i in items], should_stop=lambda: ctx.cancelled, pause=pause
            )
        except Stopped:
            ctx.check_cancelled()
            raise
        header, rows = recording_table(items, zone, layout, pause)
        total = len(rows)
    ctx.check_cancelled()
    written = write_csv(path, header, rows, ctx.report, lambda: ctx.cancelled, pause, total)
    ctx.report(written, written, "Saved")
    log.info("job %d: saved a table of %d row(s)", ctx.job_id, written)
    return written


def check_recording(
    ctx: JobContext, source: RecordingSource, location: str, password: str = ""
) -> CheckReport:
    phase = "Checking the recording for problems"
    ctx.report(0, None, phase)

    def got(progress: ByteProgress) -> None:
        ctx.report(progress.done, progress.total or None, phase)

    if source.isolate:
        report = ctx.run_isolated(
            source.check, location, password, on_message=got, stop_grace=STOP_GRACE_SECONDS
        )
    else:
        report = source.check(location, password, send=got, cancel=ctx.cancel_token)
    log.info(
        "job %d: checked %d part(s), %d problem(s)",
        ctx.job_id,
        report.parts_checked,
        report.problem_count,
    )
    return report


def pack_or_unpack(
    ctx: JobContext,
    location: str,
    destination_folder: str,
    size_bytes: int | None = None,
    password: str = "",
) -> str:
    """Pack a recording folder into one file, or unpack one into a folder,
    always into a new path (see :mod:`..model.archive`). Returns the path of
    the new recording."""
    plan = archive.plan(location, destination_folder)
    archive.check_space(plan, size_bytes)
    ctx.add_cleanup(lambda: archive.remove_temporary(plan))
    phase = "Packing into one file" if plan.packing else "Unpacking into a folder"
    ctx.report(0, None, phase)

    def got(message) -> None:
        if isinstance(message, ByteProgress):  # mef3io's own count, in bytes
            ctx.report(message.done, message.total or None, phase)
        else:  # "checking": the copy is written, now it is compared
            ctx.report(0, None, "Checking the copy")

    # mef3io stops by itself on Stop and removes what it had written; the
    # cleanup above removes anything still under the temporary name.
    ctx.run_isolated(
        archive.run_in_helper, plan, password, on_message=got, stop_grace=STOP_GRACE_SECONDS
    )
    ctx.check_cancelled()
    result = archive.finish(plan)
    log.info("job %d: %s finished", ctx.job_id, "packing" if plan.packing else "unpacking")
    return result


def self_check(ctx: JobContext) -> SelfTestResult:
    return run_self_test(report=ctx.report, cancel=ctx.cancel_token, run=ctx.run_isolated)
