"""Top-level functions for helper-process tests (must be importable by the
spawned child, so they live in a module, not inside a test)."""

import os
import time


def native_crash():
    # A native abort (SIGABRT on POSIX). Reading address 0 through ctypes is
    # not enough: on Windows ctypes turns the access violation into OSError.
    os.abort()


def hard_exit():
    os._exit(3)


def sleep_then(seconds):
    time.sleep(seconds)
    return "done"


def answer():
    return 42


def _rchar():
    with open("/proc/self/io") as fh:
        for line in fh:
            if line.startswith("rchar"):
                return int(line.split()[1])
    raise RuntimeError("no rchar")


def measure_inspect(warm_up, location):
    """Bytes this process reads (``/proc/self/io`` rchar, which counts every
    byte read, page cache or not) while the explorer inspects ``location``,
    exactly as the app does. ``warm_up`` is inspected first so that Python
    imports done on first use are not counted."""
    import mef3io

    from mef3io_studio.model.source import LocalRecordingSource

    source = LocalRecordingSource()
    source.inspect_many([(warm_up, "")])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the explorer must not read the signal or the block table")

    # Exact, independent of the byte count: these calls are never made.
    for name in ("read", "read_raw", "toc"):
        setattr(mef3io.Reader, name, forbidden)
    from mef3io_studio.model.privacycheck import build_check

    before = _rchar()
    (outcome,) = source.inspect_many([(location, "")])
    assert outcome.details is not None, outcome.error
    # The Privacy check card is built from the same details: counted too.
    check = build_check(outcome.details)
    read = _rchar() - before
    assert check.items
    return read, len(outcome.details.channels), outcome.details.part_count


def count_to(n, send):
    for i in range(n):
        send(i)
    return "counted"


def write_pid_then_sleep(path):
    with open(path, "w") as fh:
        fh.write(str(os.getpid()))
    time.sleep(60)


def orphan_parent(pid_file):
    """Runs as a separate Python program: starts a helper, then waits to be
    killed (see test_helper_exits_when_the_app_dies)."""
    from mef3io_studio.workers.isolated import run_isolated

    run_isolated(write_pid_then_sleep, pid_file)


class CrashOnSecond:
    """A recording source whose helper dies (natively) on the second
    recording it is given, as a damaged file could make it."""

    isolate = True

    def inspect_many(self, requests, send=None, cancel=None):
        from mef3io_studio.model.source import LocalRecordingSource

        real = LocalRecordingSource()
        for location, password in requests:
            if location.endswith("crash.mefd"):
                os.abort()
            real.inspect_many([(location, password)], send=send, cancel=cancel)
        return []


def stop_cleanly(marker, cancel):
    """Waits for Stop, then stops by itself the way mef3io does (raising
    mef3io.Cancelled) after leaving a mark that it saw the request."""
    import mef3io

    while not cancel.cancelled:
        time.sleep(0.01)
    with open(marker, "w") as fh:
        fh.write("stopped cleanly")
    raise mef3io.Cancelled("operation cancelled")


def ignore_stop(cancel):
    """Never looks at the token: must be ended after the grace period."""
    time.sleep(60)


def gated_make_copy(request, send=None, cancel=None, *, gate_dir="", when="during"):
    """mef3io's anonymize_session, held at a known point until Stop arrives
    (the stop token is set), so a Stop test never races the copy:

    * ``when="during"``: at the first progress report (mef3io is copying),
      then the copy goes on and notices the stop at its next check;
    * ``when="after"``: after mef3io checked and published the copy, then the
      finished result is returned (Stop arrived too late to stop anything).

    ``gate_dir/entered`` is created when the hold starts."""
    import os
    import time

    from mef3io_studio.model import anonymize

    def hold() -> None:
        open(os.path.join(gate_dir, "entered"), "w").close()
        deadline = time.monotonic() + 60
        while not (cancel is not None and cancel.cancelled) and time.monotonic() < deadline:
            time.sleep(0.01)

    held = [False]

    def gated_send(message) -> None:
        if when == "during" and not held[0]:
            held[0] = True
            hold()
        if send is not None:
            send(message)

    result = anonymize.make_copy(request, gated_send, cancel)
    if when == "after":
        hold()
    return result


def make_foreign_folder(request, send=None, cancel=None, *, gate_dir=""):
    """Stands in for the copy: a folder with the copy's name appears (not a
    copy at all), then it waits for Stop."""
    import os
    import time

    os.makedirs(request.destination)
    with open(os.path.join(request.destination, "someone-elses.txt"), "w") as handle:
        handle.write("not a recording")
    open(os.path.join(gate_dir, "entered"), "w").close()
    deadline = time.monotonic() + 60
    while not (cancel is not None and cancel.cancelled) and time.monotonic() < deadline:
        time.sleep(0.01)
    from mef3io_studio.model.cancel import JobCancelled

    raise JobCancelled()
