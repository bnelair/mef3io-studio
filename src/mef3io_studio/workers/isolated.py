"""Run a call in a separate helper process.

Why: a worker *thread* only keeps the window responsive if the call releases
Python's GIL. mef3io currently holds it for the whole of a recording open
(bnelair/mef3io#79) — half a second or more on a large recording, during which
the window cannot redraw. A helper process has its own GIL, so the window
stays live whatever the library does. It also makes Cancel immediate: the
helper is simply stopped.

Cost: starting a helper takes a fraction of a second, so this is for calls
that take longer than that (opening a recording), not for tiny ones. Once
mef3io#79 ships, sources can switch ``isolate`` off.

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
from collections.abc import Callable
from typing import Any

from ..model.cancel import CancelToken, JobCancelled
from ..model.errors import HelperCrashed

_POLL_SECONDS = 0.05


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


def _child(conn, fn, args, kwargs, messages: bool) -> None:  # runs in the helper process
    _watch_parent()
    try:
        if messages:

            def send(item: Any) -> None:
                conn.send(("message", item))

            kwargs = dict(kwargs, send=send)
        result = fn(*args, **kwargs)
        message = ("result", True, result)
    except BaseException as exc:  # noqa: BLE001 - everything goes back to the parent
        message = ("result", False, exc)
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
    on_poll: Callable[[], None] | None = None,
    **kwargs: Any,
) -> Any:
    """Call ``fn(*args, **kwargs)`` in a helper process and return its result
    (or raise its exception). ``fn`` and its arguments must be picklable.
    If ``cancel`` is set while it runs, the helper is stopped and
    :class:`JobCancelled` is raised.

    ``on_message``: ``fn`` is also given ``send=``, a function that passes a
    picklable item back while it works (one opened recording, a progress
    step); each item reaches ``on_message`` here, in this thread, in order.
    ``on_poll`` is called about every 50 ms while the helper runs (e.g. to
    measure how far a copy has got)."""
    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    pickle.dumps((fn, args, kwargs))  # fail early and clearly, in this process
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_child,
        args=(sender, fn, args, kwargs, on_message is not None),
        daemon=True,
    )
    proc.start()
    sender.close()
    finished = False
    try:
        while True:
            if cancel.cancelled:
                raise JobCancelled()
            if receiver.poll(_POLL_SECONDS):
                try:
                    message = receiver.recv()
                except EOFError:
                    proc.join(1)
                    raise HelperCrashed(proc.exitcode) from None
                if message[0] == "message":
                    if on_message is not None:
                        on_message(message[1])
                    continue
                _tag, ok, value = message
                finished = True
                if ok:
                    return value
                raise value
            if on_poll is not None:
                on_poll()
            if not proc.is_alive() and not receiver.poll():
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
