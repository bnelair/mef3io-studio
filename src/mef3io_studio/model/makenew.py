"""Make new recordings: the plain logic behind the batch window (#16).

One window makes the same changes in many recordings at once: which
channels to keep (a saved choice or the picker), other details, new
passwords, new names. Each recording becomes one
:class:`~.changes.ChangeRequest`, run by the ONE batch runner
(``model.pipeline`` + ``workers.tasks.run_batch``: journal, resume after a
crash, Stop, progress, space checks). Nothing here touches a recording or
the screen; originals are only ever read.

Plain Python, no Qt. Never logs anything (names and channels identify).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .anonymize import destination_for, estimate_bytes, inside_any_recording, name_problem
from .changes import ChangeRequest, changed_details, too_long
from .channels import ChannelSelection
from .presets import ChannelPreset, match_recording
from .refusals import DETAIL_LABELS

#: How the new names are made.
NAME_ADDED = "added"  # the original's name, then a text
NAME_NUMBERED = "numbered"  # a text, then 001, 002, ...
NAMINGS = (NAME_ADDED, NAME_NUMBERED)


@dataclass(frozen=True)
class Source:
    """One recording of the batch, as plain data."""

    key: str
    path: str = field(repr=False)
    name: str = field(repr=False)
    channels: tuple[str, ...] = field(repr=False)
    #: Bytes of each channel's signal, for the space estimate.
    channel_bytes: Mapping[str, int] = field(default_factory=dict, repr=False)
    #: The full-access password that opened it (memory only).
    password: str = field(default="", repr=False)


def new_names(sources: Sequence[Source], naming: str, text: str) -> list[str]:
    """The new recordings' names, in the order of ``sources``."""
    text = text.strip() if naming == NAME_NUMBERED else text
    if naming == NAME_NUMBERED:
        width = max(3, len(str(len(sources))))
        return [f"{text}{n:0{width}d}" for n in range(1, len(sources) + 1)]
    return [f"{s.name}{text}" for s in sources]


def name_problems(sources: Sequence[Source], names: Sequence[str], folder: str) -> list[str]:
    """Why these names and this folder cannot be used (plain words; the
    first problem of each recording, then the shared ones). Checks names,
    duplicates, the folder and names already taken; free space is checked
    by the batch's own check before it starts."""
    total = len(sources)
    out: list[str] = []
    seen: dict[str, int] = {}
    for n, name in enumerate(names):
        found = name_problem(name)
        if found:
            out.append(f"Recording {n + 1} of {total}: {found}")
            continue
        folded = name.casefold()
        if folded in seen:
            out.append(
                f"Recordings {seen[folded] + 1} and {n + 1} of {total} would get the same name."
            )
        seen.setdefault(folded, n)
    if not folder or not os.path.isdir(folder):
        out.append("Choose a folder that exists.")
        return out
    paths = [s.path for s in sources]
    if inside_any_recording(os.path.join(folder, "new.mefd"), paths):
        out.append("Choose a folder that is not inside a recording.")
        return out
    for n, name in enumerate(names):
        if name_problem(name):
            continue
        destination = destination_for(folder, name)
        if os.path.lexists(destination):
            out.append(
                f"Recording {n + 1} of {total}: a file or folder with the new name is already "
                "in the chosen folder."
            )
        elif os.path.lexists(destination + ".partial"):
            out.append(
                f"Recording {n + 1} of {total}: an unfinished copy left by an earlier run is in "
                "the way. If nothing else is running, delete it, or choose another name."
            )
    return out


@dataclass(frozen=True)
class Row:
    """What the channel choice does to one recording."""

    key: str
    kept: tuple[str, ...] = field(repr=False)
    left_out: tuple[str, ...] = field(repr=False)
    #: Channels of the applied saved choice this recording does not have.
    missing: tuple[str, ...] = field(default=(), repr=False)

    @property
    def empty(self) -> bool:
        """Nothing would be kept: this recording blocks the batch."""
        return not self.kept


def rows(
    sources: Sequence[Source],
    selection: ChannelSelection,
    preset: ChannelPreset | None = None,
    ignore_case: bool = False,
) -> list[Row]:
    """The picker's choice, recording by recording. ``preset``: the saved
    choice that was applied, for the channels each recording lacks (shown,
    not an error)."""
    out = []
    for s in sources:
        missing: tuple[str, ...] = ()
        if preset is not None:
            missing = match_recording(preset, s.channels, ignore_case).missing
        out.append(Row(s.key, selection.kept_in(s.key), selection.excluded_in(s.key), missing))
    return out


def details_from(typed: Mapping[str, str]) -> dict:
    """mef3io's ``metadata=`` for the details typed once for the whole
    batch: an empty box keeps what each recording has (nothing is read from
    the recordings here), any text replaces it."""
    return changed_details({}, {k: v for k, v in typed.items() if v != ""})


def details_problem(metadata: Mapping | None) -> str:
    """ "" or why the details cannot be used (a text that is too long is
    refused, never cut)."""
    long = too_long(metadata)
    if not long:
        return ""
    labels = "; ".join(DETAIL_LABELS[n] for n in long)
    return f"Too long, and the app never cuts a text short: {labels}. Make it shorter."


def requests_for(
    sources: Sequence[Source],
    selection: ChannelSelection,
    names: Sequence[str],
    folder: str,
    metadata: Mapping | None = None,
    output_password: tuple[str, str] | None = None,
) -> list[ChangeRequest]:
    """One request per recording. Channels are sent as the ones LEFT OUT of
    that recording (what the app does everywhere), so a channel another
    recording has is never named to this one."""
    return [
        ChangeRequest(
            s.path,
            destination_for(folder, name),
            password=s.password,
            exclude_channels=selection.excluded_in(s.key),
            output_password=output_password,
            metadata=dict(metadata) if metadata else None,
            key=s.key,
        )
        for s, name in zip(sources, names, strict=True)
    ]


def estimates_for(sources: Sequence[Source], selection: ChannelSelection) -> dict[str, int]:
    """Bytes each new recording needs, by key."""
    return {s.key: estimate_bytes(dict(s.channel_bytes), selection.kept_in(s.key)) for s in sources}


def nothing_to_do(selection: ChannelSelection, metadata: Mapping | None, output_password) -> bool:
    """No channel left out, no details and no password: the new recordings
    would be plain copies."""
    return not selection.excluded and not metadata and output_password is None
