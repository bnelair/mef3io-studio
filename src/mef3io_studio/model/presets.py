"""Saved channel choices ("presets") for making new recordings (#16).

A preset is a name plus a list of channel names that are KEPT, or (the other
way round) LEFT OUT. Applying one to a set of recordings is by channel name
(exact, or ignoring case) and works through :class:`~.channels.ChannelSelection`
(the picker's own rules), so a preset is only another way to set the same
choice. Per recording, :func:`match_recording` says what it keeps, what it
leaves out and which of the preset's channels it does not have (shown, never
an error), and whether the recording would be left with no channel at all
(blocked).

Channel names can identify (a montage is a fingerprint), so presets are
stored like the app's other private state: ONE sealed file in the per-user
app-data folder (``model.journal.seal``: encrypted and authenticated, 0600,
never beside a recording), written under a temporary name and renamed. Nothing
here ever logs a name. Export writes a plain file the user chooses (to share
with colleagues); the dialog says that it holds channel names.

Plain Python, no Qt.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .channels import ChannelSelection
from .journal import data_dir, seal, unseal

#: Version of the exported file and of the sealed store.
FORMAT = 1
FILE_KIND = "mef3io-studio channel presets"
STORE_FILE = "presets.json"

MAX_NAME_CHARS = 80
MAX_CHANNEL_CHARS = 256
MAX_CHANNELS = 5000
MAX_PRESETS = 500
#: A shared file is a few KB; refuse anything near this before reading it.
MAX_FILE_BYTES = 1024 * 1024


class PresetError(ValueError):
    """A preset or a preset file that cannot be used, in plain words."""


@dataclass(frozen=True)
class ChannelPreset:
    """A named channel choice. ``exclude``: the names are the channels to
    LEAVE OUT (everything else is kept); otherwise they are the ones to KEEP."""

    name: str
    channels: tuple[str, ...] = field(repr=False)
    exclude: bool = False


def name_problem(name: str, existing: Iterable[str] = ()) -> str | None:
    """Why ``name`` cannot name a preset (plain words), or None."""
    text = name.strip()
    if not text:
        return "Give the saved choice a name."
    if not text.isprintable():
        return "Use only letters, digits, spaces and punctuation in the name."
    if len(text) > MAX_NAME_CHARS:
        return f"Use at most {MAX_NAME_CHARS} characters for the name."
    if text.casefold() in {e.casefold() for e in existing}:
        return "A saved choice with that name already exists."
    return None


def _clean(channels: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for channel in channels:
        if not isinstance(channel, str):
            raise PresetError("A channel name is not text.")
        text = channel.strip()
        if text and text not in seen:
            seen[text] = None
    out = tuple(seen)
    if not out:
        raise PresetError("Choose at least one channel to save.")
    if len(out) > MAX_CHANNELS or any(len(c) > MAX_CHANNEL_CHARS for c in out):
        raise PresetError("That list of channels is too long to save.")
    return out


def make_preset(name: str, channels: Iterable[str], exclude: bool = False) -> ChannelPreset:
    """A checked preset (raises :class:`PresetError`)."""
    problem = name_problem(name)
    if problem:
        raise PresetError(problem)
    return ChannelPreset(name.strip(), _clean(channels), bool(exclude))


def preset_from_selection(
    name: str, selection: ChannelSelection, exclude: bool = False
) -> ChannelPreset:
    """The picker's current choice as a preset: the kept channels, or (with
    ``exclude``) the channels left out."""
    names = (
        selection.excluded if exclude else tuple(n for n in selection.order if n in selection.kept)
    )
    return make_preset(name, names, exclude)


# --------------------------------------------------------------------------
# Applying


def _fold(name: str, ignore_case: bool) -> str:
    return name.casefold() if ignore_case else name


@dataclass(frozen=True)
class PresetMatch:
    """What a preset does to ONE recording."""

    #: The recording's channels the preset keeps / leaves out.
    kept: tuple[str, ...] = field(repr=False)
    left_out: tuple[str, ...] = field(repr=False)
    #: The preset's channels this recording does not have.
    missing: tuple[str, ...] = field(repr=False)

    @property
    def empty(self) -> bool:
        """The recording would keep no channel at all: blocked."""
        return not self.kept

    @property
    def channels(self) -> tuple[str, ...] | None:
        """mef3io's ``channels=`` (keep only these) or None."""
        return None if not self.left_out else self.kept

    @property
    def exclude_channels(self) -> tuple[str, ...]:
        """mef3io's ``exclude_channels=``: the same choice the other way."""
        return self.left_out


