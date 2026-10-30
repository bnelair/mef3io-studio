"""Viewer montages: lead parser, pairing, reference (synthetic names only)."""

from __future__ import annotations

import numpy as np
import pytest

from mef3io_studio.model import montage as m


@pytest.mark.parametrize(
    "name,expected",
    [
        ("LA3", ("LA", 3)),
        ("LA_3", ("LA", 3)),
        ("LA-3", ("LA", 3)),
        ("LA 12", ("LA", 12)),
        ("A1'", ("A'", 1)),
        ("A'1", ("A'", 1)),
        ("LA03", ("LA", 3)),
        ("ECG", ("ECG", None)),
        ("7", ("7", None)),
    ],
)
def test_lead_and_contact(name, expected):
    assert m.lead_and_contact(name) == expected


def test_as_recorded():
    rows = m.as_recorded(["a", "b"])
    assert [r.label for r in rows] == ["a", "b"] and all(r.minus is None for r in rows)


def test_referenced_hides_reference_and_notes_other_rates():
    rate = {"LA1": 256.0, "LA2": 256.0, "Cz": 256.0, "ECG": 512.0}
    rows = m.referenced(["LA1", "Cz", "LA2", "ECG"], "Cz", rate.__getitem__)
    assert [r.label for r in rows] == ["LA1 − Cz", "LA2 − Cz", "ECG"]
    assert rows[2].minus is None and rows[2].note == "not re-referenced (different rate)"
    with pytest.raises(ValueError):
        m.referenced(["a"], "zz", lambda n: 1.0)


def test_bipolar_pairs_and_natural_order():
    names = ["LA10", "LA2", "LA1", "LA3", "ECG", "RA1", "RA2"]
    rows = m.lead_bipolar(names)
    assert [r.label for r in rows] == [
        "LA1 − LA2",
        "LA2 − LA3",
        "LA10",
        "ECG",
        "RA1 − RA2",
    ]


def test_bipolar_gap_not_paired_and_last_contact_not_repeated():
    rows = m.lead_bipolar(["B1", "B2", "B4", "B5"])
    assert [r.label for r in rows] == ["B1 − B2", "B4 − B5"]


def test_bipolar_duplicates_not_paired():
    rows = m.lead_bipolar(["C1", "C_1", "C2"])
    assert all(r.minus is None for r in rows)
    assert len({r.label for r in rows}) == 3


def test_bipolar_prime_forms_share_lead_and_labels_unique():
    rows = m.lead_bipolar(["A1'", "A'2"])
    assert [(r.plus, r.minus) for r in rows] == [("A1'", "A'2")]


def test_bipolar_different_rate_pair_left_alone():
    rate = {"D1": 256.0, "D2": 512.0}
    rows = m.lead_bipolar(["D1", "D2"], rate.__getitem__)
    assert [r.label for r in rows] == ["D1", "D2"]


def test_apply_nan_propagates_and_required():
    data = {
        "a": np.array([1, 2, np.nan, 4], np.float32),
        "b": np.array([1, np.nan, 1, 1], np.float32),
    }
    rows = [m.Row("a", "a"), m.Row("a − b", "a", "b")]
    out = m.apply(rows, data)
    assert out.dtype == np.float32
    np.testing.assert_array_equal(out[0], data["a"])
    assert out[1, 0] == 0 and np.isnan(out[1, 1]) and np.isnan(out[1, 2]) and out[1, 3] == 3
    assert m.required_channels(rows) == ["a", "b"]
    assert m.apply([], data).shape[0] == 0


def test_already_bipolar_names_stay_as_recorded():
    assert m.lead_and_contact("F3-C3") == ("F3-C3", None)
    assert m.lead_and_contact("C3-C4") == ("C3-C4", None)
    assert m.lead_and_contact("LA-3") == ("LA", 3)
    rows = m.lead_bipolar(["F3-C3", "C3-C4", "LA1", "LA2"])
    assert [r.label for r in rows] == ["F3-C3", "C3-C4", "LA1 \u2212 LA2"]


def test_labels_unique_against_existing_labels_in_every_montage():
    names = ["X", "X (2)", "X "]
    for rows in (
        m.as_recorded(["X", "X", "X (2)"]),
        m.lead_bipolar(["X", "X", "X (2)", "Y1", "Y1"]),
        m.referenced(["R", "A", "A"], "R", lambda n: 1.0),
    ):
        labels = [r.label for r in rows]
        assert len(set(labels)) == len(labels)
    assert names  # keeps the list used


def test_apply_errors_name_the_row_not_the_data():
    rows = [m.Row("LA1 \u2212 LA2", "LA1", "LA2")]
    with pytest.raises(ValueError, match="LA1 \u2212 LA2"):
        m.apply(rows, {"LA1": np.zeros(4, np.float32)})
    with pytest.raises(ValueError, match="different length"):
        m.apply(rows, {"LA1": np.zeros(4, np.float32), "LA2": np.zeros(3, np.float32)})
