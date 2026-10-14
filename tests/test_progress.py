from mef3io_studio.model.cancel import CancelToken, JobCancelled
from mef3io_studio.model.progress import (
    EtaEstimator,
    Progress,
    ProgressThrottle,
    format_duration,
)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_progress_fraction_and_determinate():
    assert Progress(5, 10).fraction == 0.5
    assert Progress(5, None).fraction is None
    assert not Progress(5, 0).determinate
    assert Progress(20, 10).fraction == 1.0


def test_throttle_limits_to_ten_per_second():
    clock = Clock()
    throttle = ProgressThrottle(0.1, clock)
    emitted = 0
    for i in range(10_000):  # 10k reports over 1 simulated second
        clock.t = i / 10_000
        emitted += throttle.should_emit(Progress(i, 20_000, "x"))
    assert 9 <= emitted <= 11


def test_throttle_always_lets_through_phase_change_and_completion():
    clock = Clock()
    throttle = ProgressThrottle(0.1, clock)
    assert throttle.should_emit(Progress(0, 10, "a"))
    assert not throttle.should_emit(Progress(1, 10, "a"))
    assert throttle.should_emit(Progress(1, 10, "b"))
    assert throttle.should_emit(Progress(10, 10, "b"))


def test_eta_only_when_meaningful():
    clock = Clock()
    eta = EtaEstimator(min_elapsed=2.0, min_fraction=0.02, clock=clock)
    clock.t = 1.0
    assert eta.remaining(Progress(50, 100)) is None  # too early
    clock.t = 10.0
    assert eta.remaining(Progress(None, None)) is None  # indeterminate
    assert eta.remaining(Progress(1, 100)) is None  # too little done
    assert eta.remaining(Progress(25, 100)) == 30.0
    assert eta.remaining(Progress(100, 100)) is None  # finished


def test_format_duration_is_plain_language():
    assert format_duration(None) == ""
    assert format_duration(5) == "less than a minute"
    assert format_duration(60) == "about 1 minute"
    assert format_duration(600) == "about 10 minutes"
    assert format_duration(5400) == "about 1.5 hours"


def test_cancel_token():
    token = CancelToken()
    token.raise_if_cancelled()
    token.cancel()
    assert token.cancelled
    try:
        token.raise_if_cancelled()
    except JobCancelled:
        pass
    else:
        raise AssertionError("expected JobCancelled")
