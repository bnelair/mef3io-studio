"""Acceptance (issue #4): opening and inspecting a large recording reads
well under 1% of its bytes: no signal, no block table.

Counted in the helper process that does the work, with /proc/self/io
``rchar`` (every byte read, from disk or cache), the same method as mef3io's
tests/test_p17_large_files.py. Linux only: elsewhere there is no /proc.
"""

from __future__ import annotations

import os
from pathlib import Path

import mef3io
import pytest
from explorer_sessions import write_large, write_plain

from mef3io_studio.workers.isolated import run_isolated

pytestmark = pytest.mark.skipif(
    not Path("/proc/self/io").exists(), reason="needs /proc/self/io (Linux)"
)

BUDGET = 0.01


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


@pytest.fixture(scope="module")
def large(tmp_path_factory):
    root = tmp_path_factory.mktemp("large")
    folder = write_large(root / "large.mefd")
    packed = Path(mef3io.archive_session(str(folder), str(root / "large.mefd.tar")))
    warm = write_plain(root / "warm.mefd")
    return folder, packed, warm


@pytest.mark.parametrize("kind", ["folder", "single file"])
def test_inspecting_reads_under_one_percent(large, kind):
    folder, packed, warm = large
    target = folder if kind == "folder" else packed
    total = _size(target)
    assert total > 30_000_000, "the recording must be large for the ratio to mean anything"
    read, channels, parts = run_isolated(
        __import__("helper_targets").measure_inspect, str(warm), str(target)
    )
    assert (channels, parts) == (2, 2)
    assert read < BUDGET * total, f"read {read} bytes of {total} ({100 * read / total:.2f}%)"
    # A loose absolute bound too: the details files (16 kB per part) plus
    # small notes files and, for a single file, its table of contents. On this
    # fixture the block table is too small for the bound to catch a toc() read;
    # the guard in helper_targets.measure_inspect is what pins that exactly.
    details = channels * parts * 16_384
    assert read < 3 * details + 200_000, read


def test_the_measurement_would_catch_a_signal_read(large):
    """Guard against a vacuous pass: reading the signal shows up."""
    folder, _packed, _warm = large
    before = int(
        next(line for line in open("/proc/self/io") if line.startswith("rchar")).split()[1]
    )
    with mef3io.Reader(str(folder), warn_declarations=False) as reader:
        reader.read(reader.channels[0])
    after = int(next(line for line in open("/proc/self/io") if line.startswith("rchar")).split()[1])
    assert after - before > BUDGET * _size(folder)
    assert os.path.isdir(folder)
