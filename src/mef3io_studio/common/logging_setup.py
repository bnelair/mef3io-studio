"""The app's log: a rotating file in the platform's user log folder, with the
privacy filter on every handler."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from platformdirs import user_log_dir

from .privacy import PrivacyFilter

APP_NAME = "mef3io-studio"
APP_AUTHOR = "BNEL"
LOG_FILE_NAME = "mef3io-studio.log"
#: Override the log folder (tests, portable use).
LOG_DIR_ENV = "MEF3IO_STUDIO_LOG_DIR"

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_installed: list[logging.Handler] = []


def log_dir() -> Path:
    override = os.environ.get(LOG_DIR_ENV)
    if override:
        return Path(override)
    return Path(user_log_dir(APP_NAME, APP_AUTHOR))


def setup_logging(
    directory: Path | None = None, level: int = logging.INFO, console: bool = False
) -> Path:
    """Install the rotating file handler (5 files x 1 MB) on the root logger.

    Safe to call again: previously installed handlers are replaced. Returns the
    log file path. If the folder cannot be created, logging stays off rather
    than stopping the app.
    """
    teardown_logging()
    directory = Path(directory) if directory is not None else log_dir()
    path = directory / LOG_FILE_NAME
    root = logging.getLogger()
    privacy = PrivacyFilter()
    handlers: list[logging.Handler] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                path, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
            )
        )
    except OSError:
        pass
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(privacy)
        root.addHandler(handler)
        _installed.append(handler)
    root.setLevel(level)
    logging.captureWarnings(True)
    return path


def teardown_logging() -> None:
    root = logging.getLogger()
    while _installed:
        handler = _installed.pop()
        root.removeHandler(handler)
        handler.close()
