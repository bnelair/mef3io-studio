"""What the viewer shows on top of the signals, without Qt: the recording
band, notes and events (their colours, types, search, stepping), the
banner text, and the export range (docs/design/viewer.md, "Knowing which
recording is which", "Annotations", "Export range").

Privacy: a note's text is identifying. Nothing here reads a name or a text
unless the caller says the recording is shown (``shown``), and nothing here
logs.
"""

from __future__ import annotations

import bisect
import csv
import io
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import tzinfo

from . import timefmt

# --- the recording band ---------------------------------------------------------


def band_spans(
    coverage: Sequence[tuple[int, int, int]], t0: int, t1: int
) -> list[tuple[int, int, tuple[int, ...]]]:
    """``(a, b, recordings)`` pieces of [t0, t1) in order, each with the set
    of recordings that hold data in it (empty = none). ``coverage``:
    ``(start, end, recording)`` spans."""
    if t1 <= t0:
        return []
    cuts = {t0, t1}
    for a, b, _ in coverage:
        for p in (a, b):
            if t0 < p < t1:
                cuts.add(p)
    points = sorted(cuts)
    out: list[tuple[int, int, tuple[int, ...]]] = []
    for a, b in zip(points[:-1], points[1:], strict=True):
        mid = (a + b) / 2
        who = tuple(sorted({r for s, e, r in coverage if s <= mid < e}))
        if out and out[-1][2] == who:
            out[-1] = (out[-1][0], b, who)
        else:
            out.append((a, b, who))
    return out


def letters_of(indices: Iterable[int], letter: Callable[[int], str]) -> list[str]:
    return [letter(i) for i in sorted(set(indices))]


def join_letters(letters: Sequence[str]) -> str:
    """ "A", "A and B", "A, B and C"."""
    if len(letters) <= 1:
        return "".join(letters)
    return ", ".join(letters[:-1]) + " and " + letters[-1]


def banner_text(letters: Sequence[str], name: str | None = None) -> str:
    """The banner when the recordings on the page change. ``name``: the name
    of the single recording, or None while it is hidden (or with several)."""
    if not letters:
        return "No recording on this page"
    if len(letters) == 1:
        tail = f"recording {letters[0]}" + (f": {name}" if name else "")
        return f"Now showing {tail}"
    return f"Now showing recordings {join_letters(letters)}"


def gap_text(before: Sequence[str], after: Sequence[str], gap_us: int | None) -> str:
    """The label of a boundary line: "A to B: gap 2 min 30 s", "A to B: no
    gap", or, where recordings share a period, what starts or ends."""
    a, b = join_letters(before), join_letters(after)
    if gap_us is None:
        gained = [x for x in after if x not in before]
        lost = [x for x in before if x not in after]
        if gained and not lost:
            return f"{join_letters(gained)} starts"
        if lost and not gained:
            return f"{join_letters(lost)} ends"
        return f"{a} to {b}"
    if gap_us <= 0:
        return f"{a} to {b}: no gap"
    return f"{a} to {b}: gap {timefmt.format_span(gap_us / 1e6)}"


# --- notes and events ----------------------------------------------------------------------

#: The known record types: code -> (plain name, colour on light, on dark).
#: Fixed. Unknown types are grey. The colours avoid the page/cursor blue.
MEF_TYPES: dict[str, tuple[str, str, str]] = {
    "Note": ("Note", "#6d28d9", "#a78bfa"),
    "EDFA": ("Event", "#a16207", "#facc15"),
    "Seiz": ("Seizure", "#dc2626", "#f87171"),
    "SyLg": ("System message", "#475569", "#94a3b8"),
    "Epoc": ("Time marker", "#db2777", "#f472b6"),
    "Curs": ("Cursor", "#0891b2", "#22d3ee"),
    "CSti": ("Cortical stimulation", "#ea580c", "#fb923c"),
    "ESti": ("Electrical stimulation", "#4338ca", "#818cf8"),
    "LNTP": ("Line-noise template", "#65a30d", "#a3e635"),
}
UNKNOWN_TYPE = ("Other", "#6b7280", "#9ca3af")

#: The app's quick types (the start of the text: "Spike: ..."), with colours.
QUICK_TYPES: dict[str, tuple[str, str]] = {
    "Seizure onset": ("#dc2626", "#f87171"),
    "Seizure end": ("#9f1239", "#fb7185"),
    "Spike": ("#ea580c", "#fb923c"),
    "Artifact": ("#78716c", "#a8a29e"),
    "Sleep": ("#4f46e5", "#a5b4fc"),
}

TYPE_PREFIX = "type:"
QUICK_PREFIX = "quick:"
HIDDEN_TEXT = "(hidden)"


def quick_label(text: str | None) -> str | None:
    """The quick type a note's text starts with ("Spike: ..."), or None."""
    if not text or ":" not in text:
        return None
    head = text.split(":", 1)[0].strip().casefold()
    for name in QUICK_TYPES:
        if name.casefold() == head:
            return name
    return None


def kind_of(code: str, text: str | None, text_known: bool) -> tuple[str, str]:
    """``(key, plain label)`` of a record: its quick type when the text says
    one (and may be read), else its record type."""
    quick = quick_label(text) if text_known else None
    if quick:
        return QUICK_PREFIX + quick, quick
    return TYPE_PREFIX + code, MEF_TYPES.get(code, UNKNOWN_TYPE)[0]


def kind_colour(key: str, dark: bool) -> str:
    i = 2 if dark else 1
    if key.startswith(QUICK_PREFIX):
        pair = QUICK_TYPES.get(key[len(QUICK_PREFIX) :])
        if pair:
            return pair[1 if dark else 0]
    code = key[len(TYPE_PREFIX) :] if key.startswith(TYPE_PREFIX) else ""
    return MEF_TYPES.get(code, UNKNOWN_TYPE)[i]


