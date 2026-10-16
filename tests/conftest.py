"""Test setup: headless Qt, an isolated log folder, and a network guard.

The network guard makes ANY socket connection, bind, send or name lookup
during the suite raise and fail the test that tried it: the app must make no
network connections (issue #2). It patches Python's socket layer (what
mef3io, numpy and the app would use) in this process, and — through
tests/netguard/sitecustomize.py on PYTHONPATH — in every Python child process
too. Starting any program other than this Python interpreter is also refused.
The app never imports QtNetwork (tests/test_no_network.py), and the packaged
Linux app is additionally run under strace in CI.
"""

from __future__ import annotations

import _socket
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_LOG_DIR = tempfile.mkdtemp(prefix="mef3io-studio-test-logs-")
os.environ["MEF3IO_STUDIO_LOG_DIR"] = _LOG_DIR
os.environ["MEF3IO_STUDIO_SCRATCH_DIR"] = os.path.join(_LOG_DIR, "scratch")
os.environ["MEF3IO_STUDIO_CRASH_DIR"] = os.path.join(
    _LOG_DIR, "..", "crash-" + os.path.basename(_LOG_DIR)
)
_NETGUARD_DIR = str(Path(__file__).parent / "netguard")
os.environ["PYTHONPATH"] = os.pathsep.join(
    [_NETGUARD_DIR, *filter(None, [os.environ.get("PYTHONPATH")])]
)
os.environ["MEF3IO_STUDIO_NETGUARD"] = "1"


class NetworkAccessError(AssertionError):
    pass


NETWORK_ATTEMPTS: list[str] = []


def _blocked(name):
    def guard(*args, **kwargs):
        NETWORK_ATTEMPTS.append(name)
        raise NetworkAccessError(f"network access attempted during tests: {name}")

    return guard


_RealPopen = subprocess.Popen


class _GuardedPopen(_RealPopen):
    """Only this Python interpreter may be started (it inherits the guard)."""

    def __init__(self, args, *a, **kw):
        argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        program = os.path.realpath(os.fspath(argv[0])) if argv else ""
        allowed = {os.path.realpath(sys.executable)}
        if program not in allowed:
            NETWORK_ATTEMPTS.append(f"subprocess {argv[:1]}")
            raise NetworkAccessError(f"tests may only start this Python, not {argv[:1]}")
        super().__init__(args, *a, **kw)


def _guarded_raw_socket_class():
    raw = _socket.socket

    class GuardedRawSocket(raw):
        pass

    for name in ("connect", "connect_ex", "bind", "sendto", "sendmsg"):
        if hasattr(raw, name):
            setattr(GuardedRawSocket, name, _blocked(f"_socket.socket.{name}"))
    return GuardedRawSocket


@pytest.fixture(autouse=True, scope="session")
def no_network():
    mp = pytest.MonkeyPatch()
    for name in ("connect", "connect_ex", "bind", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):  # no sendmsg on Windows
            mp.setattr(socket.socket, name, _blocked(f"socket.{name}"))
    for name in (
        "create_connection",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
    ):
        mp.setattr(socket, name, _blocked(name))
    # The C module underneath, for code that bypasses socket.py.
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        mp.setattr(_socket, name, _blocked(f"_socket.{name}"))
    mp.setattr(_socket, "socket", _guarded_raw_socket_class())
    mp.setattr(subprocess, "Popen", _GuardedPopen)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _fail_on_swallowed_network_attempt(request):
    """Catches attempts that code under test caught and ignored."""
    before = len(NETWORK_ATTEMPTS)
    yield
    if request.node.get_closest_marker("allow_network_attempt"):
        del NETWORK_ATTEMPTS[before:]
        return
    attempts = NETWORK_ATTEMPTS[before:]
    assert not attempts, f"network access attempted: {attempts}"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allow_network_attempt: the test deliberately triggers the network guard"
    )


@pytest.fixture
def settings(tmp_path):
    from mef3io_studio.common.settings import Settings

    return Settings(tmp_path / "settings.ini")


@pytest.fixture
def jobs(qapp):
    from mef3io_studio.workers.jobs import JobManager

    manager = JobManager()
    yield manager
    manager.dispose()


@pytest.fixture
def window(qtbot, qapp, settings):
    from mef3io_studio.app import build_main_window, create_application

    create_application()
    win = build_main_window(qapp, settings)
    qtbot.addWidget(win)
    yield win
    win._closing = True
    win.jobs.dispose()


@pytest.fixture
def synthetic_session(tmp_path):
    """A tiny synthetic recording written by mef3io (no real data)."""
    import mef3io
    import numpy as np

    path = tmp_path / "synthetic.mefd"
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        for name in ("a1", "a2", "a3"):
            w.write(name, np.sin(np.arange(2560) / 10.0), 946_684_800_000_000, 256.0)
    return path


@pytest.fixture(scope="session")
def multisegment_session(tmp_path_factory):
    """64 channels x 60 parts: large enough that opening it takes a while
    (the case bnelair/mef3io#79 is about). Synthetic zeros."""
    import mef3io
    import numpy as np

    path = tmp_path_factory.mktemp("big") / "multisegment.mefd"
    with mef3io.Writer(str(path), overwrite=True) as w:
        for seg in range(60):
            for ch in range(64):
                w.write(
                    f"c{ch:02d}",
                    np.zeros(256),
                    946_684_800_000_000 + seg * 10_000_000,
                    256.0,
                    new_segment=True,
                )
    return path
