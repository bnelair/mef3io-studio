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
    return replace(message, detail=anonymize.collapse_placeholders(scrub(message.detail)))


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
    """How the batch's bundle ended: its final path, or why it is not there.
    ``partial`` = finished on request with only the recordings done
    (``recordings`` of ``total``)."""

    path: str = field(repr=False)
    ok: bool = False
    message: object = None
    recordings: int = 0
    total: int = 0
    partial: bool = False


@dataclass(frozen=True)
class BatchState:
    """Published by the batch task: the journal that can continue it, and
    whether it ended paused, stopped or with recordings left to retry."""

    journal: object = field(repr=False)
    paused: bool = False
    retry: tuple[str, ...] = ()
    bundle_pending: bool = False


#: Consecutive failures to reach the source or destination before the
#: batch pauses (a network drive that went away).
PAUSE_AFTER = 3


def preflight_batch(
    ctx: JobContext,
    batch: pipeline.Batch,
    states: list[str] | None = None,
    output_full_password: str = "",
    journal=None,
) -> dict:
    """Everything that can be checked before a long batch starts, so it does
    not fail after hours: every original is still there and opens with ITS
    password (full access), the work folder is usable, nothing at the
    destination is in the way, there is room, and mef3io accepts every name.
    On a resume (``states``), only the recordings still to do are checked,
    a finished copy is opened with the copies' new full-access password, and
    finals that already exist for unfinished recordings are listed.

    Returns ``{"problems": [plain words, "Recording N of M: ..."], "names":
    {key: None or UserMessage}, "originals": {key: (name, start, end)},
    "existing": [index]}``. Never a name or path in the problems."""
    total = len(batch.items)
    states = states or [journal_.WAITING] * total
    todo = [(n, i) for n, i in enumerate(batch.items) if states[n] == journal_.WAITING]
    requests = [i.request for _n, i in todo]
    _register_copy_identity([i.request for i in batch.items])
    ctx.report(0, None, "Checking everything before starting")
    problems: list[str] = []

    def label(n: int) -> str:
        return f"Recording {n + 1} of {total}"

    for n, item in todo:
        if not os.path.exists(item.request.source):
            problems.append(
                f"{label(n)} is no longer where it was. Check that the drive is connected."
            )
    if batch.output != pipeline.FOLDERS:
        problem = pipeline.work_folder_problem(
            batch.work_folder, [i.request.source for i in batch.items]
        )
        if problem:
            problems.append(problem)
    pending = replace(batch, items=tuple(i for _n, i in todo))
    if journal is None:
        # A new batch: nothing may be in the way. (On a resume, results
        # already there are the user's choice: keep them or make them again.)
        problems += pipeline.destination_problems(pending)
    problems += pipeline.space_problems(
        pending,
        pipeline.free_bytes(batch.work_folder) if batch.work_folder else None,
        pipeline.free_bytes(batch.destination),
        same_volume=pipeline.same_volume(batch.work_folder, batch.destination),
    )
    ctx.check_cancelled()
    if journal is not None:
        # The journal proves these are the app's own: unfinished copies,
        # temporary archives and work copies of the recordings still to do.
        tidy_unfinished(journal)
    # Open every original with its own password (all of them: the finished
    # ones' dates are needed again for the date key), and (on a resume) one
    # finished copy with the copies' new password.
    sources = [(i.request.source, i.request.password) for i in batch.items]
    done_outputs = [
        i.final
        for n, i in enumerate(batch.items)
        if states[n] == journal_.DONE and batch.output != pipeline.BUNDLE
    ]
    outputs = [(done_outputs[0], output_full_password)] if done_outputs else []
    probe = ctx.run_isolated(pipeline.probe, sources, outputs, stop_grace=STOP_GRACE_SECONDS)
    originals = {}
    finished_originals = {}
    waiting = {n for n, _i in todo}
    for n, (item, found) in enumerate(zip(batch.items, probe["sources"], strict=True)):
        if found.get("problem"):
            if n in waiting:
                problems.append(f"{label(n)}: {found['problem']}")
        elif n in waiting:
            originals[item.request.key] = (found["name"], found["start"], found["end"])
        else:
            finished_originals[item.request.key] = (found["name"], found["start"], found["end"])
    if probe["outputs"] and not probe["outputs"][0]:
        problems.append(
            "The full-access password typed for the copies does not open the copies already "
            "made. Type the same passwords as when the batch started."
        )
    # mef3io's own pre-flight (it writes nothing): the copy where it really
    # goes on a new batch; on a resume, everything but the destination
    # (results already there are the user's choice, asked separately).
    names = ctx.run_isolated(
        anonymize.check_names_many if journal is not None else anonymize.check_copies,
        [replace(r, output_password=None) for r in requests],
        stop_grace=STOP_GRACE_SECONDS,
    )
    names = {k: scrubbed(v) for k, v in names.items()}
    existing = []
    if batch.output != pipeline.BUNDLE:
        existing = [n for n, i in todo if os.path.lexists(i.final)]
    if journal is not None:
        # A resume: refusals are reported by position, never by name.
        position = {i.request.key: n for n, i in todo}
        for key, message in list(names.items()):
            if message is not None:
                problems.append(f"{label(position[key])}: {message.title}. {message.text}")
                names[key] = None
    log.info(
        "job %d: checked %d of a batch of %d before starting: %d problem(s), %d name(s) refused",
        ctx.job_id,
        len(todo),
        total,
        len(problems),
        sum(1 for v in names.values() if v is not None),
    )
    return {
        "problems": problems,
        "names": names,
        "originals": originals,
        "finished_originals": finished_originals,
        "existing": existing,
    }


