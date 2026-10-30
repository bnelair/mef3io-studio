"""The edit-mode draft without Qt (model/annotation_draft.py): add, change,
take out, undo and redo; stored notes are changed in the draft but never
planned for writing; routing; import and export."""

from __future__ import annotations

import json
from datetime import timedelta, timezone

import pytest

from mef3io_studio.model import annotation_draft as ad
from mef3io_studio.model import viewer_marks as vm

T0 = 946_684_800_000_000
SEC = 1_000_000


def holders_for(spans):
    """spans: {channel or None: [(a, b, recording)]}; None = any channel."""

    def holders(channel, t):
        rows = spans.get(channel) if channel is not None else sum(spans.values(), [])
        return [i for a, b, i in rows or [] if a <= t < b]

    return holders


SEQ = holders_for({"Fp1": [(T0, T0 + 60 * SEC, 0), (T0 + 90 * SEC, T0 + 150 * SEC, 1)]})
SIDE = holders_for({"Fp1": [(T0, T0 + 60 * SEC, 0)], "Cz": [(T0, T0 + 60 * SEC, 1)]})


def stored(time=T0 + 5 * SEC, text="old", recording=0, channel=None, end=None):
    return vm.Mark(time, end, "Note", recording, "channel" if channel else "session", channel, text)


# --- the text that is stored ------------------------------------------------------------------


def test_text_is_composed_and_split_back():
    assert ad.compose_text("Note", "plain") == "plain"
    assert ad.compose_text("Spike", "sharp") == "Spike: sharp"
    assert ad.compose_text("Spike", "") == "Spike:"
    assert ad.split_text("Spike: sharp wave") == ("Spike", "sharp wave")
    assert ad.split_text("Spike:") == ("Spike", "")
    assert ad.split_text("just words") == ("Note", "just words")
    assert vm.quick_label(ad.compose_text("Seizure onset", "x")) == "Seizure onset"


def test_a_point_is_a_note_and_a_span_an_event():
    d = ad.Draft()
    p = d.add(T0, None, "Spike", "a")
    s = d.add(T0 + SEC, T0 + 3 * SEC, "Note", "b")
    assert p.record() == {"type": "Note", "time": T0, "text": "Spike: a"}
    assert s.record() == {"type": "EDFA", "time": T0 + SEC, "text": "b", "duration": 2 * SEC}
    assert d.add(T0, T0, "Note").end is None  # an end that is not after the start is a point


def test_quick_types_preference():
    assert ad.parse_quick_types("") == ad.DEFAULT_QUICK_TYPES
    assert ad.parse_quick_types("Eye blink\n\n eye  blink \nTremor") == ("Eye blink", "Tremor")
    assert len(ad.parse_quick_types("\n".join(f"T{i}" for i in range(20)))) == 9


# --- undo, redo, changes ----------------------------------------------------------------------


def test_undo_and_redo_step_through_the_changes():
    d = ad.Draft()
    a = d.add(T0, None, "Spike")
    b = d.add(T0 + SEC, None, "Note", "x")
    d.update(b.uid, text="y")
    d.remove(a.uid)
    assert [i.uid for i in d.items] == [b.uid] and d.get(b.uid).text == "y"
    assert d.undo() and [i.uid for i in d.items] == [a.uid, b.uid]
    assert d.undo() and d.get(b.uid).text == "x"
    assert d.redo() and d.get(b.uid).text == "y"
    d.add(T0 + 2 * SEC)  # a new change drops the redo steps
    assert not d.can_redo
    while d.undo():
        pass
    assert not d.dirty and not d.can_undo


def test_import_is_one_undo_step():
    d = ad.Draft()
    d.add_many({"time": T0 + i * SEC, "label": "Spike"} for i in range(5))
    assert len(d.items) == 5 and all(i.imported for i in d.items)
    d.undo()
    assert not d.dirty


def test_a_changed_or_removed_stored_note_is_kept_but_never_planned():
    d = ad.Draft()
    old, other = stored(), stored(time=T0 + 9 * SEC, text="Spike: other")
    d.edit_stored(old, text="new words", label="Artifact")
    d.remove(other)
    assert d.stored_changes == 2 and d.dirty
    assert d.additions() == []
    plan = d.save_plan(SEQ)
    assert plan.count == 0 and plan.needs_copy == 2 and not plan.can_save
    kept = d.changed_version(old)
    assert kept.replaces == old and kept.stored_text == "Artifact: new words"
    assert d.is_removed(other)
    # changing the same stored note again updates its one new version
    d.edit_stored(old, text="third")
    assert len(d.changed_stored()) == 1 and d.changed_version(old).text == "third"
    # undo brings the note back
    d.undo()
    d.undo()
    d.undo()
    assert d.stored_changes == 0 and not d.dirty
    d.clear()
    assert not d.dirty and d.stored_changes == 0