def match_recording(
    preset: ChannelPreset, available: Sequence[str], ignore_case: bool = False
) -> PresetMatch:
    """Apply ``preset`` to a recording that has the channels ``available``
    (matching by name, exactly or ignoring case). The recording's own
    spelling is what is returned."""
    wanted = {_fold(c, ignore_case) for c in preset.channels}
    have = {_fold(c, ignore_case) for c in available}
    hit = tuple(c for c in available if _fold(c, ignore_case) in wanted)
    if preset.exclude:
        kept = tuple(c for c in available if c not in hit)
        left_out = hit
    else:
        kept = hit
        left_out = tuple(c for c in available if c not in hit)
    missing = tuple(c for c in preset.channels if _fold(c, ignore_case) not in have)
    return PresetMatch(kept, left_out, missing)


def apply_to_selection(
    preset: ChannelPreset, selection: ChannelSelection, ignore_case: bool = False
) -> tuple[str, ...]:
    """Set the picker's choice to the preset (one undo step). Returns the
    preset's channels that NO chosen recording has (shown, never an error)."""
    names = selection.order
    wanted = {_fold(c, ignore_case) for c in preset.channels}
    hit = [n for n in names if _fold(n, ignore_case) in wanted]
    kept = [n for n in names if n not in hit] if preset.exclude else hit
    selection.replace_kept(kept)
    present = {_fold(n, ignore_case) for n in names}
    return tuple(c for c in preset.channels if _fold(c, ignore_case) not in present)


# --------------------------------------------------------------------------
# The private store


def _to_json(presets: Sequence[ChannelPreset]) -> dict:
    return {
        "format": FORMAT,
        "presets": [
            {"name": p.name, "channels": list(p.channels), "exclude": p.exclude} for p in presets
        ],
    }


def _from_json(value: object) -> list[ChannelPreset]:
    if not isinstance(value, dict) or not isinstance(value.get("presets"), list):
        raise PresetError("This file does not hold saved channel choices.")
    if value.get("format", FORMAT) != FORMAT:
        raise PresetError("This file was made by a newer version of the app.")
    raw = value["presets"]
    if len(raw) > MAX_PRESETS:
        raise PresetError("This file holds too many saved choices.")
    out: list[ChannelPreset] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise PresetError("A saved choice in this file is damaged.")
        channels = item.get("channels")
        if not isinstance(channels, list):
            raise PresetError("A saved choice in this file is damaged.")
        exclude = item.get("exclude", False)
        if not isinstance(exclude, bool):
            raise PresetError("A saved choice in this file is damaged.")
        out.append(make_preset(item["name"], channels, exclude))
    return out


def _unique(name: str, taken: Iterable[str]) -> str:
    used = {t.casefold() for t in taken}
    if name.casefold() not in used:
        return name
    n = 2
    while f"{name} ({n})".casefold() in used:
        n += 1
    return f"{name} ({n})"


