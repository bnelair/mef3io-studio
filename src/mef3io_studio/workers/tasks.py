"""Job functions: plain functions taking a :class:`JobContext` first, run by
:class:`JobManager`. They call into ``model`` / mef3io and never touch widgets.

Every call that opens a recording goes through the recording source, which
runs it in a helper process while mef3io holds the GIL during opens
(bnelair/mef3io#79). Log lines carry counts and job ids only: never a
location, a name or anything read from a recording.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field, replace

from ..common.privacy import register_identity, scrub
from ..model import anonymize, archive, awake, bundle, pipeline
from ..model import journal as journal_
from ..model.cancel import JobCancelled, is_stop
from ..model.coverage import Stopped, combine
from ..model.errors import HelperCrashed, UserMessage, describe_error
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


def _register_copy_identity(requests: list[anonymize.CopyRequest]) -> None:
    """Everything a copy request carries that identifies or unlocks: the
    original's name and password, the new names and passwords."""
    values: list[str] = []
    for r in requests:
        values += [recording_name(r.source), r.name, r.pseudonym, r.password]
        if r.output_password:
            values += list(r.output_password)
    register_identity(*(v for v in values if v))


def scrubbed(message):
    """A plain message whose technical detail went through the log filter's
    scrub (dates, raw times, known identities), as every detail that came
    back from a copy or a name check does before the UI shows it."""
    if message is None or not getattr(message, "detail", ""):
        return message
    return replace(message, detail=scrub(message.detail))


def check_copy_names(ctx: JobContext, requests: list[anonymize.CopyRequest]) -> dict[str, object]:
    """mef3io's own verdict on each copy's name and pseudonym, before
    anything is written: ``{key: None (accepted) or UserMessage}``. One
    helper process for the whole batch (it reads only details)."""
    _register_copy_identity(requests)
    ctx.report(0, len(requests), "Checking the names")
    verdicts = ctx.run_isolated(
        anonymize.check_names_many, list(requests), stop_grace=STOP_GRACE_SECONDS
    )
    verdicts = {k: scrubbed(v) for k, v in verdicts.items()}
    refused = sum(1 for v in verdicts.values() if v is not None)
    log.info("job %d: checked %d name(s), %d refused", ctx.job_id, len(verdicts), refused)
    return verdicts


def anonymize_recordings(
    ctx: JobContext, requests: list[anonymize.CopyRequest]
) -> list[anonymize.CopyResult]:
    """Make each anonymized copy in turn, in a helper process, with mef3io's
    byte progress. A refusal or failure of one copy is reported for that copy
    and the batch goes on; Stop ends the batch (mef3io removes the unfinished
    copy; the cleanup below catches what a helper ended after the grace
    could leave). Each result is published as soon as it is known."""
    _register_copy_identity(requests)
    total = len(requests)
    results: list[anonymize.CopyResult] = []
    for i, request in enumerate(requests):
        ctx.check_cancelled()
        phase = f"Making anonymized copy {i + 1} of {total}"
        ctx.report(i, total, phase)
        if not os.path.lexists(request.destination + ".partial"):
            destination = request.destination
            ctx.add_cleanup(lambda d=destination: anonymize.remove_unfinished(d))

        def got(message, i=i, phase=phase) -> None:
            if isinstance(message, ByteProgress):
                ctx.report(i + message.fraction, total, phase)

        try:
            result = ctx.run_isolated(
                anonymize.make_copy, request, on_message=got, stop_grace=STOP_GRACE_SECONDS
            )
        except Exception as exc:
            if not (isinstance(exc, JobCancelled) or is_stop(exc)):
                raise_stop = False
            else:
                raise_stop = True
            # Stopped, or the helper itself failed (a crash): make sure the
            # unfinished copy is gone, and say so if it is not.
            if not anonymize.remove_unfinished(request.destination):
                ctx.publish(anonymize.Leftover(request.key, request.destination + ".partial"))
                log.warning("job %d: an unfinished copy could not be removed", ctx.job_id)
            if raise_stop:
                raise
            result = anonymize.CopyResult(
                request.key,
                request.destination,
                ok=False,
                message=anonymize.describe_copy_error(exc, request),
            )
        if result.message is not None:
            result = replace(result, message=scrubbed(result.message))
        results.append(result)
        ctx.publish(result)
        ctx.report(i + 1, total, phase)
    saved = sum(1 for r in results if r.ok)
    log.info("job %d: %d of %d anonymized copies saved and checked", ctx.job_id, saved, total)
    return results


@dataclass(frozen=True)
class BundleOutcome:
    """How the batch's bundle ended: its final path, or why it is not there."""

    path: str = field(repr=False)
    ok: bool = False
    message: object = None
    recordings: int = 0


