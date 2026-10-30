"""Viewer montages: as recorded, referenced to a channel, lead bipolar.

Pure numpy, no Qt. Spec: ``docs/design/viewer.md`` ("Montages"). A montage is a
list of :class:`Row` (a channel, or a channel minus another); :func:`apply`
computes them. Channel names can identify: nothing here logs.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

MINUS = "−"
DIFFERENT_RATE_NOTE = "not re-referenced (different rate)"

_TRAILING = re.compile(r"^(.*?)[\s_\-]*(\d+)$")
_PRIME_AFTER = re.compile(r"^(.*?)(\d+)'$")
_NATURAL = re.compile(r"(\d+)")


@dataclass(frozen=True)
class Row:
    """One displayed trace: ``plus`` minus ``minus`` (None = as recorded)."""

    label: str
    plus: str
    minus: str | None = None
    note: str | None = None


def lead_and_contact(name: str) -> tuple[str, int | None]:
    """Split ``LA3`` / ``LA_3`` / ``LA-3`` into ``("LA", 3)``.

    ``A1'`` and ``A'1`` both give ``("A'", 1)``. A name without a trailing
    contact number (or made only of one) is its own lead with contact None.
    """
    s = name.strip()
    m = _PRIME_AFTER.match(s)
    if m and m.group(1).strip(" _-"):
        return m.group(1).rstrip(" _-") + "'", int(m.group(2))
    m = _TRAILING.match(s)
    if m:
        lead = m.group(1).rstrip(" _-")
        if lead:
            return lead, int(m.group(2))
    return name, None


def natural_key(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in _NATURAL.split(name)]


def as_recorded(names: Sequence[str]) -> list[Row]:
    return [Row(n, n) for n in names]


def referenced(names: Sequence[str], ref: str, rate_of: Callable[[str], float]) -> list[Row]:
    """Every channel minus ``ref``; the reference row itself is left out.

    Channels at another rate than the reference stay as recorded, noted."""
    if ref not in names:
        raise ValueError("the reference channel is not in the list")
    ref_rate = rate_of(ref)
    rows = []
    for n in names:
        if n == ref:
            continue
        if rate_of(n) != ref_rate:
            rows.append(Row(n, n, None, DIFFERENT_RATE_NOTE))
        else:
            rows.append(Row(f"{n} {MINUS} {ref}", n, ref))
    return rows


def lead_bipolar(names: Sequence[str], rate_of: Callable[[str], float] | None = None) -> list[Row]:
    """Pair each contact with the next contact number on the same lead.

    Gaps and duplicated numbers are not paired; unpaired and unnumbered
    channels stay as recorded. Leads keep the order they first appear in,
    contacts are sorted by number inside a lead. Labels are made unique.
    """
    order: list[str] = []
    leads: dict[str, list[tuple[int, str]]] = {}
    plain: dict[str, str] = {}
    for n in names:
        lead, contact = lead_and_contact(n)
        key = lead if contact is not None else "\0" + n
        if key not in leads:
            leads[key] = []
            order.append(key)
        if contact is None:
            plain[key] = n
        else:
            leads[key].append((contact, n))
    rows: list[Row] = []
    for key in order:
        if key in plain:
            rows.append(Row(plain[key], plain[key]))
            continue
        items = sorted(leads[key], key=lambda t: (t[0], natural_key(t[1])))
        count: dict[int, int] = {}
        for c, _ in items:
            count[c] = count.get(c, 0) + 1
        by_contact = {c: n for c, n in items if count[c] == 1}
        used_as_minus: set[str] = set()
        for c, n in items:
            nxt = by_contact.get(c + 1)
            if (
                count[c] == 1
                and nxt is not None
                and (rate_of is None or rate_of(n) == rate_of(nxt))
            ):
                rows.append(Row(f"{n} {MINUS} {nxt}", n, nxt))
                used_as_minus.add(nxt)
            elif n not in used_as_minus:
                rows.append(Row(n, n))
        # a contact that was only the minus of a pair is shown through that pair
    return _unique(rows)


def _unique(rows: list[Row]) -> list[Row]:
    seen: dict[str, int] = {}
    out = []
    for r in rows:
        k = seen.get(r.label, 0) + 1
        seen[r.label] = k
        if k == 1:
            out.append(r)
        else:
            out.append(Row(f"{r.label} ({k})", r.plus, r.minus, r.note))
    return out


def required_channels(rows: Sequence[Row]) -> list[str]:
    """Channel names the rows need, once each, in first-use order."""
    out: dict[str, None] = {}
    for r in rows:
        out[r.plus] = None
        if r.minus is not None:
            out[r.minus] = None
    return list(out)


def apply(rows: Sequence[Row], data: Mapping[str, np.ndarray]) -> np.ndarray:
    """Compute the rows from ``data`` (name -> equal-length 1-D arrays).

    Returns float32 ``[len(rows), n]``. NaN in either channel gives NaN."""
    if not rows:
        return np.zeros((0, 0), dtype=np.float32)
    n = len(data[rows[0].plus])
    out = np.empty((len(rows), n), dtype=np.float32)
    for i, r in enumerate(rows):
        a = np.asarray(data[r.plus], dtype=np.float32)
        out[i] = a if r.minus is None else a - np.asarray(data[r.minus], dtype=np.float32)
    return out
