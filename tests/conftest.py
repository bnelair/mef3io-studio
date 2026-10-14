"""Test setup: headless Qt, an isolated log folder, and a network guard.

The network guard makes ANY socket connection or DNS lookup during the suite
raise and fail the test that tried it: the app must make no network
connections (issue #2). It patches Python's socket layer, which is what
mef3io, numpy and the app would use; the app also never imports QtNetwork
(tests/test_no_network.py checks that).
"""

from __future__ import annotations

import os
import socket
import tempfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_LOG_DIR = tempfile.mkdtemp(prefix="mef3io-studio-test-logs-")
os.environ["MEF3IO_STUDIO_LOG_DIR"] = _LOG_DIR


class NetworkAccessError(AssertionError):
    pass


NETWORK_ATTEMPTS: list[str] = []


def _blocked(name):
    def guard(*args, **kwargs):
        NETWORK_ATTEMPTS.append(f"{name}{args[1:] if name.startswith('socket.') else args}")
        raise NetworkAccessError(f"network access attempted during tests: {name}")

    return guard


@pytest.fixture(autouse=True, scope="session")
def no_network():
    mp = pytest.MonkeyPatch()
    mp.setattr(socket.socket, "connect", _blocked("socket.connect"))
    mp.setattr(socket.socket, "connect_ex", _blocked("socket.connect_ex"))
    mp.setattr(socket, "create_connection", _blocked("create_connection"))
    mp.setattr(socket, "getaddrinfo", _blocked("getaddrinfo"))
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
    manager.shutdown()


@pytest.fixture
def window(qtbot, qapp, settings):
    from mef3io_studio.app import build_main_window, create_application

    create_application()
    win = build_main_window(qapp, settings)
    qtbot.addWidget(win)
    yield win
    win.jobs.shutdown()


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