class PresetStore:
    """The presets, in one sealed file in the app's data folder. ``folder``
    is only for tests; the app uses the per-user data folder. A file that
    cannot be read back (another key, changed) reads as empty, but is NEVER
    overwritten: the next save moves it aside (``presets.json.unreadable-N``,
    never deleted) and :attr:`moved_aside` says so. ``model.journal.
    KeyUnavailable`` propagates (nothing is replaced without the key).

    Two app instances saving at the same moment can lose one of the changes
    (last writer wins, each write is atomic); nothing else is ever lost."""

    def __init__(self, folder: str | None = None):
        self._folder = folder
        #: Set by a save that had to move an unreadable store aside: the new
        #: name of that file (for the notice), else "".
        self.moved_aside = ""

    @property
    def path(self) -> str:
        return os.path.join(self._folder or data_dir(), STORE_FILE)

    def _read(self) -> tuple[bool, list[ChannelPreset]]:
        """(readable, presets). A missing file is readable and empty."""
        try:
            with open(self.path, encoding="utf-8") as handle:
                sealed = json.load(handle)["sealed"]
        except FileNotFoundError:
            return True, []
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            return False, []
        if not isinstance(sealed, str):
            return False, []
        value = unseal(sealed)
        try:
            return True, _from_json(value)
        except (PresetError, RecursionError):
            return False, []

    def load(self) -> list[ChannelPreset]:
        return self._read()[1]

    @property
    def unreadable(self) -> bool:
        """The file is there but cannot be read back."""
        return not self._read()[0]

    def _move_aside(self) -> None:
        n = 1
        while os.path.lexists(f"{self.path}.unreadable-{n}"):
            n += 1
        aside = f"{self.path}.unreadable-{n}"
        os.replace(self.path, aside)
        self.moved_aside = os.path.basename(aside)

    def save(self, presets: Sequence[ChannelPreset]) -> None:
        folder = os.path.dirname(self.path)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        self.moved_aside = ""
        sealed = seal(_to_json(presets))  # (KeyUnavailable before anything moves)
        if os.path.lexists(self.path) and self.unreadable:
            self._move_aside()
        temporary = f"{self.path}.{secrets.token_hex(4)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with open(fd, "w", encoding="utf-8") as handle:
                json.dump({"sealed": sealed}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def names(self) -> list[str]:
        return [p.name for p in self.load()]

    def get(self, name: str) -> ChannelPreset | None:
        return next((p for p in self.load() if p.name == name), None)

    def add(self, preset: ChannelPreset, replace: bool = False) -> None:
        """Save ``preset``; an existing name is refused unless ``replace``."""
        presets = self.load()
        same = [p for p in presets if p.name.casefold() == preset.name.casefold()]
        if same and not replace:
            raise PresetError("A saved choice with that name already exists.")
        if len(presets) - len(same) >= MAX_PRESETS:
            raise PresetError("There are too many saved choices. Delete some first.")
        self.save([p for p in presets if p not in same] + [preset])

    def remove(self, name: str) -> bool:
        presets = self.load()
        kept = [p for p in presets if p.name != name]
        if len(kept) == len(presets):
            return False
        self.save(kept)
        return True

    def rename(self, old: str, new: str) -> None:
        presets = self.load()
        others = [p.name for p in presets if p.name != old]
        problem = name_problem(new, others)
        if problem:
            raise PresetError(problem)
        self.save(
            [
                ChannelPreset(new.strip(), p.channels, p.exclude) if p.name == old else p
                for p in presets
            ]
        )

    def import_text(self, text: str) -> list[str]:
        """Add the presets of an exported file; a name already used gets
        "(2)" etc. Returns the names added. Raises :class:`PresetError`."""
        incoming = parse_export(text)
        presets = self.load()
        if len(presets) + len(incoming) > MAX_PRESETS:
            raise PresetError("There would be too many saved choices. Delete some first.")
        added = []
        for p in incoming:
            name = _unique(p.name, [q.name for q in presets])
            problem = name_problem(name)
            if problem:
                raise PresetError(problem)
            presets.append(ChannelPreset(name, p.channels, p.exclude))
            added.append(name)
        self.save(presets)
        return added


# --------------------------------------------------------------------------
# Sharing as a small file


def export_text(presets: Sequence[ChannelPreset]) -> str:
    """The presets as a small JSON file's text (channel names only)."""
    value = {"kind": FILE_KIND, **_to_json(presets)}
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def parse_export(text: str) -> list[ChannelPreset]:
    """The presets of an exported file (raises :class:`PresetError`)."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        raise PresetError("This file does not hold saved channel choices.") from None
    if isinstance(value, dict) and value.get("kind", FILE_KIND) != FILE_KIND:
        raise PresetError("This file does not hold saved channel choices.")
    return _from_json(value)
