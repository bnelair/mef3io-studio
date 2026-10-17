"""The unattended batch: anonymize, archive, check, tidy up, one recording
after another (mef3io-studio#5, "one unattended pipeline").

Three ways to deliver the copies:

* ``folders``: one anonymized ``<name>.mefd`` folder per recording, written
  by mef3io straight into the chosen folder (as before);
* ``archives``: one ``<name>.mefd.tar`` per recording. mef3io anonymizes into
  the WORK FOLDER, ``mef3io.archive_session`` packs that copy under a
  temporary name next to its final one (``model.archive``), the archive is
  opened and compared with the checked work copy (channels, parts, start and
  end, sample counts), renamed, and only then is the work copy deleted;
* ``bundle``: one ``.tar`` holding every anonymized recording as its own
  folder (``model.bundle``): each checked work copy is added, the added
  member names and sizes are read back, then the work copy is deleted; the
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
import secrets
import shutil
from dataclasses import asdict, dataclass, field, replace

from .anonymize import CopyRequest, inside_any_recording

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


def space_problems(batch: Batch, free_work: int | None, free_destination: int | None) -> list[str]:
    """Free space, checked before starting (plain words)."""
    out = []
    from .timefmt import format_size

    total = int(sum(i.estimate for i in batch.items) * MARGIN) + EXTRA_BYTES
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
                "original": [i.original_name, i.original_start, i.original_end],
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


def from_payload(payload: dict, password_for, output_password) -> Batch:
    """The batch again. ``password_for(source)`` gives the original's
    password when it needs one; ``output_password`` is the copies' pair
    (both typed again by the user: never journaled)."""
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
        name, start, end = raw["original"]
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


def needs_passwords(payload: dict) -> tuple[bool, bool]:
    """(some original needs its password, the copies are protected)."""
    items = payload.get("items", [])
    return (
        any(i["request"].get("needs_password") for i in items),
        any(i["request"].get("protected") for i in items),
    )
