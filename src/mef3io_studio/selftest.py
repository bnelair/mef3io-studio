"""A quick check that this installation works: write a tiny synthetic
recording with mef3io, read it back, and report.

Used by ``mef3io-studio --self-test`` (to smoke-test the packaged apps in CI,
no window shown) and by Help > Check this installation (as a background job).
Everything here is synthetic: a sine wave and a fixed made-up start time.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .model.cancel import CancelToken
from .model.source import LocalRecordingSource

CHANNELS = ("test_1", "test_2")
SAMPLING_FREQUENCY = 256.0
SECONDS = 10
# A fixed synthetic start (2000-01-01 00:00 UTC, in microseconds); not a real
# recording's time.
START_UUTC = 946_684_800_000_000

ReportFn = Callable[[float, float | None, str], None]


@dataclass(frozen=True)
class SelfTestResult:
    channel_count: int
    mef3io_version: str


def run_self_test(
    report: ReportFn | None = None, cancel: CancelToken | None = None
) -> SelfTestResult:
    import mef3io
    import numpy as np

    report = report or (lambda done, total, phase: None)
    cancel = cancel or CancelToken()
    steps = len(CHANNELS) + 1
    n = int(SAMPLING_FREQUENCY * SECONDS)
    signal = 50.0 * np.sin(2 * np.pi * 7.0 * np.arange(n) / SAMPLING_FREQUENCY)

    with tempfile.TemporaryDirectory(prefix="mef3io-studio-selftest-") as tmp:
        session = Path(tmp) / "selftest.mefd"
        with mef3io.Writer(str(session), overwrite=True, units="uV") as writer:
            for i, name in enumerate(CHANNELS):
                cancel.raise_if_cancelled()
                report(i, steps, "Writing a small test recording")
                writer.write(name, signal * (i + 1), START_UUTC, SAMPLING_FREQUENCY)
        report(len(CHANNELS), steps, "Reading it back")
        summary = LocalRecordingSource().summarize(session, cancel=cancel)
        if summary.channel_count != len(CHANNELS):
            raise RuntimeError(
                f"wrote {len(CHANNELS)} channels but read back {summary.channel_count}"
            )
        report(steps, steps, "Reading it back")
    return SelfTestResult(summary.channel_count, str(mef3io.__version__))