def tidy_unfinished(journal) -> None:
    """Before a batch continues: remove the app's own unfinished files of
    the recordings still to do (``.partial`` folders, temporary archives,
    work copies). Never a final result."""
    for n, item in enumerate(journal.payload.get("items", [])):
        if journal.states[n] != journal_.WAITING:
            continue
        if item.get("work"):
            pipeline.remove_work_copy(item["work"])
        else:
            anonymize.remove_unfinished(item["final"])
        for path in item.get("temporaries", []):
            pipeline.remove_temporary_file(path)
            journal_.forget_temporary(path)


def _fail(ctx, item, message, retryable: bool = False) -> anonymize.CopyResult:
    return anonymize.CopyResult(
        item.request.key, item.final, ok=False, message=scrubbed(message), retryable=retryable
    )


EARLIER_FAILURE = UserMessage(
    "Not made in the earlier run",
    "This recording failed before the batch was interrupted, so it was not made. The "
    "original was not changed. Start a new batch for it after checking why.",
)

#: Called with (path) just before an archive's bytes are checked (tests use
#: it to damage what was written). None in the app.
before_archive_check = None


def run_batch(
    ctx: JobContext,
    batch: pipeline.Batch,
    journal: journal_.Journal | None = None,
) -> list:
    """Run the whole batch, unattended (see ``model.pipeline``): per
    recording, anonymize, then archive or add to the bundle, check every
    byte, delete the work copy. A failure ends that recording only; a
    failure to reach the source or the destination leaves it to retry, and
    after :data:`PAUSE_AFTER` in a row the batch pauses. Stop ends the
    current step cleanly. The journal records each finished recording, so a
    crash can be continued; it is deleted only when nothing is left to do
    (with one archive for the batch, a stopped or paused batch keeps its
    unfinished archive and its journal, so it can be completed). The
    computer is kept awake meanwhile."""
    requests = [i.request for i in batch.items]
    _register_copy_identity(requests)
    total = len(batch.items)
    if journal is None:
        journal = journal_.Journal.new(total, pipeline.to_payload(batch))
    results: list = []
    writer = None
    if batch.output == pipeline.BUNDLE:
        writer = _bundle_writer(batch, journal)
    output_password = next(
        (i.request.output_password for i in batch.items if i.request.output_password), None
    )
    check_password = output_password[1] if output_password else ""
    kept: set[str] = set()
    retry: list[str] = []
    in_a_row = 0
    paused = False

    def publish(result) -> None:
        results.append(result)
        ctx.publish(result)

    def ended(paused_: bool = False) -> None:
        pending = any(s == journal_.WAITING for s in journal.states)
        ctx.publish(
            BatchState(
                journal,
                paused=paused_,
                retry=tuple(retry),
                bundle_pending=writer is not None and (pending or paused_),
            )
        )

    with awake.KeepAwake(enabled=batch.keep_awake):
        try:
            for n, item in enumerate(batch.items):
                state = journal.states[n]
                if state == journal_.DONE:
                    unconfirmed = journal.payload["items"][n].get("unconfirmed", False)
                    publish(
                        anonymize.CopyResult(
                            item.request.key,
                            item.final,
                            ok=True,
                            notes=(UNCONFIRMED if unconfirmed else EARLIER,),
                        )
                    )
                    continue
                if state == journal_.FAILED:
                    publish(_fail(ctx, item, EARLIER_FAILURE))
                    continue
                if paused:
                    publish(_fail(ctx, item, PAUSED, retryable=True))
                    retry.append(item.request.key)
                    continue
                ctx.check_cancelled()
                result = _one(ctx, batch, item, n, total, writer, journal, check_password, kept)
                publish(result)
                if getattr(result, "retryable", False):
                    retry.append(item.request.key)
                    in_a_row += 1
                    if in_a_row >= PAUSE_AFTER:
                        paused = True
                        log.warning(
                            "job %d: paused after %d failures to reach the files",
                            ctx.job_id,
                            in_a_row,
                        )
                else:
                    in_a_row = 0
            if writer is not None and not paused and not retry:
                publish(_finish_bundle(ctx, writer, total))
        except BaseException as exc:
            stop = isinstance(exc, JobCancelled) or is_stop(exc)
            if stop and writer is not None and writer.written:
                # Stopped: the unfinished batch archive and the journal stay,
                # so Continue can complete it, or the user can finish it with
                # what is done. Never half an archive under the final name.
                ended()
            elif stop:
                if writer is not None:
                    writer.discard()
                journal.delete()
            raise
    if any(s == journal_.WAITING for s in journal.states):
        ended(paused)  # something is left to retry: keep the journal
    else:
        journal.delete()
        ctx.publish(BatchState(None))
    saved = sum(1 for r in results if getattr(r, "ok", False))
    log.info("job %d: batch of %d finished, %d result(s) saved", ctx.job_id, total, saved)
    return results