def test_saved_items_leave_the_draft_and_its_history():
    d = ad.Draft()
    a, b = d.add(T0), d.add(T0 + SEC)
    d.written({a.uid})
    assert [i.uid for i in d.items] == [b.uid] and not d.can_undo


# --- routing ----------------------------------------------------------------------------------


def test_routing_for_recordings_in_turn():
    d = ad.Draft()
    d.add(T0 + 10 * SEC)  # in A
    d.add(T0 + 100 * SEC)  # in B
    d.add(T0 + 10 * SEC, channel="Fp1")
    plan = d.save_plan(SEQ)
    assert plan.can_save and sorted(plan.by_recording) == [0, 1]
    assert plan.by_recording[0].count == 2 and plan.by_recording[0].channels["Fp1"]
    assert plan.by_recording[1].count == 1 and plan.by_recording[1].span == (
        T0 + 100 * SEC,
        T0 + 100 * SEC,
    )


def test_a_whole_recording_note_between_recordings_is_refused_with_a_reason():
    d = ad.Draft()
    d.add(T0 + 70 * SEC)  # the gap
    d.add(T0 + 10 * SEC, channel="Nope")
    plan = d.save_plan(SEQ)
    assert not plan.can_save and plan.count == 0
    reasons = [r for _i, r in plan.refused]
    assert len(reasons) == 2 and all("No recording holds data" in r for r in reasons)


def test_side_by_side_whole_recording_notes_need_a_choice():
    d = ad.Draft()
    d.add(T0 + 10 * SEC)
    d.add(T0 + 20 * SEC, channel="Cz")  # a channel note has one home
    plan = d.save_plan(SIDE)
    assert not plan.can_save
    assert plan.choice_groups == [(0, 1)] and [i.time for i, _c in plan.ambiguous] == [
        T0 + 10 * SEC
    ]
    assert plan.by_recording[1].channels["Cz"]
    chosen = d.save_plan(SIDE, {(0, 1): 1})
    assert chosen.can_save and chosen.by_recording[1].count == 2 and 0 not in chosen.by_recording
    bad = d.save_plan(SIDE, {(0, 1): 7})  # not one of the choices
    assert not bad.can_save


def test_an_explicit_recording_must_hold_data_then():
    item = ad.DraftItem(1, T0 + 100 * SEC, None, "Note", recording=0)
    assert ad.route(item, SEQ).status == "outside"
    assert ad.route(ad.DraftItem(1, T0 + 10 * SEC, None, "Note", recording=0), SEQ).recording == 0


# --- typing a time ----------------------------------------------------------------------------


def test_local_times_round_trip_and_reject_nonsense():
    zone = timezone(timedelta(hours=1))
    us = T0 + 123_000
    assert ad.format_local(us, zone) == "2000-01-01 01:00:00.123"
    assert ad.parse_local("2000-01-01 01:00:00.123", zone) == us
    assert ad.parse_local("2000-01-01 01:00", zone) == T0
    for bad in ("", "tomorrow", "2000-13-01 00:00:00"):
        with pytest.raises(ValueError):
            ad.parse_local(bad, zone)


# --- import -----------------------------------------------------------------------------------


CSV = (
    "start,end,type,text,channel\n"
    "2000-01-01T00:00:10+00:00,2000-01-01T00:00:14+00:00,Spike,sharp wave,\n"
    f"{T0 + 20 * SEC},,Note,plain words,Fp1\n"
    "2000-01-01T01:00:30+01:00,,Seizure onset,,\n"  # +01:00: 00:00:30 UTC
    "2000-01-01T00:00:40,,Note,no zone,\n"
    "garbage,,Note,x,\n"
    "2000-01-01T00:00:50Z,2000-01-01T00:00:40Z,Note,backwards,\n"
    "2000-01-01T00:01:10Z,,Note,between recordings,\n"
    "2000-01-01T00:00:15Z,,Note,'=SUM(A1),\n"
)


