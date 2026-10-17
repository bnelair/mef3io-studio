"""The unattended batch: anonymize, archive, check, tidy up, one recording
after another (mef3io-studio#5, "one unattended pipeline").

Three ways to deliver the copies:

* ``folders``: one anonymized ``<name>.mefd`` folder per recording, written
  by mef3io straight into the chosen folder (as before);
* ``archives``: one ``<name>.mefd.tar`` per recording. mef3io anonymizes into
  the WORK FOLDER, ``mef3io.archive_session`` packs that copy under a
  temporary name next to its final one (``model.archive``), the archive is
  opened and compared with the checked work copy (channels, parts, start and
  end, sample counts), then every member is re-read and its SHA-256 compared
  with the work copy's file; only then is it renamed and the work copy
  deleted (a mismatch keeps the work copy and fails the recording);
* ``bundle``: one ``.tar`` holding every anonymized recording as its own
  folder (``model.bundle``): each checked work copy is added, the added
  members are read back and their names, sizes and SHA-256 compared with the
  work copy's files, then the work copy is deleted; the
  bundle is renamed into place when every recording is in it.

The work folder only ever holds the recording being worked on, in a folder
with an opaque name (``work-<batch>-<item>``). This module holds the plain
data of a batch and the checks made BEFORE it starts, so nothing fails after
hours: names (mef3io's own refusal), a writable work folder outside any
recording, free space at the work folder (the largest copy) and at the
destination (all of them). The task that runs it is
``workers.tasks.run_batch``.

Plain Python, no Qt. Never logs anything.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field, replace

from .anonymize import (
    CLEAR_ALL,
    CLEAR_DESCRIPTIONS,
    CLEAR_LOCATION,
    CLEAR_PATIENT,
    TEXT_DROP,
    CopyRequest,
    inside_any_recording,
)

FOLDERS = "folders"
ARCHIVES = "archives"
BUNDLE = "bundle"
OUTPUTS = (FOLDERS, ARCHIVES, BUNDLE)

#: Work copies live in folders named like this (opaque ids only).
JOB_PREFIX = "work-"
#: Margin on every space estimate: the notes files and the file system.
MARGIN = 1.05
EXTRA_BYTES = 16 * 1024 * 1024


def default_work_folder() -> str:
    from .journal import data_dir

    return os.path.join(data_dir(), "work")


def work_folder_problem(folder: str, recordings: list[str] | tuple[str, ...] = ()) -> str | None:
    """Why ``folder`` cannot be the work folder (plain words), or None. It
    must be a folder the app can write to, and not inside a recording."""
    if not folder:
        return "Choose a work folder."
    if inside_any_recording(folder, list(recordings)):
        return "The work folder is inside a recording. Choose a folder outside any recording."
    try:
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, f".mef3io-studio-probe-{secrets.token_hex(6)}")
        with open(probe, "wb") as handle:
            handle.write(b"x")
        os.unlink(probe)
    except OSError:
        return "The app cannot write to the work folder. Choose another folder."
    return None


def leftover_work_copies(folder: str) -> list[str]:
    """Work-copy folders an interrupted run left behind (de-identified but
    never checked: they must not be kept silently)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return sorted(
        os.path.join(folder, n)
        for n in names
        if n.startswith(JOB_PREFIX) and os.path.isdir(os.path.join(folder, n))
    )


def remove_work_copy(path: str) -> bool:
    """Delete one work-copy folder. True when nothing is left."""
    if os.path.basename(path).startswith(JOB_PREFIX):
        shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


def remove_temporary_file(path: str) -> None:
    """Remove a temporary file of the app's own (opaque ``.mef3io-studio-``
    name) and anything next to it starting with that name."""
    base = os.path.basename(path)
    if not base.startswith(".mef3io-studio-"):
        return
    folder = os.path.dirname(path) or "."
    try:
        names = os.listdir(folder)
    except OSError:
        return
    stem = base.split(".mefd")[0]
    for name in names:
        if name.startswith(stem):
            target = os.path.join(folder, name)
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    os.unlink(target)
                except OSError:
                    pass


