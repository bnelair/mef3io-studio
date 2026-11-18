"""A draft note in a recording with very many notes: it appears in the side list
at once, marked as not stored, and adding one does not walk over every stored
note (docs/design/viewer.md, "Adding notes quickly")."""

# ruff: noqa: F811
from __future__ import annotations

import dataclasses
import random

import pytest
from PySide6.QtCore import Qt
from test_viewer_marks_ui import wait_marks
from test_viewer_notes_many import SEC, T0, write_notes
from test_viewer_ui import make_window, settle  # noqa: F401

from mef3io_studio.model import viewer_display as vd
from mef3io_studio.model import viewer_marks as vm
from mef3io_studio.viewer import window as window_module


def mark(t, text="x", end=None, rec=0, code="Note", channel=None, state="", uid=None):
    return vm.Mark(
        t, end, code, rec, "channel" if channel else "session", channel, text, state, uid
    )


def reference(stored, drafted, changed, hidden, shown):
    """The old way, one note at a time: what compose must give."""
    out = []
    for m in stored:
        out.append(dataclasses.replace(m, state="removed") if m in changed else m)
    out += drafted
    out.sort(key=lambda m: (m.time, m.recording, m.channel or ""))
    counts = vm.type_counts(out, shown)
    visible = [m for m in out if vm.kind_for(m, shown(m.recording))[0] not in hidden]
    return out, counts, visible


@pytest.mark.parametrize("seed", range(8))
def test_compose_gives_what_a_walk_over_every_note_gives(seed):
    rng = random.Random(seed)
    shown = lambda i: i == 0  # noqa: E731 - recording 1 keeps its text hidden
    texts = ["plain", "Spike: sharp", "Seizure onset: x", "Artifact: y"]
    stored = [
        mark(
            T0 + rng.randrange(0, 500) * SEC,
            rng.choice(texts),
            code=rng.choice(["Note", "EDFA", "Seiz"]),
            end=None,
            rec=rng.choice([0, 1]),
            channel=rng.choice([None, "Fp1"]),
        )
        for _ in range(300)
    ]
    stored.sort(key=lambda m: (m.time, m.recording, m.channel or ""))
    drafted = [
        mark(
            T0 + rng.randrange(-5, 505) * SEC,
            rng.choice(texts),
            end=T0 + 600 * SEC if rng.random() < 0.3 else None,
            rec=rng.choice([0, 1]),
            state=rng.choice(["new", "changed"]),
            uid=k,
        )
        for k in range(rng.randrange(0, 6))
    ]
    changed = set(rng.sample(stored, rng.randrange(0, 4)))
    hidden = frozenset(rng.sample(["quick:Spike", "type:Note", "quick:Artifact"], rng.randrange(3)))
    got = vd.compose(vd.Stored(stored, shown), drafted, changed, hidden, shown)
    want, counts, visible = reference(stored, drafted, changed, hidden, shown)
    assert [(m.time, m.recording, m.channel, m.state) for m in got.marks] == [
        (m.time, m.recording, m.channel, m.state) for m in want
    ]
    assert got.counts == counts
    assert [id(m) for m in got.visible] == [
        id(m) for m in got.marks if vm.kind_for(m, shown(m.recording))[0] not in hidden
    ]
    assert got.times == [m.time for m in got.visible]
    assert got.keys == [vm.kind_for(m, shown(m.recording))[0] for m in got.visible]
    assert [got.visible[i].state != "" for i in got.drafted] == [True] * len(got.drafted)
    assert sum(1 for m in got.visible if m.state) == len(got.drafted) == int(got.drafted_np.sum())
    assert got.max_span == max((m.end - m.time for m in got.visible if m.end), default=0)


