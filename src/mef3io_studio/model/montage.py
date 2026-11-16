"""Viewer montages: as recorded, referenced to a channel, lead bipolar.

Pure numpy, no Qt. Spec: ``docs/design/viewer.md`` ("Montages"). A montage is a
list of :class:`Row` (a channel, or a channel minus another); :func:`apply`
computes them. Channel names can identify: nothing here logs.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
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
    head, _, tail = s.rpartition("-")
    # Already a derivation ("F3-C3", "Fp1-REF"): a contact before the dash and
    # a name after it. "LH-A1" (no digit before the dash) is lead "LH-A".
    if head[-1:].isdigit() and any(ch.isalpha() for ch in tail):
        return name, None
    m = _PRIME_AFTER.match(s)
    if m and m.group(1).strip(" _-"):
        return m.group(1).rstrip(" _-") + "'", int(m.group(2))
    m = _TRAILING.match(s)
    if m:
        lead = m.group(1).rstrip(" _-")
        if lead:
            return lead, int(m.group(2))
    return name, None


#: A name prefix is a lead only with at least this many contacts, one of them
#: contact 1 (``LA1`` ... ``LA12``): scalp names (``F3`` ``F4`` ``F7`` ``F8``,
#: ``T3``-``T6``, ``A1`` ``A2``, ``Fp1`` ``Fp2``) are not leads.
MIN_LEAD_CONTACTS = 4


def real_leads(names: Sequence[str]) -> frozenset[str]:
    """The leads among ALL recorded channels: prefixes with at least
    :data:`MIN_LEAD_CONTACTS` different contacts, contact 1 among them."""
    contacts: dict[str, set[int]] = {}
    for n in names:
        lead, contact = lead_and_contact(n)
        if contact is not None:
            contacts.setdefault(lead, set()).add(contact)
    return frozenset(
        lead
        for lead, c in contacts.items()
        if len(c) >= MIN_LEAD_CONTACTS and 1 in c and not _surely_not_eeg(lead)
    )


def lead_of(name: str, leads: Collection[str]) -> str | None:
    """The lead ``name`` belongs to (one of ``leads``), else None."""
    lead, contact = lead_and_contact(name)
    return lead if contact is not None and lead in leads else None


#: Channels that are not EEG whatever else is true of the name (``ECG1`` ... ``ECG4``,
#: ``EMG1``, ``DC3``, ``SpO2``, triggers): never a lead, always after the EEG.
SURELY_NOT_EEG = frozenset(
    "ecg ekg emg eog dc spo sao osat trig trigger resp respiration pleth ppg photic "
    "pulse airflow thorax abdomen snore etco ptt".split()
)

#: Short keys that are not EEG when alone (``HR``, ``CO``, ``TEMP``) but that a depth
#: lead may be called: they count as EEG when they form a lead (:func:`real_leads`).
AMBIGUOUS_NON_EEG = frozenset("loc roc hr co bp temp flow effort event".split())

#: Both together: channels that are not EEG, by their name without digits and signs.
NON_EEG = SURELY_NOT_EEG | AMBIGUOUS_NON_EEG

_SURE_PREFIXES = ("ecg", "ekg", "emg", "eog")


def _surely_not_eeg(name: str) -> bool:
    key = re.sub(r"[^a-z]", "", name.casefold())
    return key in SURELY_NOT_EEG or key.startswith(_SURE_PREFIXES)


def is_non_eeg(name: str, leads: Collection[str] = ()) -> bool:
    """Whether the channel is, by its name, not EEG. ECG, EMG, EOG, DC, SpO2,
    pulse, breathing and trigger channels always are. An ambiguous short name
    (``HR``, ``CO``, ``LOC``, ``TEMP`` ...) is EEG when it belongs to a lead (see
    :func:`real_leads`): a depth lead may be called ``HR`` or ``CO``."""
    if _surely_not_eeg(name):
        return True
    if lead_of(name, leads) is not None:
        return False
    return re.sub(r"[^a-z]", "", name.casefold()) in AMBIGUOUS_NON_EEG


def non_eeg_last(rows: Sequence[Row], leads: Collection[str] = ()) -> list[Row]:
    """The rows with the channels that are not EEG (ECG, EMG, DC, SpO2 ...) after
    the others, each group in its own order (judged by the row's own channel:
    ``ECG - Cz`` is an ECG row)."""
    eeg = [r for r in rows if not is_non_eeg(r.plus, leads)]
    rest = [r for r in rows if is_non_eeg(r.plus, leads)]
    return eeg + rest if rest and eeg else list(rows)


#: A run of at least this many rows of one lead is drawn as a group.
MIN_GROUP = 3
OTHER_GROUP = "\0other"


@dataclass(frozen=True)
class RowGroup:
    """How one row sits among the others: ``group`` names its run (a lead, or
    the channels that are not EEG), ``first`` marks the start of a run that is
    drawn as a group (a thin line above it and the lead named once), ``short``
    is the label without the lead's name for the rows after the first ("" = the
    label as it is)."""

    group: str = ""
    first: bool = False
    short: str = ""


def group_rows(rows: Sequence[Row], leads: Collection[str] | None = None) -> list[RowGroup]:
    """Group consecutive rows of one lead (``LA1``, ``LA2`` ...); a run of fewer
    than :data:`MIN_GROUP` rows is not a group and keeps its labels. ``leads`` are
    the real leads among ALL recorded channels (:func:`real_leads`; None = judged
    from these rows only). Rows of channels that are not EEG form one group at the
    end. Only a row that is a channel as recorded is shortened: a derivation
    (``LA2 − LA3``, ``LA2 − Cz``) always keeps its full label."""
    if leads is None:
        leads = real_leads([r.plus for r in rows])
    keys: list[str] = []
    for r in rows:
        if is_non_eeg(r.plus, leads):
            keys.append(OTHER_GROUP)
        else:
            keys.append(lead_of(r.plus, leads) or "")
    out = [RowGroup(k) for k in keys]
    i = 0
    while i < len(rows):
        j = i
        while j < len(rows) and keys[j] == keys[i]:
            j += 1
        if keys[i] and j - i >= (1 if keys[i] == OTHER_GROUP else MIN_GROUP):
            for k in range(i, j):
                short = ""
                if (
                    keys[i] != OTHER_GROUP
                    and k > i
                    and rows[k].minus is None
                    and rows[k].label == rows[k].plus
                    and rows[k].label.startswith(keys[i])
                ):
                    rest = rows[k].label[len(keys[i]) :].lstrip(" _-")
                    short = rest if rest and not rest[0].isalpha() else ""
                out[k] = RowGroup(keys[i], k == i, short)
        else:
            out[i:j] = [RowGroup("") for _ in range(j - i)]
        i = j
    return out


def natural_key(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in _NATURAL.split(name)]


def as_recorded(names: Sequence[str]) -> list[Row]:
    return _unique([Row(n, n) for n in names])


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
    return _unique(rows)


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
    """Make labels unique, also against labels that exist elsewhere in the list."""
    reserved = {r.label for r in rows}
    used: set[str] = set()
    out = []
    for r in rows:
        label = r.label
        if label in used:
            k = 2
            while f"{r.label} ({k})" in reserved or f"{r.label} ({k})" in used:
                k += 1
            label = f"{r.label} ({k})"
            r = Row(label, r.plus, r.minus, r.note)
        used.add(label)
        out.append(r)
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

    Returns float32 ``[len(rows), n]``. NaN in either channel gives NaN. A row
    whose channel is missing, or arrays of different lengths, raise ValueError
    (naming the row label, never the data)."""
    if not rows:
        return np.zeros((0, 0), dtype=np.float32)
    arrays = []
    for r in rows:
        for name in (r.plus, r.minus):
            if name is not None and name not in data:
                raise ValueError(f"No data for the row '{r.label}'.")
        arrays.append((np.asarray(data[r.plus], dtype=np.float32), r))
    n = max(
        [a.shape[0] for a, _ in arrays]
        + [np.shape(data[r.minus])[0] for r in rows if r.minus is not None]
    )
    out = np.empty((len(rows), n), dtype=np.float32)
    for i, (a, r) in enumerate(arrays):
        b = None if r.minus is None else np.asarray(data[r.minus], dtype=np.float32)
        if a.shape[0] != n or (b is not None and b.shape[0] != n):
            raise ValueError(f"The row '{r.label}' has a different length from the others.")
        out[i] = a if b is None else a - b
    return out