def preflight_batch(ctx: JobContext, batch: pipeline.Batch) -> dict:
    """Everything that can be checked before a long batch starts, so it does
    not fail after hours: the originals are still there, the work folder is
    usable, nothing at the destination is in the way, there is room at both,
    and mef3io accepts every name. Returns ``{"problems": [plain words],
    "names": {key: None or UserMessage}}``."""
    requests = [i.request for i in batch.items]
    _register_copy_identity(requests)
    ctx.report(0, None, "Checking everything before starting")
    problems: list[str] = []
    for item in batch.items:
        if not os.path.exists(item.request.source):
            problems.append(
                f"{item.original_name or 'A recording'} is no longer where it was. "
                "Check that the drive is connected."
            )
    if batch.output != pipeline.FOLDERS:
        problem = pipeline.work_folder_problem(
            batch.work_folder, [i.request.source for i in batch.items]
        )
        if problem:
            problems.append(problem)
    problems += pipeline.destination_problems(batch)
    problems += pipeline.space_problems(
        batch,
        pipeline.free_bytes(batch.work_folder) if batch.work_folder else None,
        pipeline.free_bytes(batch.destination),
    )
    ctx.check_cancelled()
    names = ctx.run_isolated(
        anonymize.check_names_many,
        [replace(r, output_password=None) for r in requests],
        stop_grace=STOP_GRACE_SECONDS,
    )
    names = {k: scrubbed(v) for k, v in names.items()}
    log.info(
        "job %d: checked a batch of %d before starting: %d problem(s), %d name(s) refused",
        ctx.job_id,
        len(requests),
        len(problems),
        sum(1 for v in names.values() if v is not None),
    )
    return {"problems": problems, "names": names}


def _fail(ctx, item, message) -> anonymize.CopyResult:
    return anonymize.CopyResult(item.request.key, item.final, ok=False, message=scrubbed(message))


def run_batch(
    ctx: JobContext, batch: pipeline.Batch, journal: journal_.Journal | None = None
) -> list:
    """Run the whole batch, unattended (see ``model.pipeline``): per
    recording, anonymize, then archive or add to the bundle, check, delete
    the work copy. A failure ends that recording only. Stop ends the current
    step cleanly: an unfinished copy or archive is removed; a bundle is
    finished with the recordings already in it. The journal records each
    finished recording, so a crash can be continued; it is deleted when the
    batch ends or is stopped. The computer is kept awake meanwhile."""
    requests = [i.request for i in batch.items]
    _register_copy_identity(requests)
    total = len(batch.items)
    if journal is None:
        journal = journal_.Journal.new(total, pipeline.to_payload(batch))
    results: list = []
    writer = None
    if batch.output == pipeline.BUNDLE:
        temporary = journal.payload.get("bundle_temporary", "")
        offset = int(journal.payload.get("bundle_offset", 0))
        if temporary and os.path.exists(temporary):
            writer = bundle.BundleWriter(batch.bundle, temporary=temporary, offset=offset)
            writer.written = bundle_members_so_far(writer)
        else:
            writer = bundle.BundleWriter(batch.bundle)
            journal.payload["bundle_temporary"] = writer.temporary
            journal.payload["bundle_offset"] = 0
            journal.save()
    output_password = next(
        (i.request.output_password for i in batch.items if i.request.output_password), None
    )
    check_password = output_password[1] if output_password else ""

    def publish(result) -> None:
        results.append(result)
        ctx.publish(result)

    with awake.KeepAwake(enabled=batch.keep_awake):
        try:
            for n, item in enumerate(batch.items):
                state = journal.states[n]
                if state == journal_.DONE:
                    publish(
                        anonymize.CopyResult(
                            item.request.key, item.final, ok=True, notes=("made earlier",)
                        )
                    )
                    continue
                if state == journal_.FAILED:
                    publish(_fail(ctx, item, EARLIER_FAILURE))
                    continue
                ctx.check_cancelled()
                publish(_one(ctx, batch, item, n, total, writer, journal, check_password))
            if writer is not None:
                publish(_finish_bundle(ctx, writer, journal))
        except BaseException as exc:
            if writer is not None and (isinstance(exc, JobCancelled) or is_stop(exc)):
                # Stopped: deliver what is complete, never half a bundle.
                ctx.publish(_finish_bundle(ctx, writer, journal))
            if isinstance(exc, JobCancelled) or is_stop(exc):
                journal.delete()
            raise
    journal.delete()
    saved = sum(1 for r in results if getattr(r, "ok", False))
    log.info("job %d: batch of %d finished, %d result(s) saved", ctx.job_id, total, saved)
    return results


EARLIER_FAILURE = UserMessage(
    "Not made in the earlier run",
    "This recording failed before the batch was interrupted, so it was not made. The "
    "original was not changed. Start a new batch for it after checking why.",
)


def bundle_members_so_far(writer: bundle.BundleWriter) -> list:
    """What an interrupted bundle already holds (up to its last checked
    recording), read back from its headers."""
    if writer.offset <= 0:
        return []
    return writer._read_members(0, writer.offset)


