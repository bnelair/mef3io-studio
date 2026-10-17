"""The privacy check: does a recording still hold anything that identifies the
patient? (mef3io-studio#5, "Privacy check" card.)

Built ONLY from what the Explorer already read with ``mef3io.inspect()``
(:class:`~.recording.RecordingDetails`): no signal, no extra file. For any
recording, original or copy, it answers item by item:

* patient names and ID; the recording location; the recording, channel and
  reference descriptions; the recording's name (the pseudonym, in a copy);
  the text of notes and events; folder and file names; the time-zone
  details; the date-hiding offset (meflib's recording-time offset); other
  programs' private areas in the files;

and whether the recording was **made by the anonymizer**: every part's files
carry mef3io's anonymize stamp (history operation "anonymize", code 6). Only
then are its dates known to be moved, and only then are the private areas
known to be cleared (inspect cannot see them).

Values are returned so the UI can show them after "Show"; never log them.

Plain Python, no Qt.
"""

from __future__ import annotations

import enum
import unicodedata
from dataclasses import dataclass, field

from .recording import OFFSET_DISPUTED, OFFSET_LOCKED, OFFSET_NONE, RecordingDetails


class Mark(enum.StrEnum):
    EMPTY = "empty"  # ✓ nothing there
    FOUND = "found"  # ✗ holds something that may identify the patient
    KEPT = "kept"  # ! note text kept on purpose when the copy was made
    INFO = "info"  # shown for reference (the copy's name)
    UNKNOWN = "unknown"  # – cannot be told from here (locked, or not checkable)


SYMBOLS = {
    Mark.EMPTY: "✓",
    Mark.FOUND: "✗",
    Mark.KEPT: "!",
    Mark.INFO: "✓",
    Mark.UNKNOWN: "–",
}


#: Rows that describe identifying content: while any of them could not be
#: checked, the card never says "no patient details found".
IDENTITY_KEYS = frozenset({"patient", "location", "descriptions", "notes", "zone", "offset"})


@dataclass(frozen=True)
class CheckItem:
    key: str
    label: str
    mark: Mark
    #: One plain sentence about it.
    summary: str
    #: The values found (identifying: shown only after "Show").
    values: tuple[str, ...] = field(default=(), repr=False)
    #: A value that is safe to show without "Show" (the pseudonym of a copy).
    visible_value: str = field(default="", repr=False)


@dataclass(frozen=True)
class PrivacyCheck:
    items: tuple[CheckItem, ...]
    made_by_anonymizer: bool
    #: Some parts were anonymized but the recording was changed afterwards
    #: (data added, parts added, or some could not be read).
    partly_anonymizer: bool

    @property
    def found(self) -> tuple[CheckItem, ...]:
        return tuple(i for i in self.items if i.mark is Mark.FOUND)

    @property
    def clean(self) -> bool:
        """Nothing found AND everything that identifies could be checked."""
        return not self.found and not self.unchecked

    @property
    def unchecked(self) -> tuple[CheckItem, ...]:
        """Identity rows that could not be checked (locked, or unreadable)."""
        return tuple(i for i in self.items if i.key in IDENTITY_KEYS and i.mark is Mark.UNKNOWN)

    @property
    def state(self) -> str:
        """ "bad" (something found), "unknown" (nothing found, but some rows
        could not be checked: never shown as green) or "good"."""
        if self.found:
            return "bad"
        return "unknown" if self.unchecked else "good"

    @property
    def kept_text(self) -> bool:
        return any(i.mark is Mark.KEPT for i in self.items)

    def item(self, key: str) -> CheckItem:
        return next(i for i in self.items if i.key == key)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _values(details: RecordingDetails, *names: str) -> tuple[tuple[str, ...], bool | None]:
    """(values, present) over several identity fields; present None when
    one of them could not be read and none of them holds anything."""
    values: list[str] = []
    unknown = False
    for name in names:
        fact = details.identity_fact(name)
        if fact is None:
            continue
        if fact.present is None:
            unknown = True
        values += [v for v in fact.values if v.strip()]
        if fact.present and not fact.values:
            values.append("")  # set, but its text was not returned
    values = list(dict.fromkeys(values))
    if values:
        return tuple(v for v in values if v), True
    return (), (None if unknown else False)


def _patient_words(details: RecordingDetails) -> list[str]:
    """The identifying values this recording holds, to look for in names."""
    words: list[str] = []
    for name in ("subject_name_1", "subject_name_2", "subject_id", "recording_location"):
        fact = details.identity_fact(name)
        if fact is not None:
            words += [v.strip() for v in fact.values if len(v.strip()) >= 3]
    return words


def _identity_item(
    details: RecordingDetails, key: str, label: str, names: tuple[str, ...], what: str
) -> CheckItem:
    values, present = _values(details, *names)
    if present is None:
        return CheckItem(key, label, Mark.UNKNOWN, "Locked: needs the full-access password.")
    if present:
        return CheckItem(key, label, Mark.FOUND, f"Holds {what}.", values)
    return CheckItem(key, label, Mark.EMPTY, "Empty.")


