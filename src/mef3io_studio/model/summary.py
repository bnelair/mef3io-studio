""" "Copy summary": a recording described as text or JSON, for an e-mail or a
data-management log.

It is a user-initiated export, so it follows the masking the user chose:
**names and dates are hidden** (the recording's name, its start and end, the
patient details and the text and times of notes) unless the user chose
"Show" for that recording. Hidden values read ``[hidden]``; they are never
included in any form. Durations, channels, rates and sizes are kept: they
do not identify anyone.

Plain Python, no Qt.
"""

from __future__ import annotations

import json
from datetime import tzinfo

from .problems import CheckReport
from .recording import (
    ACCESS_BASIC,
    ACCESS_FULL,
    OFFSET_DISPUTED,
    InspectOutcome,
    note_kind_label,
    operation_words,
)
from .timefmt import format_size, format_span, format_time, zone_label

HIDDEN = "[hidden]"

DATE_OFFSET_NOTE = (
    "This recording hides its real date in a way that can be undone; treat it as identifiable."
)
UNVERIFIED_NOTE = (
    "The times may not be the real ones: the part of the recording that confirms "
    "them needs the full-access password."
)
UNVERIFIED_OFFSET_NOTE = (
    "The times may not be the real ones: older programs stored times in recordings "
    "like this one in two different ways, and the file does not say which."
)


UNVERIFIED_DISPUTED_NOTE = (
    "The times may not be the real ones: different parts of this recording say "
    "different things about how their times are stored."
)
LEGACY_PASSWORD_NOTE = (
    "This password works here because the recording was protected by an older version "
    "of the mef3io library. Other programs may not accept this password for this "
    "recording. Nothing is wrong with the recording."
)


def unverified_note(details) -> str:
    if details.time_offset_status == OFFSET_DISPUTED:
        return UNVERIFIED_DISPUTED_NOTE
    return UNVERIFIED_OFFSET_NOTE if details.recording_time_offset else UNVERIFIED_NOTE


def _access_words(access: str) -> str:
    if access == ACCESS_FULL:
        return "yes (opened with the full-access password)"
    if access == ACCESS_BASIC:
        return "yes (opened with the basic password; patient details locked)"
    return "no"


def build_summary(
    outcome: InspectOutcome,
    zone: tzinfo,
    show: bool = False,
    check: CheckReport | None = None,
    app_version: str = "",
    library_version: str = "",
) -> dict:
    """The summary as a dict of plain values (what JSON gets)."""

    def hide(value):
        return value if show else HIDDEN

    out: dict = {
        "recording": hide(outcome.name),
        "kind": "single file" if outcome.packed else "folder",
        "size_on_disk": format_size(outcome.size_bytes) if outcome.size_bytes else "unknown",
    }
    details = outcome.details
    if details is None:
        if outcome.needs_password:
            out["status"] = "password needed"
        else:
            out["status"] = "could not be opened"
        out["made_with"] = f"mef3io-studio {app_version}, mef3io {library_version}".strip()
        return out

    out["times_shown_in"] = zone_label(zone)
    out["start"] = hide(format_time(details.start, zone))
    out["end"] = hide(format_time(details.end, zone))
    if not details.times_verified:
        out["times_note"] = unverified_note(details)
    if details.uses_time_offset:
        out["date_note"] = DATE_OFFSET_NOTE
    out["duration"] = format_span(details.duration_seconds)
    gaps = details.gaps()
    out["gaps"] = {
        "count": len(gaps),
        "total": format_span(sum((b - a) / 1e6 for a, b in gaps)) if gaps else "",
        "longest": format_span(max((b - a) / 1e6 for a, b in gaps)) if gaps else "",
    }
    if details.parts_with_gaps_inside:
        out["gaps"]["inside_parts"] = format_span(details.missing_inside_seconds)
    out["parts"] = details.part_count
    out["sampling_rates_hz"] = list(details.sampling_rates)
    out["channel_count"] = len(details.channels)
    out["channels"] = [
        {
            "name": c.name,
            "sampling_rate_hz": c.sampling_frequency,
            "units": c.units,
            "samples": c.samples,
            "parts": len(c.parts),
        }
        for c in details.channels
    ]
    out["password_protected"] = _access_words(details.access)
    if details.legacy_password:
        out["password_note"] = LEGACY_PASSWORD_NOTE
    if details.part_problems:
        out["unreadable_parts"] = [p.where for p in details.part_problems]
    patient = details.patient
    if show:
        if patient.names_available:
            out["patient"] = {
                "first_name": patient.first_name or "",
                "last_name": patient.last_name or "",
                "id": patient.patient_id or "",
                "location": patient.location or "",
                "recording_description": patient.recording_description or "",
            }
        else:
            out["patient"] = {
                "names": "locked (needs the full-access password)",
                "recording_description": patient.recording_description or "",
            }
    else:
        out["patient"] = HIDDEN
    notes = details.notes
    if details.notes_problem is not None and not notes:
        out["notes_and_events"] = {"count": "could not be read"}
    elif details.notes_problem is not None or details.notes_without_times:
        out["notes_and_events"] = {"count": f"{len(notes)}, some could not be read"}
    else:
        out["notes_and_events"] = {"count": len(notes)}
    if show:
        out["notes_and_events"]["items"] = [
            {
                "time": format_time(n.time, zone),
                "stored_with": n.where,
                "kind": note_kind_label(n.kind),
                "text": "locked (needs the full-access password)" if n.locked else n.text,
            }
            for n in notes
        ]
    histories = details.history_groups()
    out["history"] = [
        {
            "parts": parts,
            "created_by": f"mef3io {h.created_by}" if h else "another program",
            "last_changed_by": f"mef3io {h.last_modified_by}" if h else "",
            "operations": [operation_words(op) for op in h.operations] if h else [],
        }
        for h, parts in histories
    ]
    if check is not None:
        out["check_for_problems"] = check.short_status()
    out["made_with"] = f"mef3io-studio {app_version}, mef3io {library_version}".strip()
    return out


