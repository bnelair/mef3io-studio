"""Rename recordings: the checks that need no files, in plain words.

A recording's name lives in several places (its folder, the notes file that
belongs to the whole recording, a name stored in every file, a matching
description). Changing all of them together is mef3io's ``rename_session``
(a journaled operation, see :mod:`.rename_ops`); this module only decides
whether a NEW name is acceptable, before anything is touched:

* the rules mef3io enforces, so the preview already says no: at most 250
  bytes of UTF-8 for a folder recording (246 for a single-file one), a
  portable file name (no ``/ \\ : * ? " < > |``, no control character, no
  Windows device name such as CON, no trailing dot or space), never cut
  short;
* collisions: with something already in the folder, or between two names in
  the same batch, compared without regard to capital letters and after
  Unicode normalisation (so "é" typed two ways is the same name);
* a path that would be too long for Windows at the deepest file;
* swaps and cycles (A to B while B to A): refused with an explanation, a
  chain (A to B while B is renamed away first) is ordered correctly;
* names built from a template: ``{start:%Y-%m-%d_%H%M%S}``, ``{index:03d}``,
  ``{name}``.

Plain Python, no Qt, no mef3io. Every message is everyday words
(docs/ui-language.md).
"""

from __future__ import annotations

import os
import re
import string
import sys
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import tzinfo
from enum import StrEnum

from .recording import SUFFIXES
from .timefmt import UTC, to_datetime

#: At most this many bytes of UTF-8 in a new name (mef3io's limit): so that
#: ``<name>.mefd`` and ``<name>.ridx`` fit a 255-byte file name and the name
#: fits the stored-name field. A single-file recording: ``<name>.mefd.tar``.
SESSION_NAME_LIMIT = 250
ARCHIVE_NAME_LIMIT = 246
#: The longest path Windows accepts without special handling.
WINDOWS_PATH_LIMIT = 260

_ILLEGAL = set('/\\:*?"<>|')
_DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)

TEMPLATE_FIELDS = ("start", "index", "name")
TEMPLATE_HELP = (
    "Use {start:%Y-%m-%d_%H%M%S} for the start time, {index:03d} for the "
    "number in time order and {name} for the current name."
)


class NameProblem(StrEnum):
    EMPTY = "empty"
    TOO_LONG = "too_long"
    ILLEGAL = "illegal"
    CONTROL = "control"
    RESERVED = "reserved"
    ENDS_WITH = "ends_with"
    SAME = "same"
    TAKEN = "taken"
    COLLIDES = "collides"
    CYCLE = "cycle"
    PATH_TOO_LONG = "path_too_long"
    NO_START = "no_start"
    BAD_TEMPLATE = "bad_template"


def name_limit(packed: bool) -> int:
    return ARCHIVE_NAME_LIMIT if packed else SESSION_NAME_LIMIT


def name_key(name: str) -> str:
    """Two names that are the same to a file system compare equal: same
    letters after Unicode normalisation, capitals ignored."""
    return unicodedata.normalize("NFC", name).casefold()


def clean_name(typed: str, packed: bool = False) -> str:
    """What was typed, without surrounding blanks and without the ending
    ``.mefd`` / ``.mefd.tar`` (the ending is always added by the app)."""
    name = typed.strip()
    lowered = name.lower()
    for suffix in SUFFIXES if packed else (".mefd",):
        if lowered.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)].rstrip()
    return name


def _bytes(name: str, decomposed: bool) -> int:
    form = "NFD" if decomposed else "NFC"
    return len(unicodedata.normalize(form, name).encode("utf-8"))


def check_name(
    name: str,
    packed: bool = False,
    decomposes: bool | None = None,
) -> list[NameProblem]:
    """What is wrong with a new name by itself (empty list = fine). Length
    is measured as the file system will store it: macOS volumes store
    accented letters decomposed, which can make a name longer
    (``decomposes``, by default True on macOS)."""
    problems: list[NameProblem] = []
    if not name:
        return [NameProblem.EMPTY]
    if decomposes is None:
        decomposes = sys.platform == "darwin"
    longest = max(_bytes(name, False), _bytes(name, True) if decomposes else 0)
    if longest > name_limit(packed):
        problems.append(NameProblem.TOO_LONG)
    if any(c in _ILLEGAL for c in name):
        problems.append(NameProblem.ILLEGAL)
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        problems.append(NameProblem.CONTROL)
    if _DEVICE.match(name.split(".")[0].rstrip()):
        problems.append(NameProblem.RESERVED)
    if name.endswith((".", " ")) or name.startswith(" "):
        problems.append(NameProblem.ENDS_WITH)
    return problems


