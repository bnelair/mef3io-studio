"""Run a batch in its own process, for the crash test: the parent kills
this process in the middle of the second recording (a real process death,
not an exception)."""

from __future__ import annotations

import pickle
import sys


def main(path: str) -> None:
    with open(path, "rb") as handle:
        batch = pickle.load(handle)
    from mef3io_studio.model.cancel import CancelToken
    from mef3io_studio.model.progress import Mailbox, ProgressSlot
    from mef3io_studio.workers import tasks
    from mef3io_studio.workers.jobs import JobContext

    ctx = JobContext(CancelToken(), ProgressSlot(), 1, Mailbox())
    tasks.run_batch(ctx, batch)


if __name__ == "__main__":
    main(sys.argv[1])
