"""The channel step's rules (model/channels.py), without Qt."""

from __future__ import annotations

import time

import pytest

from mef3io_studio.model.channels import (
    OTHER_GROUP,
    ChannelRow,
    ChannelSelection,
    contact_of,
    group_channels,
    group_key,
    natural_key,
    prefix_of,
)


def electrodes(n=256):
    prefixes = [f"{side}{x}" for side in "LR" for x in "ABCDEFGH"]
    return [f"{p}{i}" for p in prefixes for i in range(1, 17)][:n]


def test_prefixes_and_natural_order():
    assert prefix_of("LA12") == "LA"
    assert prefix_of("RH-3") == "RH"
    assert prefix_of("Fp1") == "Fp"
    assert prefix_of("ECG") == "ECG"
    assert prefix_of("  C3 ") == "C"
    names = ["LA10", "LA2", "LA1"]
    assert sorted(names, key=natural_key) == ["LA1", "LA2", "LA10"]


def test_groups_in_order_of_appearance_and_singletons_pooled():
    rows = [ChannelRow(n) for n in ("RA2", "RA1", "ECG", "LA1", "LA2", "EMG", "RA10")]
    groups = group_channels(rows)
    assert [g.prefix for g in groups] == ["RA", "LA", OTHER_GROUP]
    assert groups[0].names == ("RA1", "RA2", "RA10")
    assert groups[2].names == ("ECG", "EMG")


def test_everything_is_kept_at_the_start():
    s = ChannelSelection({"r": electrodes()})
    assert s.total == 256 and len(s.kept) == 256 and s.excluded == ()
    assert len(s.groups) == 16 and s.can_continue


def test_ranges_follow_the_list_order_and_the_filter():
    s = ChannelSelection({"r": electrodes()})
    assert s.set_range("LA14", "LB2", keep=False)
    assert s.excluded == ("LA14", "LA15", "LA16", "LB1", "LB2")
    # With a filter, a range never reaches channels that are not shown.
    s.keep_all()
    shown = s.matching("1")  # LA1, LA10..LA16, LB1, ...
    s.set_range("LA1", "LB1", keep=False, visible=shown)
    assert "LA2" not in s.excluded and "LA10" in s.excluded and "LB1" in s.excluded


def test_group_state_is_tri_state():
    s = ChannelSelection({"r": electrodes()})
    la = s.groups[0]
    assert s.group_state(la) is True
    s.toggle("LA3")
    assert s.group_state(la) is None
    s.set_kept(la.names, False)
    assert s.group_state(la) is False


def test_bulk_actions_and_undo_redo():
    s = ChannelSelection({"r": electrodes()})
    s.set_kept(s.matching("RA"), False)
    assert len(s.kept) == 240
    s.invert()
    assert set(s.kept) == {f"RA{i}" for i in range(1, 17)}
    s.exclude_all()
    assert not s.kept and not s.can_continue
    assert s.undo() and len(s.kept) == 16
    assert s.undo() and len(s.kept) == 240
    assert s.redo() and len(s.kept) == 16
    # A change after an undo clears the redo history.
    s.keep_all()
    assert not s.can_redo
    # A click that changes nothing adds no undo step.
    steps = len(s._undo)
    assert not s.set_kept(["LA1"], True)
    assert len(s._undo) == steps


def test_batch_is_by_name_and_flags_missing_names():
    s = ChannelSelection({"a": ["x1", "x2", "ecg"], "b": ["x1", "x2"], "c": ["x1"]})
    assert s.total == 3
    assert s.missing_from() == {"x2": ("c",), "ecg": ("b", "c")}
    s.set_kept(["ecg", "x2"], False)
    assert s.excluded_in("a") == ("x2", "ecg") and s.kept_in("c") == ("x1",)
    s.set_kept(["x1"], False)
    assert set(s.recordings_left_empty()) == {"a", "b", "c"} and not s.can_continue
    one = ChannelSelection({"a": ["x"]})
    assert one.missing_from() == {}


def test_fast_enough_for_hundreds_of_channels():
    s = ChannelSelection({k: electrodes() for k in "abcd"})
    t0 = time.perf_counter()
    for _ in range(50):
        s.invert()
        s.set_range("LA1", "RH16", keep=True)
        s.matching("RA1")
        s.missing_from()
        s.recordings_left_empty()
    assert time.perf_counter() - t0 < 1.0


# --------------------------------------------------------------------------
# Real montages (review round 1)


def montage(style: str, n=256) -> list[str]:
    contacts = electrodes(n)
    if style == "eeg-ref":
        return [f"EEG {c}-Ref" for c in contacts]
    if style == "pol-avg":
        return [f"POL {c}-AVG" for c in contacts]
    if style == "bipolar":
        # LA1-LA2, LA2-LA3, ... per electrode (15 pairs of 16 contacts) + 16 more
        out = []
        for p in dict.fromkeys(prefix_of(c) for c in contacts):
            out += [f"{p}{i}-{p}{i + 1}" for i in range(1, 17)]
        return out[:n]
    if style == "mixed-case":
        return [c.lower() if i % 2 else c for i, c in enumerate(contacts)]
    raise AssertionError(style)


@pytest.mark.parametrize("style", ["eeg-ref", "pol-avg", "bipolar", "mixed-case"])
def test_256_channel_montages_group_by_electrode(style):
    names = montage(style)
    assert len(names) == 256
    s = ChannelSelection({"r": names})
    assert len(s.groups) == 16, [g.prefix for g in s.groups]
    assert all(len(g.rows) == 16 for g in s.groups)
    assert OTHER_GROUP not in [g.prefix for g in s.groups]


def test_recorder_labels_references_and_bipolar_pairs():
    assert contact_of("EEG LA1-Ref") == "LA1"
    assert contact_of("POL RH12-REF") == "RH12"
    assert contact_of("C3-AVG") == "C3"
    assert contact_of("LAH1-LAH2") == "LAH1"
    assert prefix_of("LAH1-LAH2") == "LAH"
    assert prefix_of("RH-3") == "RH"  # a dash before a number is not a pair
    assert group_key("La1") == group_key("LA2") == "la"
