import socket
import subprocess
import sys

import pytest

from conftest import NetworkAccessError


@pytest.mark.allow_network_attempt
@pytest.mark.parametrize(
    "attempt",
    [
        lambda: socket.create_connection(("example.org", 80), timeout=1),
        lambda: socket.getaddrinfo("example.org", 443),
        lambda: socket.gethostbyname("example.org"),
        lambda: socket.gethostbyname_ex("example.org"),
        lambda: socket.gethostbyaddr("127.0.0.1"),
    ],
)
def test_guard_blocks_lookups_and_connections(attempt):
    with pytest.raises(NetworkAccessError):
        attempt()


@pytest.mark.allow_network_attempt
@pytest.mark.parametrize("method", ["connect", "connect_ex", "bind", "sendto"])
def test_guard_blocks_socket_methods(method):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(NetworkAccessError):
            if method == "sendto":
                s.sendto(b"x", ("127.0.0.1", 9))
            else:
                getattr(s, method)(("127.0.0.1", 9))
    finally:
        s.close()


@pytest.mark.allow_network_attempt
def test_guard_blocks_the_c_socket_module():
    import _socket

    with pytest.raises(NetworkAccessError):
        _socket.getaddrinfo("example.org", 443)
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        with pytest.raises(NetworkAccessError):
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()


@pytest.mark.allow_network_attempt
def test_guard_blocks_other_programs():
    with pytest.raises(NetworkAccessError):
        subprocess.run(["curl", "https://example.org"], check=False)


def test_guard_also_applies_in_child_pythons():
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 9), timeout=1)\n"
        "except OSError as e:\n"
        "    print('BLOCKED' if 'test guard' in str(e) else 'OTHER', e)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert "BLOCKED" in out.stdout, out.stdout + out.stderr


def test_app_never_loads_qt_network():
    code = (
        "import os, sys\n"
        "os.environ['QT_QPA_PLATFORM']='offscreen'\n"
        "import tempfile\n"
        "from mef3io_studio.app import create_application, build_main_window\n"
        "from mef3io_studio.common.settings import Settings\n"
        "app = create_application()\n"
        "d = tempfile.mkdtemp()\n"
        "w = build_main_window(app, Settings(os.path.join(d, 's.ini')))\n"
        "w.show(); app.processEvents()\n"
        "net = ('requests', 'urllib3', 'httpx')\n"
        "bad = [m for m in sys.modules if 'QtNetwork' in m or m in net]\n"
        "print('BAD', bad)\n"
        "w.jobs.dispose()\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "BAD []" in out.stdout
