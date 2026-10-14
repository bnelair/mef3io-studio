"""The app's log: a rotating file in the platform's user log folder, with the
privacy filter on every handler. Uncaught exceptions (main thread and worker
threads), Qt's own messages and hard crashes are routed through the same
filter.
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from platformdirs import user_log_dir

from .privacy import PrivacyFilter, scrub

APP_NAME = "mef3io-studio"
APP_AUTHOR = "BNEL"
LOG_FILE_NAME = "mef3io-studio.log"
#: Raw output of faulthandler from a hard crash. It cannot be filtered while
#: the process is dying, so it is scrubbed into the log on the next start and
#: deleted.
CRASH_FILE_NAME = "crash-pending.txt"
#: Override the log folder (tests, portable use).
LOG_DIR_ENV = "MEF3IO_STUDIO_LOG_DIR"

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_installed: list[logging.Handler] = []
_crash_file = None
log = logging.getLogger("mef3io_studio")


def log_dir() -> Path:
    override = os.environ.get(LOG_DIR_ENV)
    if override:
        return Path(override)
    return Path(user_log_dir(APP_NAME, APP_AUTHOR))


def setup_logging(
    directory: Path | None = None,
    level: int = logging.INFO,
    console: bool = False,
    hooks: bool = False,
) -> Path:
    """Install the rotating file handler (5 files x 1 MB) on the root logger.

    ``hooks=True`` (the app) also installs the exception hooks and the crash
    handler. Safe to call again: previously installed handlers are replaced.
    Returns the log file path. If the folder cannot be created, logging stays
    off rather than stopping the app.
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
    if hooks:
        install_hooks(directory)
    return path


def teardown_logging() -> None:
    root = logging.getLogger()
    while _installed:
        handler = _installed.pop()
        root.removeHandler(handler)
        handler.close()


# --------------------------------------------------------------------------
# Hooks
# --------------------------------------------------------------------------


def _log_uncaught(exc_type, exc, tb, where: str) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        return
    log.critical("uncaught %s in %s", exc_type.__name__, where, exc_info=(exc_type, exc, tb))


def _sys_hook(exc_type, exc, tb) -> None:
    _log_uncaught(exc_type, exc, tb, "the main thread")


def _thread_hook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return
    _log_uncaught(args.exc_type, args.exc_value, args.exc_traceback, "a background thread")


def _qt_message_handler(mode, context, message) -> None:
    from PySide6.QtCore import QtMsgType

    level = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }.get(mode, logging.WARNING)
    logging.getLogger("qt").log(level, "%s", message)


def install_qt_message_handler() -> None:
    from PySide6.QtCore import qInstallMessageHandler

    qInstallMessageHandler(_qt_message_handler)


def recover_crash_report(directory: Path) -> None:
    """Move a crash report left by the previous run into the log, scrubbed."""
    crash = directory / CRASH_FILE_NAME
    try:
        text = crash.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return
    if text:
        log.error("the previous run stopped unexpectedly:\n%s", scrub(text))
    try:
        crash.unlink()
    except OSError:
        pass


def install_hooks(directory: Path) -> None:
    global _crash_file
    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    recover_crash_report(directory)
    try:
        if _crash_file is None:
            _crash_file = open(directory / CRASH_FILE_NAME, "w", encoding="utf-8")  # noqa: SIM115
            faulthandler.enable(_crash_file, all_threads=True)
    except (OSError, RuntimeError, ValueError):
        _crash_file = None


def close_crash_file() -> None:
    """On a clean exit: stop the crash handler and remove its (empty) file."""
    global _crash_file
    if _crash_file is None:
        return
    try:
        faulthandler.disable()
        name = _crash_file.name
        _crash_file.close()
        if os.path.getsize(name) == 0:
            os.unlink(name)
    except OSError:
        pass
    _crash_file = None
