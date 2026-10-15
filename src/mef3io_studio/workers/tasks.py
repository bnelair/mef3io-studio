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
from ..model.errors import HelperCrashed, describe_error
from ..model.problems import CheckReport
from ..model.recording import InspectOutcome, location_key, recording_name
from ..model.scan import ScanResult, find_recordings
from ..model.source import RecordingSource, RecordingSummary
from ..selftest import SelfTestResult, run_self_test
from .jobs import JobContext

log = logging.getLogger(__name__)


def summarize_recording(
    ctx: JobContext, source: RecordingSource, location: str, password: str = ""
) -> RecordingSummary:
    ctx.report(0, None, "Opening the recording")
    if source.isolate:
        summary = ctx.run_isolated(source.summarize, location, password)
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
    for location, password in requests:
        register_identity(recording_name(location), password)

    def got(outcome: InspectOutcome) -> None:
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
                ctx.run_isolated(source.inspect_many, remaining, on_message=got)
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
            source.inspect_many([request], send=got)
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


def check_recording(
    ctx: JobContext, source: RecordingSource, location: str, password: str = ""
) -> CheckReport:
    ctx.report(0, None, "Checking the recording for problems")
    if source.isolate:
        report = ctx.run_isolated(source.check, location, password)
    else:
        report = source.check(location, password)
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
    phase = {"text": "Packing into one file" if plan.packing else "Unpacking into a folder"}
    total = size_bytes or None
    last = {"t": 0.0}
    ctx.report(0, total, phase["text"])

    def poll() -> None:
        now = time.monotonic()
        if now - last["t"] < 0.25 or phase["text"] == "Checking the copy":
            return
        last["t"] = now
        done = archive.written_bytes(plan)
        ctx.report(min(done, total) if total else done, total, phase["text"])

    def step(_name: str) -> None:
        phase["text"] = "Checking the copy"
        ctx.report(0, None, phase["text"])

    ctx.run_isolated(archive.run_in_helper, plan, password, on_message=step, on_poll=poll)
    ctx.check_cancelled()
    result = archive.finish(plan)
    log.info("job %d: %s finished", ctx.job_id, "packing" if plan.packing else "unpacking")
    return result


def self_check(ctx: JobContext) -> SelfTestResult:
    return run_self_test(report=ctx.report, cancel=ctx.cancel_token, run=ctx.run_isolated)
