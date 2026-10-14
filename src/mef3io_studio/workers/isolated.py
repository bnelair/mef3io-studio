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
import pickle
from collections.abc import Callable
from typing import Any

from ..model.cancel import CancelToken, JobCancelled

_POLL_SECONDS = 0.05


class HelperProcessError(RuntimeError):
    """The helper process stopped without returning a result."""


def _child(conn, fn, args, kwargs) -> None:  # runs in the helper process
    try:
        result = fn(*args, **kwargs)
        message = (True, result)
    except BaseException as exc:  # noqa: BLE001 - everything goes back to the parent
        message = (False, exc)
    try:
        conn.send(message)
    except Exception:
        # The result or the exception could not be pickled: send a plain one.
        ok, value = message
        conn.send((False, HelperProcessError(f"{type(value).__name__} (not transferable)")))
    finally:
        conn.close()


def run_isolated(
    fn: Callable[..., Any], *args: Any, cancel: CancelToken | None = None, **kwargs: Any
) -> Any:
    """Call ``fn(*args, **kwargs)`` in a helper process and return its result
    (or raise its exception). ``fn`` and its arguments must be picklable.
    If ``cancel`` is set while it runs, the helper is stopped and
    :class:`JobCancelled` is raised."""
    cancel = cancel or CancelToken()
    cancel.raise_if_cancelled()
    pickle.dumps((fn, args, kwargs))  # fail early and clearly, in this process
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(sender, fn, args, kwargs), daemon=True)
    proc.start()
    sender.close()
    try:
        while True:
            if cancel.cancelled:
                raise JobCancelled()
            if receiver.poll(_POLL_SECONDS):
                try:
                    ok, value = receiver.recv()
                except EOFError:
                    raise HelperProcessError(
                        f"helper process ended unexpectedly (exit code {proc.exitcode})"
                    ) from None
                if ok:
                    return value
                raise value
            if not proc.is_alive() and not receiver.poll():
                raise HelperProcessError(
                    f"helper process ended unexpectedly (exit code {proc.exitcode})"
                )
    finally:
        receiver.close()
        if proc.is_alive():
            proc.terminate()
        proc.join(5)
        if proc.is_alive():  # pragma: no cover - terminate is normally enough
            proc.kill()
            proc.join(5)
