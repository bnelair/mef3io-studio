"""Job functions: plain functions taking a :class:`JobContext` first, run by
:class:`JobManager`. They call into ``model`` / mef3io and never touch widgets."""

from __future__ import annotations

import logging

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


def self_check(ctx: JobContext) -> SelfTestResult:
    return run_self_test(report=ctx.report, cancel=ctx.cancel_token, run=ctx.run_isolated)
