import socket
import subprocess
import sys

import pytest

from conftest import NetworkAccessError


@pytest.mark.allow_network_attempt
def test_guard_blocks_connections():
    with pytest.raises(NetworkAccessError):
        socket.create_connection(("example.org", 80), timeout=1)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkAccessError):
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()
    with pytest.raises(NetworkAccessError):
        socket.getaddrinfo("example.org", 443)


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
        "w.jobs.shutdown()\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "BAD []" in out.stdout
