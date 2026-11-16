"""A montage change costs no reading: raw tiles are read and cached in FIXED
groups of the recording's channels (docs/design/viewer.md, "Data"), whatever the
montage; a reference or bipolar partner is taken from whichever group holds it."""

# ruff: noqa: F811
from __future__ import annotations

import threading

from test_viewer_ui import SEC, T0, make_window, settle, write  # noqa: F401

from mef3io_studio.model import dsp
from mef3io_studio.model import viewer_view as vv
from mef3io_studio.workers import viewer_pipeline as pipe

NAMES = [f"E{i:02d}" for i in range(40)]
FILTERS = dsp.FilterSettings(0.5, 70.0)


def spy_reads(win):
    reads: list[tuple[str, ...]] = []
    lock = threading.Lock()
    real = win.data.read_tile

    def counting(channels, rate, k, cancel=None):
        with lock:
            reads.append(tuple(channels))
        return real(channels, rate, k, cancel)

    win.data.read_tile = counting
    return reads


def cached_groups(win) -> set:
    with win.controller.cache._lock:
        return {
            k[3] for k in win.controller.cache._items if len(k) == 5 and k[0] == win.data.data_id
        }


def picture(win):
    img = win.canvas.grab_traces().toImage()
    return bytes(img.constBits())


def show(win, qtbot, montage, reference=None):
    win.set_state(montage=montage, reference=reference)
    settle(qtbot, win)


def test_the_fixed_groups_follow_the_recording_not_the_montage(make_window, tmp_path):
    win = make_window(write(tmp_path / "m.mefd", NAMES, seconds=30))
    fixed = pipe.fixed_groups(win.data)
    assert set(fixed) == set(NAMES) and {len(g) for g in fixed.values()} == {16, 8}
    assert fixed["E00"] == tuple(NAMES[:16]) and fixed["E39"] == tuple(NAMES[32:])
    rows = win.current_rows()
    for plan in pipe.plan_groups(win.data, rows):  # as recorded: one group per fixed group
        assert len(plan.sources) == 1


def test_changing_the_montage_reads_at_most_the_missing_groups_then_nothing(
    make_window, tmp_path, qtbot
):
    path = write(tmp_path / "m.mefd", NAMES, seconds=30)
    win = make_window(path)
    win.set_state(filters=FILTERS, t0=T0 + 5 * SEC)
    settle(qtbot, win)
    fixed = set(pipe.fixed_groups(win.data).values())
    before = cached_groups(win)
    reads = spy_reads(win)
    # a reference in a group the page has not read yet: only missing groups are read
    show(win, qtbot, vv.MONTAGE_REFERENCE, "E39")
    assert set(reads) <= fixed - before
    assert tuple(NAMES[32:]) in cached_groups(win)
    reference_picture = picture(win)
    show(win, qtbot, vv.MONTAGE_BIPOLAR)
    bipolar_picture = picture(win)
    show(win, qtbot, vv.MONTAGE_RECORDED)
    # every group the page can need is cached now: no montage reads anything
    del reads[:]
    for montage, ref in (
        (vv.MONTAGE_REFERENCE, "E39"),
        (vv.MONTAGE_BIPOLAR, None),
        (vv.MONTAGE_RECORDED, None),
        (vv.MONTAGE_BIPOLAR, None),
        (vv.MONTAGE_REFERENCE, "E39"),
    ):
        show(win, qtbot, montage, ref)
    assert reads == []
    assert picture(win) == reference_picture
    # and the pictures are those of windows that opened straight on that montage
    for montage, ref, expected in (
        (vv.MONTAGE_REFERENCE, "E39", reference_picture),
        (vv.MONTAGE_BIPOLAR, None, bipolar_picture),
    ):
        fresh = make_window(path)
        fresh.set_state(filters=FILTERS, t0=T0 + 5 * SEC, montage=montage, reference=ref)
        settle(qtbot, fresh)
        assert picture(fresh) == expected
