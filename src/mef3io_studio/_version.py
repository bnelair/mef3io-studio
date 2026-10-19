"""The app's version, read from the single source of truth: the repo-root
``VERSION`` file.

Three ways the app runs, three places that file can be:

* from a source checkout: ``<repo>/VERSION`` (two levels above ``src/``);
* frozen by PyInstaller: bundled next to the app (``sys._MEIPASS/VERSION``);
* installed with pip (dev only): the distribution metadata, which setuptools
  filled from the same file.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Where every version is published. The app never checks it by itself (it
#: makes no network connections); it only shows this link.
RELEASES_URL = "https://github.com/bnelair/mef3io-studio/releases"

# A PEP 440 pre-release or development version: 0.1.0.dev3, 0.2.0rc1, ...
_TEST_VERSION = re.compile(r"(?:\.dev\d+|(?:a|b|rc)\d+)$")


def _read(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def app_version() -> str:
    frozen_dir = getattr(sys, "_MEIPASS", None)
    if frozen_dir:
        found = _read(Path(frozen_dir) / "VERSION")
        if found:
            return found
    found = _read(Path(__file__).resolve().parents[2] / "VERSION")
    if found:
        return found
    try:
        from importlib.metadata import version

        return version("mef3io-studio")
    except Exception:
        return "0+unknown"


def mef3io_version() -> str:
    """Version of the mef3io library actually loaded (the bundled one in an
    installer), or a plain note when it cannot be imported."""
    try:
        import mef3io
    except Exception:
        return "not available"
    return str(getattr(mef3io, "__version__", "unknown"))


def is_test_version(version: str) -> bool:
    """True for a test version (development or pre-release build)."""
    return bool(_TEST_VERSION.search(version))


def version_label(version: str | None = None) -> str:
    """The version as people see it: ``0.1.0.dev3 (test version)``."""
    version = version or app_version()
    return f"{version} (test version)" if is_test_version(version) else version


__version__ = app_version()