EARLIER = "made earlier"
UNCONFIRMED = "made before the interruption, not confirmed"
PAUSED = UserMessage(
    "Waiting: the batch was paused",
    "The batch paused because the recordings or the destination could not be reached "
    "several times in a row. Nothing of this recording was written. Check that the drive "
    "or network share is available, then choose Retry the failed ones.",
)


def _bundle_writer(batch: pipeline.Batch, journal) -> bundle.BundleWriter:
    temporary = journal.payload.get("bundle_temporary", "")
    offset = int(journal.payload.get("bundle_offset", 0))
    if temporary and os.path.exists(temporary):
        writer = bundle.BundleWriter(batch.bundle, temporary=temporary, offset=offset)
        writer.written = bundle_members_so_far(writer)
    else:
        writer = bundle.BundleWriter(batch.bundle)
        journal_.note_temporary(writer.temporary)
        journal.payload["bundle_temporary"] = writer.temporary
        journal.payload["bundle_offset"] = 0
        journal.save()
    return writer


def bundle_members_so_far(writer: bundle.BundleWriter) -> list:
    """What an interrupted bundle already holds (up to its last checked
    recording), read back from its headers."""
    if writer.offset <= 0:
        return []
    return writer._read_members(0, writer.offset)


def _finish_bundle(ctx, writer: bundle.BundleWriter, total: int, partial: bool = False):
    if not writer.written:
        writer.discard()
        return BundleOutcome(writer.target, ok=False, total=total, partial=partial)
    try:
        path = writer.finish()
    except Exception as exc:
        writer.discard()
        log.warning("job %d: the bundle could not be finished (%s)", ctx.job_id, type(exc).__name__)
        return BundleOutcome(
            writer.target, ok=False, message=scrubbed(describe_error(exc)), total=total
        )
    journal_.forget_temporary(writer.temporary)
    roots = {m.name.split("/")[0] for m in writer.written}
    return BundleOutcome(path, ok=True, recordings=len(roots), total=total, partial=partial)