def test_import_preview_counts_span_outside_and_invalid_rows():
    result = ad.read_rows(CSV, "csv")
    assert len(result.rows) == 5 and len(result.invalid) == 3
    pv = ad.preview_import(result, SEQ)
    assert pv.rows == 8 and len(pv.usable) == 4
    assert [n for n, _w in pv.outside] == [7]
    assert pv.first == T0 + 10 * SEC and pv.last == T0 + 30 * SEC
    reasons = dict(result.invalid)
    assert "no time zone" in reasons[4].lower() and "not a date" in reasons[5]
    assert "before the start" in reasons[6]
    spike = result.rows[0]
    assert (spike.label, spike.text, spike.end) == ("Spike", "sharp wave", T0 + 14 * SEC)
    assert result.rows[-1].text == "=SUM(A1)"  # the spreadsheet apostrophe is undone


def test_import_json_and_unusable_files():
    rows = [
        {"start": T0 + SEC, "type": "Spike", "text": "a"},
        {"start": "2000-01-01T00:00:03Z", "end": "2000-01-01T00:00:04Z", "type": "Event"},
        {"type": "Note"},
    ]
    r = ad.read_rows(json.dumps(rows), "json")
    assert len(r.rows) == 2 and r.invalid[0][0] == 3
    assert r.rows[1].label == ad.PLAIN and r.rows[1].end == T0 + 4 * SEC
    assert ad.read_rows("not json", "json").fatal
    assert "start column" in ad.read_rows("a,b\n1,2\n", "csv").fatal
    assert ad.read_rows("", "csv").fatal
    assert not ad.preview_import(ad.read_rows("a,b\n1,2\n", "csv"), SEQ).can_add


def test_export_has_the_offset_and_microseconds_and_escapes_formulas():
    zone = timezone(timedelta(hours=-5))
    rows = ad.export_rows(
        [(T0 + 1, T0 + 2 * SEC, "Spike", "=cmd()", "Fp1"), (T0, None, "Note", "plain", None)],
        zone,
    )
    assert rows[0]["start"] == "1999-12-31T19:00:00.000001-05:00"
    assert rows[0]["start_us"] == T0 + 1 and rows[1]["end"] == "" and rows[1]["channel"] == ""
    text = ad.to_csv(rows)
    assert "'=cmd()" in text and text.splitlines()[0] == ",".join(ad.EXPORT_COLUMNS)
    back = ad.read_rows(text, "csv")
    assert not back.invalid
    assert [(r.time, r.end, r.label, r.text, r.channel) for r in back.rows] == [
        (T0 + 1, T0 + 2 * SEC, "Spike", "=cmd()", "Fp1"),
        (T0, None, "Note", "plain", None),
    ]
    again = ad.read_rows(ad.to_json(rows), "json")
    assert [r.time for r in again.rows] == [T0 + 1, T0]
    assert ad.parse_time("2000-01-01T00:00:00Z") == T0 and ad.parse_time(str(T0)) == T0


def test_a_span_must_lie_within_one_recordings_data():
    d = ad.Draft()
    d.add(T0 + 50 * SEC, T0 + 100 * SEC, "Spike")  # starts in A, ends in B
    d.add(T0 + 10 * SEC, T0 + 20 * SEC, "Spike")  # inside A
    d.add(T0 + 50 * SEC, T0 + 70 * SEC, "Spike", "", "Fp1")  # ends in the gap
    plan = d.save_plan(SEQ)
    assert [i.time for i in plan.by_recording[0].session] == [T0 + 10 * SEC]
    assert len(plan.refused) == 2
    assert all("whole span" in why for _i, why in plan.refused)
    assert ad.route(ad.DraftItem(1, T0 + 50 * SEC, T0 + 60 * SEC, "Note"), SEQ).status == "ok"
    assert (
        ad.route(ad.DraftItem(1, T0 + 50 * SEC, T0 + 60 * SEC + 1, "Note"), SEQ).status == "outside"
    )


def test_control_characters_are_refused_in_the_draft_and_in_imports():
    d = ad.Draft()
    for bad in ("a\x00b", "bell\x07", "esc\x1b", "del\x7f"):
        with pytest.raises(ValueError, match="control character"):
            d.add(T0, None, "Note", bad)
    d.add(T0, None, "Note", "tab\tand\nnewline is fine")
    with pytest.raises(ValueError):
        d.add(T0, None, "Bad\x00", "x")
    assert len(d.items) == 1
    csv_text = f'start,type,text\n{T0},Note,"nul\x00inside"\n{T0},Note,fine\n'
    r = ad.read_rows(csv_text, "csv")
    assert len(r.rows) == 1 and "control character" in r.invalid[0][1]
    j = ad.read_rows(json.dumps([{"start": T0, "text": "x\u0007"}]), "json")
    assert not j.rows and "control character" in j.invalid[0][1]
    assert ad.parse_quick_types("Good\nBad\x00\nAlso") == ("Good", "Also")
