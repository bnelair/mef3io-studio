"""Top-level functions for helper-process tests (must be importable by the
spawned child, so they live in a module, not inside a test)."""

import os
import time


def native_crash():
    import ctypes

    ctypes.string_at(0)  # read address 0: a segmentation fault / access violation


def hard_exit():
    os._exit(3)


def sleep_then(seconds):
    time.sleep(seconds)
    return "done"


def answer():
    return 42