def _finish_bundle(ctx, writer: bundle.BundleWriter, journal) -> BundleOutcome:
    if not writer.written:
        writer.discard()
        return BundleOutcome(writer.target, ok=False, recordings=0)
    try:
        path = writer.finish()
    except Exception as exc:
        writer.discard()
        log.warning("job %d: the bundle could not be finished (%s)", ctx.job_id, type(exc).__name__)
        return BundleOutcome(writer.target, ok=False, message=scrubbed(describe_error(exc)))
    roots = {m.name.split("/")[0] for m in writer.written}
    return BundleOutcome(path, ok=True, recordings=len(roots))


def _one(ctx, batch, item, n, total, writer, journal, check_password):
    """One recording through every step. Returns its CopyResult."""
    request = item.request
    head = f"Recording {n + 1} of {total}"
    weights = (1.0, 0.0) if batch.output == pipeline.FOLDERS else (0.6, 0.4)

    def report(fraction: float, phase: str) -> None:
        ctx.report(n + min(1.0, max(0.0, fraction)), total, f"{head}: {phase}")

    if item.work:
        pipeline.remove_work_copy(item.work)  # a stale one from a crash
        os.makedirs(item.work, exist_ok=True)
    else:
        destination = request.destination
        if not os.path.lexists(destination + ".partial"):
            ctx.add_cleanup(lambda d=destination: anonymize.remove_unfinished(d))
    if item.work:
        ctx.add_cleanup(lambda w=item.work: pipeline.remove_work_copy(w))

    # 1. Anonymize (into the work folder, or straight to the destination).
    def got(message) -> None:
        if isinstance(message, ByteProgress):
            report(weights[0] * message.fraction, "Anonymizing")

    report(0, "Anonymizing")
    try:
        result = ctx.run_isolated(
            anonymize.make_copy, request, on_message=got, stop_grace=STOP_GRACE_SECONDS
        )
    except Exception as exc:
        stop = isinstance(exc, JobCancelled) or is_stop(exc)
        if not anonymize.remove_unfinished(request.destination):
            ctx.publish(anonymize.Leftover(request.key, request.destination + ".partial"))
        if item.work and not pipeline.remove_work_copy(item.work):
            ctx.publish(anonymize.Leftover(request.key, item.work))
        if stop:
            raise
        journal.mark(n, journal_.FAILED)
        return _fail(ctx, item, anonymize.describe_copy_error(exc, request))
    if not result.ok:
        if item.work:
            pipeline.remove_work_copy(item.work)
        journal.mark(n, journal_.FAILED)
        return replace(result, destination=item.final, message=scrubbed(result.message))
    if batch.output == pipeline.FOLDERS:
        journal.mark(n, journal_.DONE)
        return result

    # 2 and 3. Archive (or add to the bundle) and check.
    work_copy = request.destination
    try:
        if batch.output == pipeline.ARCHIVES:
            plan = archive.plan(work_copy, batch.destination)
            ctx.add_cleanup(lambda p=plan: archive.remove_temporary(p))

            def packed(message) -> None:
                if isinstance(message, ByteProgress):
                    report(weights[0] + 0.3 * message.fraction, "Making the archive")
                else:
                    report(weights[0] + 0.3, "Checking the archive")

            report(weights[0], "Making the archive")
            try:
                ctx.run_isolated(
                    archive.run_in_helper,
                    plan,
                    check_password,
                    on_message=packed,
                    stop_grace=STOP_GRACE_SECONDS,
                )
                ctx.check_cancelled()
                archive.finish(plan)
            except BaseException:
                archive.remove_temporary(plan)
                raise
        else:
            size = sum(m.size for m in bundle.members_of(work_copy)) or 1
            done = [0]

            def added(count: int) -> None:
                done[0] += count
                report(weights[0] + weights[1] * done[0] / size, "Adding to the bundle")

            report(weights[0], "Adding to the bundle")
            writer.add(work_copy, ctx.cancel_token, added)
            journal.payload["bundle_offset"] = writer.offset
    except Exception as exc:
        stop = isinstance(exc, JobCancelled) or is_stop(exc)
        if not pipeline.remove_work_copy(item.work):
            ctx.publish(anonymize.Leftover(request.key, item.work))
        if stop:
            raise
        journal.mark(n, journal_.FAILED)
        log.warning("job %d: an archive step failed (%s)", ctx.job_id, type(exc).__name__)
        return _fail(ctx, item, describe_error(exc))

    # 4. The archive matches the checked copy: the work copy can go.
    report(1.0, "Tidying up")
    if not pipeline.remove_work_copy(item.work):
        ctx.publish(anonymize.Leftover(request.key, item.work))
    journal.mark(n, journal_.DONE)
    return replace(result, destination=item.final)


def self_check(ctx: JobContext) -> SelfTestResult:
    return run_self_test(report=ctx.report, cancel=ctx.cancel_token, run=ctx.run_isolated)