def problem_text(problem: NameProblem, packed: bool = False) -> str:
    """The problem in everyday words, saying what to do (nothing is
    renamed while one is left)."""
    limit = name_limit(packed)
    words = {
        NameProblem.EMPTY: "The new name is empty. Type a name.",
        NameProblem.TOO_LONG: (
            f"The new name is too long. It can be at most {limit} letters and signs "
            "(an accented or non-Latin letter can count as 2 to 4). Shorten it; "
            "it is never cut short for you."
        ),
        NameProblem.ILLEGAL: (
            'The new name has a sign that Windows does not allow in a name: / \\ : * ? " < > |. '
            "Remove it."
        ),
        NameProblem.CONTROL: "The new name has a hidden control character. Retype the name.",
        NameProblem.RESERVED: (
            "Windows keeps this name for itself (such as CON, NUL or COM1). Choose another."
        ),
        NameProblem.ENDS_WITH: (
            "A name cannot start with a blank or end with a blank or a dot. Remove it."
        ),
        NameProblem.SAME: "The new name is the same as the current one. Type a different name.",
        NameProblem.TAKEN: (
            "Something with this name is already in the folder. Nothing is replaced; "
            "choose another name."
        ),
        NameProblem.COLLIDES: (
            "Two recordings would get this same name (capital letters do not make "
            "names different). Give each its own name."
        ),
        NameProblem.CYCLE: (
            "These recordings would swap names with each other. Give one of them a "
            "different name first, then swap in a second round."
        ),
        NameProblem.PATH_TOO_LONG: (
            "With this name, a file inside the recording would have a path longer than "
            "Windows allows. Use a shorter name, or move the recording higher up."
        ),
        NameProblem.NO_START: (
            "This name uses the start time, but the start time of this recording is not "
            "confirmed. Type its name by hand."
        ),
        NameProblem.BAD_TEMPLATE: (
            "This pattern cannot be used. " + TEMPLATE_HELP + " Nothing was renamed."
        ),
    }
    return words[problem]


# -- names from a pattern ---------------------------------------------------------


class BadTemplate(ValueError):
    """The pattern is not one of the allowed forms."""


def check_template(template: str) -> None:
    """Raises :class:`BadTemplate` unless every ``{...}`` in the pattern is
    ``start``, ``index`` or ``name``, with a plain format after a colon."""
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise BadTemplate(str(exc)) from None
    if not template.strip():
        raise BadTemplate("empty")
    for _literal, field_name, spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in TEMPLATE_FIELDS or conversion or "{" in (spec or ""):
            raise BadTemplate(field_name)


def name_from_template(
    template: str,
    *,
    index: int | None,
    old_name: str,
    start_us: int | None,
    zone: tzinfo = UTC,
) -> str | NameProblem:
    """The name this pattern gives a recording, or the problem
    (:attr:`NameProblem.NO_START` when it needs a start, or a place in the
    time order, that is not confirmed; :attr:`NameProblem.BAD_TEMPLATE`). ``{start}`` is shown in
    ``zone`` (UTC until a site time zone is chosen), never in the file's own
    offset."""
    try:
        check_template(template)
    except BadTemplate:
        return NameProblem.BAD_TEMPLATE
    used = {f for _l, f, _s, _c in string.Formatter().parse(template)}
    if ("start" in used and start_us is None) or ("index" in used and index is None):
        return NameProblem.NO_START
    start = to_datetime(start_us, zone) if start_us is not None else None
    try:
        return template.format(start=start, index=index, name=old_name).strip()
    except (ValueError, KeyError, IndexError, TypeError, OverflowError):
        return NameProblem.BAD_TEMPLATE


# -- the batch ---------------------------------------------------------------------


@dataclass(frozen=True)
class RenameItem:
    """One recording to rename. ``key`` is the explorer's key; ``location`` is
    where it is now; ``new_name`` has no ``.mefd`` ending."""

    key: str = field(repr=False)
    location: str = field(repr=False)
    old_name: str = field(repr=False)
    new_name: str = field(repr=False)
    packed: bool = False
    #: Characters in the longest path from the recording's folder down to a
    #: file inside it (for the Windows path check); 0 = not known.
    deepest_inside: int = 0


@dataclass(frozen=True)
class RowCheck:
    """The checks on one row: empty = it can be renamed."""

    problems: tuple[NameProblem, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class RenamePlan:
    #: One per item, in the order given.
    rows: tuple[RowCheck, ...]
    #: Indexes of the items in the order they must be renamed (a chain is
    #: ordered; blocked rows are left out).
    order: tuple[int, ...]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.rows) and bool(self.rows)


def _suffix(packed: bool) -> str:
    return ".mefd.tar" if packed else ".mefd"


def list_folder_keys(folder: str) -> set[str]:
    """The name keys of everything in ``folder`` (empty if unreadable)."""
    try:
        return {name_key(n) for n in os.listdir(folder)}
    except OSError:
        return set()