def remove_own_result(path: str) -> None:
    """Remove a result this batch was making, to make it again after an
    interruption: a ``.mefd`` folder or a ``.mefd.tar`` file. Only ever
    called for a result :func:`made_by_this_batch` confirmed (a name alone
    proves nothing)."""
    lowered = path.lower()
    if lowered.endswith(".mefd") and os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif lowered.endswith(".mefd.tar") and os.path.isfile(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def same_volume(a: str, b: str) -> bool:
    """Both folders are on the same drive (the work copy and the result
    then share its free space)."""
    try:
        return bool(a and b) and os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def probe(sources: list[tuple[str, str]], outputs: list[tuple[str, str]], cancel=None) -> dict:
    """In a helper process: open every original with its own password (it
    must give full access) and read its start and end; open each finished
    copy with the copies' full-access password. Details only, no signal."""
    from .inspect import inspect_recording
    from .recording import ACCESS_BASIC, recording_name

    found = []
    for source, password in sources:
        if cancel is not None:
            cancel.raise_if_cancelled()
        outcome = inspect_recording(source, password, cancel=cancel, detect_legacy=False)
        details = outcome.details
        if outcome.needs_password:
            problem = (
                "its password did not open it. Type its full-access password again."
                if password
                else "it needs its full-access password."
            )
            found.append({"problem": problem})
        elif details is None:
            found.append({"problem": "it could not be opened. Check that the drive is connected."})
        elif details.access == ACCESS_BASIC:
            found.append(
                {
                    "problem": "the password only gives basic access; anonymizing needs the "
                    "full-access password."
                }
            )
        else:
            found.append(
                {"name": recording_name(source), "start": details.start, "end": details.end}
            )
    opened = []
    for path, password in outputs:
        outcome = inspect_recording(path, password, detect_legacy=False)
        opened.append(
            outcome.details is not None
            and not outcome.needs_password
            and outcome.details.access != ACCESS_BASIC
        )
    return {"sources": found, "outputs": opened}


def result_check(item: BatchItem, path: str | None = None) -> dict:
    """What :func:`made_by_this_batch` needs to recognise this item's result
    (passwords included: in memory and in the helper only)."""
    r = item.request
    return {
        "path": path or item.final,
        "source": r.source,
        "source_password": r.password,
        "name": r.name,
        "shift": int(r.shift_us),
        "exclude": list(r.exclude_channels),
        "output_password": list(r.output_password) if r.output_password else None,
        "pseudonym": r.pseudonym,
        "clear": list(r.clear),
        "text": r.text,
    }


#: The identity facts (mef3io's inspect) each "clear" choice empties.
_CLEARED_FACTS = {
    CLEAR_PATIENT: ("subject_name_1", "subject_name_2", "subject_id"),
    CLEAR_LOCATION: ("recording_location",),
    CLEAR_DESCRIPTIONS: ("session_description", "channel_description", "reference_description"),
}


def _note_key(note, shift: int = 0) -> tuple:
    return (note.time + shift, note.kind, note.channel or "")


#: Identity values of the original whose text mef3io may take out of what
#: it keeps (see :func:`_has_content`).
_REMOVED_FACTS = (
    "subject_name_1",
    "subject_name_2",
    "subject_id",
    "recording_location",
    "session_name",
    "anonymized_name",
)


def _folded(text: str) -> str:
    """Case and accents folded: coarser than mef3io's own fold (ASCII and
    Latin letters, accents kept), so whatever matches there matches here."""
    # Canonical decomposition only: compatibility forms ("…" -> "...") would
    # split words mef3io keeps whole.
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _is_alnum(ch: str) -> bool:
    # mef3io's is_alnum: ASCII letters and digits, and every non-ASCII byte.
    return ch != "_" and (ch.isalnum() or ord(ch) >= 0x80)


def _tokens(text: str) -> list[str]:
    """Maximal runs of mef3io-alphanumeric characters ('_', '-' and the
    rest split)."""
    out, current = [], []
    for ch in text:
        if _is_alnum(ch):
            current.append(ch)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def _pieces(value: str) -> set[str]:
    """What mef3io may search for, for one value (folded), and a little
    more (``build_redactor``): the whole value (whitespace collapsed); its
    letter runs of 3+ characters (``name_words``; any non-ASCII character
    counts as a letter); its digit runs of 3+ (``id_digit_runs``, for every
    value, not only an ID); and the whole value's first and last
    alphanumeric run, whatever their length (see :func:`_has_content`)."""
    whole = " ".join(_folded(value).split())
    if not whole:
        return set()
    pieces = {whole}
    tokens = _tokens(whole)
    if tokens:
        pieces.update((tokens[0], tokens[-1]))
    letters = re.findall(r"(?:[^\W\d_]|[^\x00-\x7f])+", whole)
    pieces.update(run for run in letters if len(run) >= 3)
    pieces.update(run for run in re.findall(r"\d+", whole) if len(run) >= 3)
    return pieces


def _has_content(text: str, removed: set[str]) -> bool:
    """Whether some of ``text`` certainly survives mef3io's redaction when
    it keeps text, so that the copy must carry text there.

    mef3io has no public call for its redaction (1.4.0: anonymize.cpp,
    ``build_redactor``), so this mirrors its rules from the safe side: a
    word of the text (a maximal alphanumeric run, as mef3io splits) that
    contains none of the pieces of any value mef3io may remove
    (:func:`_pieces`) cannot be touched by any of its needles: a needle
    without separators lies inside one word, so the word would contain it;
    one reaching into the word from outside is a whole value, and the part
    inside is that value's first or last run, also a piece. (Not covered:
    a removal joining two words into a new one that a later, shorter needle
    matches; this takes a value that starts or ends with punctuation.) Such a word
    survives in this batch's own copy. Values used: every identity value
    of the original, cleared or not, and the names of the left-out
    channels; mef3io uses only some of them, and matches case and accents
    more narrowly. If mef3io ever removes more than this (a new rule), this
    batch would no longer recognise its own copy:
    ``test_own_copy_is_recognised_whatever_the_names_look_like`` in
    tests/test_batch_pipeline.py exercises the shapes that matter."""
    return any(not any(piece in word for piece in removed) for word in _tokens(_folded(text)))


def _removed_values(source, excluded: set[str]) -> set[str]:
    values = set(excluded)
    for fact in source.identity:
        if fact.name in _REMOVED_FACTS:
            values.update(fact.values)
    pieces: set[str] = set()
    for value in values:
        pieces |= _pieces(value)
    pieces.discard("")
    return pieces


def _identity_as_asked(source, copy, c: dict) -> bool:
    """The copy's identity matches this batch's choices (metadata only):

    * the pseudonym (or, without one, the copy's name) is stored;
    * what the batch cleared is empty;
    * patient details and location it kept: no value the original did not
      have (mef3io never edits them), and with no channel left out exactly
      as present as in the original (a left-out channel may have held the
      only value: inspect does not say per channel);
    * descriptions it kept: no more of them than the original had, and
      present where they must survive (:func:`_has_content`): each kept
      channel's own description channel by channel; the recording's and
      the reference descriptions when no channel was left out (they are
      stored per channel and inspect does not say which channel holds
      which: only a left-out channel may have held the surviving text);
    * the notes are the original's, minus those of channels left out, at
      the original's times plus the shift;
    * note text: none at all when the batch removed it; when it kept it,
      text on every note whose original text must survive, text only on
      notes whose original had text, and never more of it.
    A fact that could not be read (``present`` None) proves nothing."""
    facts = {f.name: f for f in copy.identity}
    originals = {f.name: f for f in source.identity}
    excluded = set(c.get("exclude", ()))
    removed = _removed_values(source, excluded)

    def known(name):
        fact = facts.get(name)
        return fact if fact is not None and fact.present is not None else None

    def must_survive(name) -> bool:
        original = originals.get(name)
        return original is not None and any(_has_content(v, removed) for v in original.values)

    stored = known("anonymized_name")
    if stored is None or set(stored.values) != {c.get("pseudonym") or c["name"]}:
        return False
    cleared = set(c.get("clear", CLEAR_ALL))
    for choice, names in _CLEARED_FACTS.items():
        for name in names:
            fact = known(name)
            if fact is None:
                return False
            original = originals.get(name)
            if choice in cleared:
                if fact.present:
                    return False
                continue
            if original is None or original.present is None:
                return False
            if choice != CLEAR_DESCRIPTIONS:
                if not set(fact.values) <= set(original.values):
                    return False
                if not excluded and fact.present != original.present:
                    return False
                continue
            if fact.occurrences > original.occurrences:
                return False
            if name != "channel_description" and not excluded:
                if must_survive(name) and not fact.present:
                    return False
        if choice == CLEAR_DESCRIPTIONS and choice not in cleared:
            kept = {ch.name: ch.description for ch in copy.channels}
            for ch in source.channels:
                if ch.name in kept and _has_content(ch.description, removed):
                    if not kept[ch.name]:
                        return False

    shift = int(c["shift"])
    expected = sorted(_note_key(n, shift) for n in source.notes if n.channel not in excluded)
    if copy.notes_problem is not None or sorted(map(_note_key, copy.notes)) != expected:
        return False
    text = known("record_text")
    if text is None or any(n.locked for n in copy.notes):
        return False
    if c.get("text", TEXT_DROP) == TEXT_DROP:
        return not text.present and not any(n.text for n in copy.notes)
    original_text = originals.get("record_text")
    kept_notes = [n for n in source.notes if n.channel not in excluded]
    with_text = {_note_key(n, shift) for n in kept_notes if n.text}
    needed = Counter(_note_key(n, shift) for n in kept_notes if _has_content(n.text, removed))
    have = Counter(_note_key(n) for n in copy.notes if n.text)
    return (
        original_text is not None
        and text.occurrences <= original_text.occurrences
        and all(key in with_text for key in have)
        and all(have[key] >= count for key, count in needed.items())
    )


def made_by_this_batch(checks: list[dict], cancel=None) -> list[bool]:
    """For each check (:func:`result_check`), whether the result at its
    ``path`` is exactly the one this batch was making:

    * written last by the anonymizer (its history), and fully readable;
    * the copy's name stored inside it;
    * starting at the original's start plus the shift;
    * holding exactly the original's channels minus those left out;
    * protected exactly as asked: it opens with the copies' full-access
      password and not without one when passwords were set, and it is not
      protected when none were;
    * its identity as asked (:func:`_identity_as_asked`): the pseudonym,
      the patient details, location and descriptions cleared or kept, the
      notes and their text removed or kept.

    Anything else (a folder or archive someone put there, even another
    anonymized copy of the same original) is never ours to remove or to call
    made. Details only, folders and single files alike."""
    from .inspect import inspect_recording
    from .recording import ACCESS_FULL, ACCESS_OPEN

    out = []
    for c in checks:
        if cancel is not None:
            cancel.raise_if_cancelled()
        source = inspect_recording(
            c["source"], c.get("source_password", ""), cancel=cancel, detect_legacy=False
        ).details
        if source is None or source.start is None:
            out.append(False)
            continue
        expected_channels = sorted(
            ch.name for ch in source.channels if ch.name not in set(c.get("exclude", ()))
        )
        output = c.get("output_password")
        if output:
            plain = inspect_recording(c["path"], "", cancel=cancel, detect_legacy=False)
            copy = inspect_recording(
                c["path"], output[1], cancel=cancel, detect_legacy=False
            ).details
            protection_ok = plain.needs_password and copy is not None and copy.access == ACCESS_FULL
        else:
            copy = inspect_recording(c["path"], "", cancel=cancel, detect_legacy=False).details
            protection_ok = copy is not None and copy.access == ACCESS_OPEN
        if copy is None or not protection_ok:
            out.append(False)
            continue
        stored = copy.identity_fact("session_name")
        names = set(stored.values) if stored is not None else set()
        out.append(
            copy.made_by_anonymizer
            and c["name"] in names
            and copy.start == source.start + c["shift"]
            and sorted(ch.name for ch in copy.channels) == expected_channels
            and _identity_as_asked(source, copy, c)
        )
    return out


def free_bytes(folder: str) -> int | None:
    try:
        return shutil.disk_usage(folder).free
    except OSError:
        return None


@dataclass(frozen=True)
class BatchItem:
    """One recording of the batch."""

    request: CopyRequest = field(repr=False)
    #: Where the delivered result goes: the ``.mefd`` folder, the
    #: ``.mefd.tar``, or (bundle) the bundle's path.
    final: str = field(repr=False)
    #: The opaque work folder of this recording ("" for folders).
    work: str = field(default="", repr=False)
    estimate: int = 0
    #: For the date key and the summary (never logged).
    original_name: str = field(default="", repr=False)
    original_start: int = field(default=0, repr=False)
    original_end: int = field(default=0, repr=False)


@dataclass(frozen=True)
class Batch:
    output: str
    items: tuple[BatchItem, ...] = field(repr=False)
    destination: str = field(default="", repr=False)
    work_folder: str = field(default="", repr=False)
    bundle: str = field(default="", repr=False)
    keep_awake: bool = True


def plan_batch(
    output: str,
    requests: list[CopyRequest],
    destination: str,
    work_folder: str,
    estimates: dict[str, int],
    originals: dict[str, tuple[str, int, int]],
    bundle_name: str = "",
    batch_id: str | None = None,
) -> Batch:
    """Where everything goes. For archives and the bundle, mef3io writes
    each copy into ``<work>/work-<batch>-<n>/<name>.mefd``."""
    batch_id = batch_id or secrets.token_hex(6)
    items = []
    for n, r in enumerate(requests):
        name = r.name
        if output == FOLDERS:
            final = os.path.join(destination, f"{name}.mefd")
            work = ""
            request = replace(r, destination=final)
        else:
            work = os.path.join(work_folder, f"{JOB_PREFIX}{batch_id}-{n}")
            request = replace(r, destination=os.path.join(work, f"{name}.mefd"))
            if output == ARCHIVES:
                final = os.path.join(destination, f"{name}.mefd.tar")
            else:
                final = os.path.join(destination, bundle_name)
        original = originals.get(r.key, ("", 0, 0))
        items.append(
            BatchItem(
                request=request,
                final=final,
                work=work,
                estimate=int(estimates.get(r.key, 0)),
                original_name=original[0],
                original_start=int(original[1]),
                original_end=int(original[2]),
            )
        )
    return Batch(
        output=output,
        items=tuple(items),
        destination=destination,
        work_folder=work_folder,
        bundle=os.path.join(destination, bundle_name) if output == BUNDLE else "",
    )


def space_problems(
    batch: Batch,
    free_work: int | None,
    free_destination: int | None,
    same_volume: bool = False,
) -> list[str]:
    """Free space, checked before starting (plain words). When the work
    folder and the destination are on one drive, the work copy and the
    results share it: the total plus the largest copy must fit."""
    out = []
    from .timefmt import format_size

    total = int(sum(i.estimate for i in batch.items) * MARGIN) + EXTRA_BYTES
    if same_volume and batch.output != FOLDERS and batch.items:
        total += int(max(i.estimate for i in batch.items) * MARGIN)
    if free_destination is not None and free_destination < total:
        out.append(
            f"The destination has {format_size(free_destination)} free; the copies need about "
            f"{format_size(total)}. Free some space or choose another folder."
        )
    if batch.output != FOLDERS and batch.items:
        largest = int(max(i.estimate for i in batch.items) * MARGIN) + EXTRA_BYTES
        if free_work is not None and free_work < largest:
            out.append(
                f"The work folder has {format_size(free_work)} free; the largest recording needs "
                f"about {format_size(largest)} there. Free some space or choose another work "
                "folder in Settings."
            )
    return out


def destination_problems(batch: Batch) -> list[str]:
    """Names already taken at the destination (``stat`` only)."""
    out = []
    finals = {i.final for i in batch.items}
    for final in sorted(finals):
        if os.path.lexists(final):
            out.append(
                f"{os.path.basename(final)} is already in the destination folder. Choose "
                "another name or folder."
            )
        if batch.output == FOLDERS and os.path.lexists(final + ".partial"):
            out.append(
                f"An unfinished copy ({os.path.basename(final)}.partial) is in the way. "
                "Delete it if no copy is running."
            )
    return out


# --------------------------------------------------------------------------
# The journal's sealed payload (passwords never included)


def to_payload(batch: Batch, bundle_temporary: str = "", bundle_offset: int = 0) -> dict:
    items = []
    for i in batch.items:
        r = asdict(i.request)
        r["needs_password"] = bool(r.pop("password"))
        r["protected"] = r.pop("output_password") is not None
        items.append(
            {
                "request": r,
                "final": i.final,
                "work": i.work,
                "estimate": i.estimate,
            }
        )
    return {
        "output": batch.output,
        "destination": batch.destination,
        "work_folder": batch.work_folder,
        "bundle": batch.bundle,
        "keep_awake": batch.keep_awake,
        "bundle_temporary": bundle_temporary,
        "bundle_offset": bundle_offset,
        "items": items,
    }


def from_payload(
    payload: dict, password_for, output_password, originals: dict | None = None
) -> Batch:
    """The batch again. ``password_for(source)`` gives the original's
    password when it needs one; ``output_password`` is the copies' pair
    (both typed again by the user: never journaled). ``originals`` (key ->
    (name, start, end), read again from the originals) fills in what the
    date key needs: the journal never holds original names or dates."""
    originals = originals or {}
    items = []
    for raw in payload["items"]:
        r = dict(raw["request"])
        needs = r.pop("needs_password", False)
        protected = r.pop("protected", False)
        request = CopyRequest(
            key=r["key"],
            source=r["source"],
            destination=r["destination"],
            shift_us=int(r["shift_us"]),
            pseudonym=r.get("pseudonym", ""),
            clear=tuple(r.get("clear", ())),
            text=r.get("text", "drop"),
            password=password_for(r["source"]) if needs else "",
            output_password=output_password if protected else None,
            exclude_channels=tuple(r.get("exclude_channels", ())),
        )
        name, start, end = originals.get(request.key, ("", 0, 0))
        items.append(
            BatchItem(
                request=request,
                final=raw["final"],
                work=raw["work"],
                estimate=int(raw["estimate"]),
                original_name=name,
                original_start=int(start),
                original_end=int(end),
            )
        )
    return Batch(
        output=payload["output"],
        items=tuple(items),
        destination=payload["destination"],
        work_folder=payload["work_folder"],
        bundle=payload.get("bundle", ""),
        keep_awake=bool(payload.get("keep_awake", True)),
    )


def needs_passwords(payload: dict) -> tuple[list[int], bool]:
    """(the positions of the originals that need their password, whether
    the copies are protected)."""
    items = payload.get("items", [])
    return (
        [n for n, i in enumerate(items) if i["request"].get("needs_password")],
        any(i["request"].get("protected") for i in items),
    )


def without_passwords(batch: Batch) -> Batch:
    """The batch with every password removed (what the page may keep)."""
    return replace(
        batch,
        items=tuple(
            replace(i, request=replace(i.request, password="", output_password=None))
            for i in batch.items
        ),
    )


def with_originals(batch: Batch, originals: dict) -> Batch:
    """The batch with each recording's name and dates read again."""
    items = []
    for i in batch.items:
        name, start, end = originals.get(
            i.request.key, (i.original_name, i.original_start, i.original_end)
        )
        items.append(replace(i, original_name=name, original_start=start, original_end=end))
    return replace(batch, items=tuple(items))
