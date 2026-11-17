"""A bounded sample of a long array of values, for estimates that need no more.

Pure numpy, no Qt. A regular stride (``a[::n // 1024]``) locks to a periodic signal
(a sine whose period divides the stride is read at one phase only), so the sample is
taken at fixed pseudo-random positions: the same for every array of one length, drawn
once from a seeded generator and kept, so it is cheap and repeatable (no run-to-run
jitter in what is drawn) and cannot lock to a period.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

#: The seed of the positions: fixed, so the same data always gives the same sample.
SEED = 20260516


@lru_cache(maxsize=64)
def sample_positions(n: int, k: int) -> np.ndarray:
    """``k`` sorted positions in ``0 .. n - 1`` (read-only), the same on every call."""
    rng = np.random.default_rng((SEED, n, k))
    idx = np.sort(rng.integers(0, n, size=k))
    idx.setflags(write=False)
    return idx


def bounded_sample(a: np.ndarray, k: int) -> np.ndarray:
    """``a`` itself when it holds at most ``k`` values, else ``k`` of them taken at
    pseudo-random positions (see the module note)."""
    n = a.size
    if n <= k:
        return a
    return a[sample_positions(n, k)]