def finish_bundle_now(ctx: JobContext, journal) -> BundleOutcome:
    """ "Finish the archive with what is done": give the unfinished batch
    archive its final name with the recordings already in it (checked), then
    end the batch (journal and leftover work copies removed)."""
    batch = pipeline.from_payload(journal.payload, lambda _s: "", None)
    ctx.report(0, None, "Finishing the batch archive")
    writer = _bundle_writer(batch, journal)
    outcome = _finish_bundle(ctx, writer, len(batch.items), partial=True)
    discard_batch_files(journal)
    journal.delete()
    log.info(
        "job %d: finished a batch archive with %d recording(s)", ctx.job_id, outcome.recordings
    )
    return outcome


def discard_batch_files(journal) -> None:
    """Remove everything an unfinished batch made that is not delivered:
    work copies, temporary archives, an unfinished batch archive and
    ``.partial`` folders of its unfinished recordings. The journal proves
    each is the app's own (opaque names it recorded)."""
    payload = journal.payload
    temporary = payload.get("bundle_temporary", "")
    if temporary and os.path.basename(temporary).startswith(bundle.TEMP_PREFIX):
        try:
            os.unlink(temporary)
        except OSError:
            pass
        journal_.forget_temporary(temporary)
    for n, item in enumerate(payload.get("items", [])):
        if item.get("work"):
            pipeline.remove_work_copy(item["work"])
        for path in item.get("temporaries", []):
            pipeline.remove_temporary_file(path)
            journal_.forget_temporary(path)
        if journal.states[n] == journal_.WAITING and not item.get("work"):
            anonymize.remove_unfinished(item["final"])


def _note_temporary(journal, n: int, path: str) -> None:
    """Journal a temporary file at the destination before it is made, so a
    crash can never leave one the app does not know about (also in the
    app's sealed list of temporaries, swept at start)."""
    journal_.note_temporary(path)
    item = journal.payload["items"][n]
    item.setdefault("temporaries", [])
    if path not in item["temporaries"]:
        item["temporaries"].append(path)
        journal.save()


def _retryable(exc: BaseException) -> bool:
    """A failure to reach the source or the destination (a network drive
    that went away), which may well work a moment later."""
    if isinstance(exc, (bundle.BundleMismatch, archive.CopyMismatch, archive.TargetExists)):
        return False
    if isinstance(exc, (FileExistsError, PermissionError, IsADirectoryError)):
        return False
    return isinstance(exc, OSError) or anonymize.is_library_io_error(exc)


