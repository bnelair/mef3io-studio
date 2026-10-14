"""Loaded automatically by every Python child process the test suite starts
(tests/conftest.py puts this folder on PYTHONPATH), so helper processes and
subprocesses are under the same no-network rule as the tests themselves."""

import os
import socket


def _blocked(name):
    def guard(*args, **kwargs):
        raise OSError(f"[mef3io-studio test guard] network access attempted in a child: {name}")

    return guard


if os.environ.get("MEF3IO_STUDIO_NETGUARD") == "1":
    for _name in ("connect", "connect_ex", "bind", "sendto", "sendmsg"):
        if hasattr(socket.socket, _name):
            setattr(socket.socket, _name, _blocked(f"socket.{_name}"))
    for _name in (
        "create_connection",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
    ):
        setattr(socket, _name, _blocked(_name))