def as_json(summaries: list[dict]) -> str:
    data = summaries[0] if len(summaries) == 1 else summaries
    return json.dumps(data, indent=2, ensure_ascii=False)


_LABELS = {
    "item": "Item",
    "recording": "Recording",
    "kind": "Kind",
    "size_on_disk": "Size on disk",
    "status": "Status",
    "times_shown_in": "Times shown in",
    "start": "Start",
    "end": "End",
    "times_note": "Note on times",
    "date_note": "Note on the date",
    "duration": "Duration",
    "gaps": "Gaps",
    "parts": "Parts",
    "sampling_rates_hz": "Sampling rates (Hz)",
    "channel_count": "Channels",
    "channels": "Channel list",
    "password_protected": "Password-protected",
    "password_note": "Note on the password",
    "unreadable_parts": "Parts that could not be read",
    "patient": "Patient details",
    "notes_and_events": "Notes and events",
    "history": "History",
    "check_for_problems": "Check for problems",
    "made_with": "Made with",
}


def _text_value(key: str, value) -> list[str]:
    if key == "gaps" and isinstance(value, dict):
        if not value["count"]:
            text = "none"
        else:
            text = f"{value['count']}, {value['total']} in total, longest {value['longest']}"
        if value.get("inside_parts"):
            text += f"; about {value['inside_parts']} missing inside parts"
        return [text]
    if key == "channels":
        return [
            f"  {c['name']}: {c['sampling_rate_hz']:g} Hz, {c['units'] or 'no units'}, "
            f"{c['samples']} samples, {c['parts']} part(s)"
            for c in value
        ]
    if key == "unreadable_parts" and isinstance(value, list):
        return [f"  {where}" for where in value]
    if key == "sampling_rates_hz":
        return [", ".join(f"{r:g}" for r in value)]
    if key == "patient" and isinstance(value, dict):
        return [f"  {k.replace('_', ' ')}: {v}" for k, v in value.items()]
    if key == "notes_and_events" and isinstance(value, dict):
        lines = [str(value["count"])]
        for item in value.get("items", []):
            lines.append(f"  {item['time']} ({item['stored_with']}) {item['kind']}: {item['text']}")
        return lines
    if key == "history" and isinstance(value, list):
        lines = []
        for item in value:
            text = f"  {item['parts']} part(s): created by {item['created_by']}"
            if item["last_changed_by"]:
                text += f", last changed by {item['last_changed_by']}"
            if item["operations"]:
                text += f" ({', '.join(item['operations'])})"
            lines.append(text)
        return lines
    return [str(value)]


def as_text(summaries: list[dict]) -> str:
    blocks = []
    for summary in summaries:
        lines = []
        for key, value in summary.items():
            label = _LABELS.get(key, key)
            rendered = _text_value(key, value)
            if len(rendered) == 1 and not rendered[0].startswith("  "):
                lines.append(f"{label}: {rendered[0]}")
            else:
                head, rest = (
                    (rendered[0], rendered[1:])
                    if rendered and not rendered[0].startswith("  ")
                    else ("", rendered)
                )
                lines.append(f"{label}: {head}".rstrip())
                lines += rest
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"
