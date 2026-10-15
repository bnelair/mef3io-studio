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


def write_test_recording(session: Path, cancel: CancelToken | None = None, report=None) -> None:
    """The synthetic recording: two sine-wave channels."""
    import mef3io
    import numpy as np

    report = report or (lambda done, total, phase: None)
    cancel = cancel or CancelToken()
    n = int(SAMPLING_FREQUENCY * SECONDS)
    signal = 50.0 * np.sin(2 * np.pi * 7.0 * np.arange(n) / SAMPLING_FREQUENCY)
    with mef3io.Writer(str(session), overwrite=True, units="uV") as writer:
        for i, name in enumerate(CHANNELS):
            cancel.raise_if_cancelled()
            report(i, len(CHANNELS) + 1, "Writing a small test recording")
            writer.write(name, signal * (i + 1), START_UUTC, SAMPLING_FREQUENCY)


def check_time_zones() -> None:
    """The time-zone database is bundled (Windows has none of its own)."""
    from importlib.resources import files

    from .model.timefmt import format_time, resolve_zone, zone_label

    # zoneinfo prefers the system database on Linux and macOS, so check the
    # bundled one itself (it is the only one on Windows).
    if not files("tzdata").joinpath("zoneinfo", "Europe", "Prague").is_file():
        raise RuntimeError("the bundled time-zone database is missing")
    zone = resolve_zone("Europe/Prague")
    if zone_label(zone) != "Europe/Prague" or not format_time(START_UUTC, zone).endswith("+01:00"):
        raise RuntimeError("the time-zone database is missing")


def run_self_test(
    report: ReportFn | None = None,
    cancel: CancelToken | None = None,
    run: Callable[..., object] | None = None,
) -> SelfTestResult:
    """``run(fn, *args)`` executes the read-back; by default in a helper
    process exactly as the app opens recordings, so the packaged app's
    helper-process start-up is tested too."""
    import mef3io

    from .workers.isolated import run_isolated

    report = report or (lambda done, total, phase: None)
    cancel = cancel or CancelToken()
    if run is None:

        def run(fn, *args):
            return run_isolated(fn, *args, cancel=cancel)

    steps = len(CHANNELS) + 1
    check_time_zones()
    with tempfile.TemporaryDirectory(prefix="mef3io-studio-selftest-") as tmp:
        session = Path(tmp) / "selftest.mefd"
        write_test_recording(session, cancel, report)
        report(len(CHANNELS), steps, "Reading it back")
        summary = run(LocalRecordingSource().summarize, str(session))
        if summary.channel_count != len(CHANNELS):
            raise RuntimeError(
                f"wrote {len(CHANNELS)} channels but read back {summary.channel_count}"
            )
        report(steps, steps, "Reading it back")
    return SelfTestResult(summary.channel_count, str(mef3io.__version__))