def plan_renames(
    items: Iterable[RenameItem],
    *,
    lister: Callable[[str], set[str]] = list_folder_keys,
    path_limit: int | None = WINDOWS_PATH_LIMIT if sys.platform == "win32" else None,
    decomposes: bool | None = None,
) -> RenamePlan:
    """Check a batch of renames against each other and against the folder.

    Collisions and the "already in the folder" test use :func:`name_key`.
    A name that is another batch member's CURRENT name is fine when that one
    is renamed away (a chain, ordered); a cycle is refused."""
    items = list(items)
    problems: list[list[NameProblem]] = []
    for item in items:
        found = check_name(item.new_name, item.packed, decomposes)
        if not found and item.new_name == item.old_name:
            found.append(NameProblem.SAME)
        if not found and path_limit is not None and item.deepest_inside:
            folder = os.path.dirname(os.path.abspath(item.location))
            total = len(os.path.join(folder, item.new_name + _suffix(item.packed))) + (
                1 + item.deepest_inside
            )
            if total >= path_limit:
                found.append(NameProblem.PATH_TOO_LONG)
        problems.append(found)

    def full(item: RenameItem, name: str) -> str:
        folder = os.path.normcase(os.path.dirname(os.path.abspath(item.location)))
        return folder + "\0" + name_key(name + _suffix(item.packed))

    now = {full(i, i.old_name): n for n, i in enumerate(items)}
    targets: dict[str, list[int]] = {}
    for n, item in enumerate(items):
        if not problems[n]:
            targets.setdefault(full(item, item.new_name), []).append(n)
    folder_names: dict[str, set[str]] = {}
    depends: dict[int, int] = {}
    for target, rows in targets.items():
        if len(rows) > 1:
            for n in rows:
                problems[n].append(NameProblem.COLLIDES)
            continue
        n = rows[0]
        folder = os.path.dirname(os.path.abspath(items[n].location))
        if folder not in folder_names:
            folder_names[folder] = lister(folder)
        owner = now.get(target)
        if owner == n:
            continue  # only capital letters or accents-as-typed differ: the same entry
        if owner is not None:
            depends[n] = owner  # renamed away first, if it is itself renamed
        elif target.split("\0", 1)[1] in folder_names[folder]:
            problems[n].append(NameProblem.TAKEN)

    # Order: whoever blocks a name must be renamed first; a cycle is refused.
    state: dict[int, int] = {}  # 1 = visiting, 2 = done
    order: list[int] = []

    def visit(n: int, trail: list[int]) -> None:
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            ring = trail[trail.index(n) :]
            for m in ring:
                if NameProblem.CYCLE not in problems[m]:
                    problems[m].append(NameProblem.CYCLE)
            return
        state[n] = 1
        blocker = depends.get(n)
        if blocker is not None:
            visit(blocker, [*trail, n])
        state[n] = 2
        order.append(n)

    for n in range(len(items)):
        if not problems[n]:
            visit(n, [])
    order = [n for n in order if not problems[n]]
    # A row that depends on a blocked one cannot go either.
    changed = True
    while changed:
        changed = False
        for n in list(order):
            blocker = depends.get(n)
            if blocker is not None and blocker not in order:
                problems[n].append(NameProblem.TAKEN)
                order.remove(n)
                changed = True
    return RenamePlan(tuple(RowCheck(tuple(p)) for p in problems), tuple(order))


# -- what the recording itself needs --------------------------------------------------


@dataclass(frozen=True)
class RowState:
    """What was found about one recording before renaming it (see
    :func:`.rename_ops.read_state`): plain flags."""

    #: The names stored inside differ from the folder's name (renamed by
    #: another program): fix this first.
    drift: bool = False
    #: A rename of it was interrupted earlier; the next rename puts it back
    #: as it was first.
    unfinished: bool = False
    #: Something wrote to it in the last few minutes, or a copy is being
    #: made beside it.
    recently_written: bool = False
    #: Password-protected and no working password is known here.
    needs_password: bool = False
    #: Open in a signal viewer.
    in_use: bool = False
    #: The name of the first problem reading it, in words (or "").
    unreadable: str = ""
    #: Characters in the longest path below the recording's folder (the
    #: Windows path-length check); 0 = not known.
    deepest_inside: int = 0


RECENT_SECONDS = 300


def blocked_reason(state: RowState, packed: bool = False) -> str:
    """Why this recording cannot be renamed right now, or "" if it can
    (a name that is out of step does not block: it is fixed first)."""
    if state.in_use:
        return "It is open in a signal viewer. Close that window first."
    if state.unreadable:
        return state.unreadable
    if state.needs_password:
        return (
            "It is password-protected, and the rename needs its password (the basic one is "
            "enough). Unlock it in Explore first."
        )
    if state.recently_written and not state.unfinished:  # its own interrupted rename is recent
        return (
            "It was written to in the last few minutes, or a copy is being made next to "
            "it. Wait until the writing is finished. A recorder that is idle but still "
            "holding the recording open cannot be detected: make sure nothing uses it."
        )
    return ""


ARCHIVE_NOTE = (
    "This renames the file only. Inside the file the recording still carries its old name. "
    "To change that too, unpack it, rename the folder, and pack it again."
)


DRIFT_NOTE = (
    "Another program renamed this recording's folder, so the name stored inside it no "
    "longer matches. It is put in step first, then renamed."
)
UNFINISHED_NOTE = (
    "A rename of this recording was interrupted earlier. Nothing is lost: renaming it "
    "again first puts it back exactly as it was. To only put it back, leave its name as it is."
)
