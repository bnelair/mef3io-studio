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