def long_study(tmp_path, n=3000):
    notes = [{"time": T0 + k * SEC // 2, "type": "Note", "text": f"stored {k}"} for k in range(n)]
    return write_notes(tmp_path / "long.mefd", notes, seconds=1800)


def test_a_note_added_far_down_a_long_list_is_listed_at_once_and_marked(
    make_window, tmp_path, qtbot, monkeypatch
):
    monkeypatch.setattr(window_module, "MAX_LISTED", 50)
    win = make_window(long_study(tmp_path), reveal=lambda i: True)
    wait_marks(qtbot, win)
    win.edit.ask_unsaved = lambda what: "discard"
    assert len(win.notes_tab.rows) == 50 and "first 50 of 3000" in win.notes_tab.count.text()
    win.edit.set_on(True)
    item = win.edit.add(T0 + 1000 * SEC, None, "Note", "just written")
    assert item is not None
    qtbot.waitUntil(lambda: any(m.state == "new" for m in win.notes_tab.rows), timeout=10_000)
    rows = win.notes_tab.rows
    assert len(rows) == 51 and rows[-1].state == "new" and rows[-1].text == "just written"
    row = len(rows) - 1
    assert win.notes_tab.cell_text(row, 0).endswith("(new)")  # marked as not stored
    assert win.notes_tab.table.currentRow() == row  # and selected
    # still found by a search, still a draft there
    win.notes_tab.search.setText("just written")
    qtbot.waitUntil(lambda: [m.state for m in win.notes_tab.rows] == ["new"], timeout=10_000)
    win.notes_tab.search.setText("")
    # and reached by the next-note key like any other
    win.set_state(t0=T0 + 990 * SEC)
    settle(qtbot, win)
    win.go_to_mark(1)
    assert abs(win.state.t0 + win.page_us() / 2 - (T0 + 1000 * SEC)) < win.page_us()


def test_adding_a_note_does_not_walk_over_every_stored_note(
    make_window, tmp_path, qtbot, monkeypatch
):
    win = make_window(long_study(tmp_path, n=3000), reveal=lambda i: True)
    wait_marks(qtbot, win)
    win.edit.ask_unsaved = lambda what: "discard"
    win.edit.set_on(True)
    win.edit.add(T0 + 100 * SEC, None, "Note", "first")  # builds what the stored notes give, once
    calls = []
    real = vm.kind_for
    monkeypatch.setattr(vm, "kind_for", lambda m, ok: (calls.append(1), real(m, ok))[1])
    for k in range(5):
        win.edit.add(T0 + (200 + k) * SEC, None, "Note", f"more {k}")
    assert len(calls) < 1500, len(calls)  # the page and the list only; not 5 x 3000 = 15000
    assert len(win.edit.draft.items) == 6
    win.edit.undo()
    assert len(win._shaped().counts) >= 1 and len(win.edit.draft.items) == 5


def test_a_taken_out_stored_note_stays_listed_struck_through_with_the_arrays_right(
    make_window, tmp_path, qtbot
):
    win = make_window(long_study(tmp_path, n=200), reveal=lambda i: True)
    wait_marks(qtbot, win)
    win.edit.ask_unsaved = lambda what: "discard"
    win.edit.set_on(True)
    stored = win.marks[10]
    win.edit.draft.remove(stored)  # the draft's way to mark a stored note as taken out
    win.edit.refresh()
    shape = win._shaped()
    assert len(shape.marks) == 200 and shape.marks[10].state == "removed"
    assert shape.drafted == [10] and bool(shape.drafted_np[10]) and shape.drafted_np.sum() == 1
    win.edit.add(T0 + 2 * SEC, None, "Note", "before it")
    shape = win._shaped()
    assert (
        len(shape.marks) == 201
        and [i for i, m in enumerate(shape.marks) if m.state] == shape.drafted
    )
    assert sorted(shape.marks[i].state for i in shape.drafted) == ["new", "removed"]
    key(win, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)


def key(win, k, mods=Qt.KeyboardModifier.NoModifier):
    from PySide6.QtTest import QTest

    QTest.keyClick(win.canvas, k, mods)


def test_the_cached_search_text_finds_exactly_what_the_slow_rule_finds(
    make_window, tmp_path, qtbot
):
    notes = [
        {"time": T0 + k * SEC, "type": "Note", "text": text}
        for k, text in enumerate(["Alpha Note", "Spike: Sharp", "plain", "ÄÖ ünï", ""] * 8)
    ]
    shown = {0: False}
    win = make_window(
        write_notes(tmp_path / "s.mefd", notes, seconds=120), reveal=lambda i: shown[i]
    )
    wait_marks(qtbot, win)
    for flag in (False, True):  # hidden: the text is not searched; shown: it is
        shown[0] = flag
        win._refresh_hidden_notice()
        win._rebuild_types()
        for query in ("", "alpha", "ALPHA ", "sharp", "spike", "ünï", "Whole", "a", "zzz"):
            mine = [
                m
                for m in win._visible
                if not query.strip() or query.strip().casefold() in win._haystack(m)
            ]
            rule = [m for m in win._visible if vm.matches(m, query, win._shown(m.recording), "A")]
            assert mine == rule, (flag, query)