def _one(ctx, batch, item, n, total, writer, journal, check_password, kept=None):
    """One recording through every step. Returns its CopyResult."""
    kept = kept if kept is not None else set()
    request = item.request
    head = f"Recording {n + 1} of {total}"
    weights = (1.0, 0.0) if batch.output == pipeline.FOLDERS else (0.5, 0.5)

    def report(fraction: float, phase: str) -> None:
        ctx.report(n + min(1.0, max(0.0, fraction)), total, f"{head}: {phase}")

    if item.work:
        pipeline.remove_work_copy(item.work)  # a stale one from a crash
        os.makedirs(item.work, exist_ok=True)
        ctx.add_cleanup(lambda w=item.work: w in kept or pipeline.remove_work_copy(w))
    else:
        destination = request.destination
        if not os.path.lexists(destination + ".partial"):
            ctx.add_cleanup(lambda d=destination: anonymize.remove_unfinished(d))

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
        retry = _retryable(exc)
        if not retry:
            journal.mark(n, journal_.FAILED)
        return _fail(ctx, item, anonymize.describe_copy_error(exc, request), retry)
    if not result.ok:
        if item.work:
            pipeline.remove_work_copy(item.work)
        if not result.retryable:
            journal.mark(n, journal_.FAILED)
        return replace(result, destination=item.final, message=scrubbed(result.message))
    if batch.output == pipeline.FOLDERS:
        journal.mark(n, journal_.DONE)
        return result

    # 2 and 3. Archive (or add to the bundle) and check every byte.
    work_copy = request.destination
    try:
        if batch.output == pipeline.ARCHIVES:
            plan = archive.plan(work_copy, batch.destination)
            _note_temporary(journal, n, plan.temporary)
            ctx.add_cleanup(lambda p=plan: archive.remove_temporary(p))
            report(weights[0], "Reading the checked copy")
            hashes = bundle.hash_folder(work_copy, ctx.cancel_token)

            def packed(message) -> None:
                if isinstance(message, ByteProgress):
                    report(weights[0] + 0.25 * message.fraction, "Making the archive")
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
                if before_archive_check is not None:
                    before_archive_check(plan.temporary)
                report(weights[0] + 0.35, "Checking the archive")
                found = bundle.without_root(
                    bundle.tar_member_hashes(plan.temporary, cancel=ctx.cancel_token)
                )
                if found != hashes:
                    raise bundle.BundleMismatch("the archive's bytes do not match the checked copy")
                archive.finish(plan)
                journal_.forget_temporary(plan.temporary)
            except BaseException:
                archive.remove_temporary(plan)
                if not os.path.lexists(plan.temporary):
                    journal_.forget_temporary(plan.temporary)
                raise
        else:
            size = sum(m.size for m in bundle.members_of(work_copy)) or 1
            done = [0]

            def added(count: int) -> None:
                done[0] += count
                report(
                    weights[0] + weights[1] * done[0] / (2 * size), "Adding to the batch archive"
                )

            report(weights[0], "Adding to the batch archive")
            writer.add(work_copy, ctx.cancel_token, added)
            journal.payload["bundle_offset"] = writer.offset
    except Exception as exc:
        stop = isinstance(exc, JobCancelled) or is_stop(exc)
        if isinstance(exc, bundle.BundleMismatch):
            # The archive does not hold exactly the checked copy: keep the
            # copy (the user is told where), never delete the only good one.
            kept.add(item.work)
            journal.mark(n, journal_.FAILED)
            log.warning("job %d: an archive did not match its checked copy", ctx.job_id)
            return _fail(ctx, item, MISMATCH)
        if not pipeline.remove_work_copy(item.work):
            ctx.publish(anonymize.Leftover(request.key, item.work))
        if stop:
            raise
        retry = _retryable(exc)
        if not retry:
            journal.mark(n, journal_.FAILED)
        log.warning("job %d: an archive step failed (%s)", ctx.job_id, type(exc).__name__)
        return _fail(ctx, item, anonymize.describe_copy_error(exc, request), retry)

    # 4. The archive matches the checked copy byte for byte: the copy goes.
    report(1.0, "Tidying up")
    if not pipeline.remove_work_copy(item.work):
        ctx.publish(anonymize.Leftover(request.key, item.work))
    journal.mark(n, journal_.DONE)
    if batch.output == pipeline.BUNDLE:
        return replace(result, destination=item.final, notes=(*result.notes, IN_BUNDLE))
    return replace(result, destination=item.final)


IN_BUNDLE = "in the batch archive"
MISMATCH = UserMessage(
    "The archive did not match the checked copy",
    "The archive was read back and its bytes differ from the checked anonymized copy, so "
    "it was not kept. The checked copy was KEPT in the work folder (the app offers to "
    "remove it at the next start). The original was not changed. Check the destination "
    "drive, then try again.",
)


def self_check(ctx: JobContext) -> SelfTestResult:
    return run_self_test(report=ctx.report, cancel=ctx.cancel_token, run=ctx.run_isolated)