@dataclass(frozen=True)
class Mark:
    """One note or event, ready to show. ``text`` is identifying: read it
    only for a recording that is shown."""

    time: int
    end: int | None
    code: str
    recording: int
    level: str
    channel: str | None
    text: str = field(repr=False, default="")

    @property
    def is_span(self) -> bool:
        return self.end is not None and self.end > self.time


def mark_end(rec: dict) -> int | None:
    """When a record with a duration ends (EDFA duration, Seiz span)."""
    t = int(rec["time"])
    dur = rec.get("duration")
    if isinstance(dur, (int, float)) and dur > 0:
        return t + int(dur)
    off = rec.get("latest_offset")
    if isinstance(off, (int, float)) and off > t:
        return int(off)
    return None


def build_marks(annotations: Iterable[dict]) -> list[Mark]:
    """Marks from the viewer data's records (each with ``recording``), sorted
    by time."""
    marks = []
    for rec in annotations:
        text = rec.get("text")
        marks.append(
            Mark(
                int(rec["time"]),
                mark_end(rec),
                str(rec.get("type") or ""),
                int(rec["recording"]),
                str(rec.get("level") or ""),
                rec.get("channel"),
                text if isinstance(text, str) else "",
            )
        )
    marks.sort(key=lambda m: (m.time, m.recording, m.channel or ""))
    return marks


def kind_for(mark: Mark, shown: bool) -> tuple[str, str]:
    """The type key and plain label of a mark; the text counts only when its
    recording is ``shown``."""
    return kind_of(mark.code, mark.text, shown)


def type_counts(marks: Sequence[Mark], shown: Callable[[int], bool]) -> list[tuple[str, str, int]]:
    """``(key, label, how many)`` for every type present, most first."""
    counts: dict[str, list] = {}
    for m in marks:
        key, label = kind_for(m, shown(m.recording))
        entry = counts.setdefault(key, [label, 0])
        entry[1] += 1
    return sorted(((k, v[0], v[1]) for k, v in counts.items()), key=lambda x: (-x[2], x[1]))


def visible(
    marks: Sequence[Mark], hidden_kinds: frozenset[str], shown: Callable[[int], bool]
) -> list[Mark]:
    return [m for m in marks if kind_for(m, shown(m.recording))[0] not in hidden_kinds]


def display_text(mark: Mark, shown: bool) -> str:
    """What the list says of a mark: its text, or "(hidden)" while its
    recording's details are hidden."""
    if not shown:
        return HIDDEN_TEXT
    return mark.text or ""


def matches(mark: Mark, query: str, shown: bool, letter: str) -> bool:
    """Search over the type, the channel, the letter and (only when the
    recording is shown) the text."""
    q = query.strip().casefold()
    if not q:
        return True
    _, label = kind_for(mark, shown)
    hay = [label, letter, mark.channel or ""]
    if shown:
        hay.append(mark.text)
    return any(q in h.casefold() for h in hay)


def step_time(times: Sequence[int], centre: float, direction: int, eps: float = 1.0) -> int | None:
    """The next time after ``centre`` (direction 1) or the previous before it
    (-1) in the sorted ``times``, or None."""
    if direction > 0:
        i = bisect.bisect_right(times, centre + eps)
        return times[i] if i < len(times) else None
    i = bisect.bisect_left(times, centre - eps)
    return times[i - 1] if i > 0 else None


# --- the export range --------------------------------------------------------------------


def ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def snap(t: float, anchor: int, rate: float) -> int:
    """``t`` moved to the nearest sample time of a grid that starts at
    ``anchor`` and runs at ``rate`` Hz."""
    if rate <= 0:
        return int(round(t))
    step = 1e6 / rate
    return int(round(anchor + round((t - anchor) / step) * step))


def range_length_text(start: int, end: int) -> str:
    return timefmt.format_span((end - start) / 1e6)


def copy_text(start: int, end: int, zone: tzinfo) -> str:
    """The text "Copy times" puts on the clipboard: both ends in the site
    time zone with the offset, and in microseconds since 1970 (UTC). Times
    identify the recording, so the text says so."""
    return (
        f"Start: {timefmt.format_time(start, zone, 3)} ({start} microseconds since 1970, UTC)\n"
        f"End: {timefmt.format_time(end, zone, 3)} ({end} microseconds since 1970, UTC)\n"
        f"Length: {range_length_text(start, end)}\n"
        "These are real dates and times. They can identify the recording."
    )


EXPORT_COLUMNS = ("number", "start", "end", "length_seconds", "start_us", "end_us", "recordings")


def export_rows(
    ranges: Sequence[tuple[int, int]],
    zone: tzinfo,
    recordings_of: Callable[[int, int], list[str]],
) -> list[dict]:
    """One row per range. ``recordings_of(start, end)``: the names of the
    recordings it covers (the caller refuses before this unless all are
    shown)."""
    return [
        {
            "number": i,
            "start": timefmt.format_time(a, zone, 3),
            "end": timefmt.format_time(b, zone, 3),
            "length_seconds": round((b - a) / 1e6, 6),
            "start_us": a,
            "end_us": b,
            "recordings": "; ".join(recordings_of(a, b)),
        }
        for i, (a, b) in enumerate(ranges, 1)
    ]


def to_csv(rows: Sequence[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=EXPORT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def to_json(rows: Sequence[dict]) -> str:
    return json.dumps(list(rows), indent=2, ensure_ascii=False) + "\n"
