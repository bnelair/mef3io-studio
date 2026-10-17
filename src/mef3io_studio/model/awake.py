"""Keep the computer awake while a long batch runs, and let it sleep after.

* **Windows:** ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``
  on the thread that runs the batch, cleared at the end.
* **macOS:** ``caffeinate -i -w <app pid>``: it also ends by itself if the
  app is gone.
* **Linux:** ``systemd-inhibit --what=sleep:idle … sleep infinity``, stopped
  at the end.

Wherever none of these is available, it does nothing (and says so through
:attr:`KeepAwake.active`). The display may still turn off; only sleep is
held off. Plain Python, no Qt. Never logs anything.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

#: Turn keeping awake off (tests, and anyone who prefers not to).
DISABLE_ENV = "MEF3IO_STUDIO_NO_KEEP_AWAKE"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """``with KeepAwake():`` holds off sleep for the block. Never raises."""

    def __init__(self, reason: str = "Making anonymized copies", enabled: bool = True):
        self.reason = reason
        self.enabled = enabled and not os.environ.get(DISABLE_ENV)
        self._process: subprocess.Popen | None = None
        self._windows = False
        #: How sleep is held off ("" when it is not).
        self.active = ""

    def __enter__(self) -> KeepAwake:
        if not self.enabled:
            return self
        try:
            if sys.platform == "win32":
                import ctypes

                kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
                if kernel.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                    self._windows = True
                    self.active = "windows"
            elif sys.platform == "darwin":
                tool = shutil.which("caffeinate")
                if tool:
                    self._process = subprocess.Popen(
                        [tool, "-i", "-w", str(os.getpid())],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self.active = "caffeinate"
            else:
                tool = shutil.which("systemd-inhibit")
                sleeper = shutil.which("sleep")
                if tool and sleeper:
                    self._process = subprocess.Popen(
                        [
                            tool,
                            "--what=sleep:idle",
                            "--who=mef3io-studio",
                            f"--why={self.reason}",
                            "--mode=block",
                            sleeper,
                            "infinity",
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self.active = "systemd-inhibit"
        except Exception:  # noqa: BLE001 - keeping awake is a convenience
            self.active = ""
        return self

    def __exit__(self, *_exc) -> None:
        try:
            if self._windows:
                import ctypes

                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)  # type: ignore[attr-defined]
            if self._process is not None:
                self._process.terminate()
                try:
                    self._process.wait(2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
        except Exception:  # noqa: BLE001
            pass
        self._process = None
        self._windows = False
        self.active = ""