def build_check(details: RecordingDetails) -> PrivacyCheck:
    anonymizer = details.made_by_anonymizer
    partly = details.changed_after_anonymizing
    items: list[CheckItem] = []
    items.append(
        _identity_item(
            details,
            "patient",
            "Patient names and ID",
            ("subject_name_1", "subject_name_2", "subject_id"),
            "a name or ID",
        )
    )
    items.append(
        _identity_item(
            details, "location", "Recording location", ("recording_location",), "a location"
        )
    )
    items.append(
        _identity_item(
            details,
            "descriptions",
            "Recording and channel descriptions",
            ("session_description", "channel_description", "reference_description"),
            "description text",
        )
    )

    # The recording's name, and the name stored inside it (the pseudonym).
    stored, _ = _values(details, "anonymized_name")
    words = [_fold(w) for w in _patient_words(details)]
    own_names = [details.name, *(_values(details, "session_name")[0]), *stored]
    name_hits = [n for n in own_names if any(w in _fold(n) for w in words)]
    if name_hits:
        items.append(
            CheckItem(
                "name",
                "The recording's name",
                Mark.FOUND,
                "Contains a patient detail found in this recording.",
                tuple(dict.fromkeys(name_hits)),
            )
        )
    elif anonymizer:
        pseudonym = stored[0] if stored else details.name
        items.append(
            CheckItem(
                "name",
                "The recording's name",
                Mark.INFO,
                "Set by the anonymizer.",
                visible_value=f"{details.name} (pseudonym: {pseudonym})",
            )
        )
    else:
        items.append(
            CheckItem(
                "name",
                "The recording's name",
                Mark.EMPTY,
                "No patient detail found in it. Check it yourself: the app can only look "
                "for details stored in the recording.",
                tuple(dict.fromkeys(n for n in own_names if n)),
            )
        )

    # Note text.
    text = details.identity_fact("record_text")
    count = text.occurrences if text is not None else 0
    if text is not None and text.present is None:
        items.append(
            CheckItem(
                "notes",
                "Text of notes and events",
                Mark.UNKNOWN,
                "Locked: needs the full-access password.",
            )
        )
    elif text is not None and text.present:
        what = f"{count} note{'s' if count != 1 else ''} with text"
        if anonymizer:
            items.append(
                CheckItem(
                    "notes",
                    "Text of notes and events",
                    Mark.KEPT,
                    f"Kept, as chosen when the copy was made ({what}). Read it before "
                    "sharing: dates and other people's names in free text are not found "
                    "automatically.",
                    text.values,
                )
            )
        else:
            items.append(
                CheckItem(
                    "notes",
                    "Text of notes and events",
                    Mark.FOUND,
                    f"Holds text ({what}).",
                    text.values,
                )
            )
    else:
        items.append(
            CheckItem(
                "notes",
                "Text of notes and events",
                Mark.EMPTY,
                "Removed." if anonymizer else "No note text.",
            )
        )

    # Folder and file names: the recording's folder and the channel names.
    file_names = [details.name, *(c.name for c in details.channels)]
    hits = [n for n in file_names if any(w in _fold(n) for w in words)]
    if hits:
        items.append(
            CheckItem(
                "files",
                "Folder and file names",
                Mark.FOUND,
                "Contain a patient detail found in this recording.",
                tuple(dict.fromkeys(hits)),
            )
        )
    else:
        items.append(
            CheckItem(
                "files",
                "Folder and file names",
                Mark.EMPTY,
                "Named by the anonymizer; no patient detail found in them."
                if anonymizer
                else "No patient detail found in them.",
            )
        )

    # Time-zone details: a location hint.
    if details.gmt_offset is None or details.dst_set is None:
        items.append(
            CheckItem(
                "zone",
                "Time-zone details",
                Mark.UNKNOWN,
                "Locked: needs the full-access password.",
            )
        )
    elif details.gmt_offset != 0 or details.dst_set:
        items.append(
            CheckItem(
                "zone",
                "Time-zone details",
                Mark.FOUND,
                "Set: they hint at where the recording was made.",
                ("time zone and daylight-saving details are set",),
            )
        )
    else:
        items.append(CheckItem("zone", "Time-zone details", Mark.EMPTY, "Cleared."))

    # The date-hiding offset (meflib's recording-time offset).
    status = details.time_offset_status
    if status == OFFSET_NONE:
        items.append(CheckItem("offset", "Date-hiding offset", Mark.EMPTY, "None."))
    elif status == OFFSET_LOCKED:
        items.append(
            CheckItem(
                "offset",
                "Date-hiding offset",
                Mark.UNKNOWN,
                "Locked: needs the full-access password.",
            )
        )
    else:
        items.append(
            CheckItem(
                "offset",
                "Date-hiding offset",
                Mark.FOUND,
                "Set: the real date can be worked out from the files."
                if status != OFFSET_DISPUTED
                else "Set, and its parts disagree: the real date can be worked out.",
                ("an older date-hiding offset is set",),
            )
        )

    # Other programs' private areas: inspect cannot see them; the
    # anonymizer clears them in every file it writes.
    if anonymizer:
        items.append(
            CheckItem(
                "private",
                "Other programs' private areas",
                Mark.EMPTY,
                "Cleared by the anonymizer.",
            )
        )
    else:
        items.append(
            CheckItem(
                "private",
                "Other programs' private areas",
                Mark.UNKNOWN,
                "Cannot be checked here. Only a copy made by the anonymizer is known to "
                "have them cleared.",
            )
        )
    return PrivacyCheck(tuple(items), anonymizer, partly)


UNCHECKED = "Some details could not be checked: open it with the full-access password"


def headline(check: PrivacyCheck) -> str:
    found = len(check.found)
    if found:
        return f"Found {found} detail{'s' if found != 1 else ''} that may identify the patient"
    if check.unchecked:
        return UNCHECKED
    return "No patient details found in this recording"


NOT_MADE_BY_ANONYMIZER = (
    "This recording wasn't made by the anonymizer, so we can't confirm its dates were moved."
)
MOVED_DATES = "These are the moved dates, not the real ones."
CHANGED_AFTER = (
    "Parts of it were made by the anonymizer, but it was changed afterwards (for example, "
    "data was added), so we can't confirm all its dates were moved."
)
