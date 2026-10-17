"""Run a call in a separate helper process.

Why it stays, now that mef3io 1.4 releases the GIL during an open and in
``inspect`` (bnelair/mef3io#79, closed): a helper process

* **contains native crashes.** A damaged or hostile file that crashes the
  C++ library takes down the helper, not the app; the user gets a plain
  error and every other task goes on;
* **gives a hard Stop.** Calls that cannot stop by themselves are ended at
  once, and calls that can are ended anyway after ``stop_grace``;
* keeps the window live whatever a call does with the GIL (a helper has its
  own).

Cost: starting a helper takes a fraction of a second, so this is for calls
that take longer than that (opening a recording, an anonymized copy), not
for tiny ones.

Stopping: by default Stop ends the helper at once. A call that can stop
cleanly by itself (mef3io 1.3 takes a ``cancel=`` token in its long
operations, and leaves nothing behind when stopped) asks for
``stop_grace``: the function is then given ``cancel=``, a token that is set
when the user presses Stop, and the helper gets that many seconds to finish
on its own before it is ended anyway.

Plain Python, no Qt. The helper is started with the "spawn" method on every
platform (identical behaviour everywhere, and safe with threads); packaged
builds therefore call ``multiprocessing.freeze_support()`` first thing
(packaging/launcher.py).
"""

from __future__ import annotations

import multiprocessing
import os
import pickle
import threading
import time
from collections.abc import Callable
from typing import Any

from ..model.cancel import CancelToken, JobCancelled, is_stop
from ..model.errors import HelperCrashed

_POLL_SECONDS = 0.05

#: How long a helper that stops by itself (``stop_grace``) normally gets:
#: mef3io notices a stop between units of work, well within this.
STOP_GRACE_SECONDS = 3.0


#: Kept for callers that catch the older name.
HelperProcessError = HelperCrashed


def _watch_parent() -> None:
    """Exit as soon as the app that started this helper is gone.

    Without this, a helper outlives an app that was killed or crashed
    (bnelair/mef3io-studio#20) and keeps reading the recording. The parent's
    sentinel becomes ready when the parent process ends, on every platform.
    """
    parent = multiprocessing.parent_process()
    if parent is None:
        return

    def watch() -> None:
        parent.join()
        os._exit(1)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def _cancel_when_set(stop_event, finished: threading.Event) -> tuple[CancelToken, threading.Thread]:
    """A token in the helper that is cancelled when the parent sets
    ``stop_event`` (the user pressed Stop). The watcher ends when
    ``finished`` is set, so no library object outlives the call."""
    token = CancelToken()

    def watch() -> None:
        while not finished.is_set():
            if stop_event.wait(_POLL_SECONDS):
                token.cancel()
                return

    thread = threading.Thread(target=watch, name="stop-watch", daemon=True)
    thread.start()
    return token, thread


def _child(conn, fn, args, kwargs, messages: bool, stop_event=None) -> None:  # in the helper
    _watch_parent()
    finished = threading.Event()
    watcher = token = None
    try:
        if messages:
            lock = threading.Lock()

            def send(item: Any) -> None:
                with lock:  # progress may come from a library thread
                    conn.send(("message", item))

            kwargs = dict(kwargs, send=send)
        if stop_event is not None:
            token, watcher = _cancel_when_set(stop_event, finished)
            kwargs = dict(kwargs, cancel=token)
        result = fn(*args, **kwargs)
        message = ("result", True, result)
    except BaseException as exc:  # noqa: BLE001 - everything goes back to the parent
        # A stop on request (mef3io.Cancelled included) travels as ours.
        message = ("result", False, JobCancelled() if is_stop(exc) else exc)
    finally:
        finished.set()
        if watcher is not None:
            watcher.join(1)
        kwargs = token = None
    try:
        conn.send(message)
    except Exception:
        # The result or the exception could not be pickled: send a plain one.
        _tag, _ok, value = message
        conn.send(("result", False, RuntimeError(f"{type(value).__name__} (not transferable)")))
    finally:
        conn.close()


def run_isolated(
    fn: Callable[..., Any],
    *args: Any,
    cancel: CancelToken | None = None,
    on_message: Callable[[Any], None] | None = None,
    stop_grace: float | None = None,
    **kwargs: Any,
) -> Any:
    """Call ``fn(*args, **kwargs)`` in a helper process and return its result
    (or raise its exception). ``fn`` and its arguments must be picklable.
    If ``cancel`` is set while it runs, the helper is stopped and
    :class:`JobCancelled` is raised.

    ``stop_grace`` (seconds): ``fn`` is also given ``cancel=``, a token set
    when ``cancel`` is, and stops by itself (mef3io removes what it had
    started); the helper is ended only if it has not finished after that
    long. Without it the helper is ended at once.

    ``on_message``: ``fn`` is also given ``send=``, a function that passes a
    picklable item back while it works (one opened recording, a progress
    step); each item reaches ``on_message`` here, in this thread, in order."""
    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    pickle.dumps((fn, args, kwargs))  # fail early and clearly, in this process
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    stop_event = ctx.Event() if stop_grace is not None else None
    proc = ctx.Process(
        target=_child,
        args=(sender, fn, args, kwargs, on_message is not None, stop_event),
        daemon=True,
    )
    proc.start()
    sender.close()
    finished = False
    stop_by: float | None = None  # when a helper asked to stop gets ended anyway
    try:
        while True:
            if stop_by is None and cancel.cancelled:
                if stop_event is None:
                    raise JobCancelled()
                stop_event.set()
                stop_by = time.monotonic() + max(0.0, stop_grace or 0.0)
            if stop_by is not None and time.monotonic() >= stop_by:
                raise JobCancelled()
            if receiver.poll(_POLL_SECONDS):
                try:
                    message = receiver.recv()
                except EOFError:
                    proc.join(1)
                    if stop_by is not None:
                        raise JobCancelled() from None
                    raise HelperCrashed(proc.exitcode) from None
                if message[0] == "message":
                    if on_message is not None and stop_by is None:
                        on_message(message[1])
                    continue
                _tag, ok, value = message
                finished = True
                if stop_by is not None:
                    raise JobCancelled()  # stopped, whatever it got to
                if ok:
                    return value
                raise value
            if not proc.is_alive() and not receiver.poll():
                if stop_by is not None:
                    raise JobCancelled()
                raise HelperCrashed(proc.exitcode)
    finally:
        receiver.close()
        if finished:
            # It answered and is exiting on its own: let it, rather than
            # SIGTERM a process that is merely tidying up.
            proc.join(1)
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
        if proc.is_alive():  # pragma: no cover - terminate is normally enough
            proc.kill()
            proc.join(5)


def stop_all_helpers(timeout: float = 1.0) -> None:
    """Stop every helper process still running (used before a forced quit,
    so none is left behind as an orphan)."""
    children = multiprocessing.active_children()
    for child in children:
        child.terminate()
    for child in children:
        child.join(timeout)
        if child.is_alive():
            child.kill()
            child.join(timeout)
