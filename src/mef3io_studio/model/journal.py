"""The batch journal: what lets an interrupted anonymizing batch continue.

A batch can run for hours, unattended. If the app is closed, crashes or the
power goes, the next start offers "Continue the batch: 4 of 7 done". For
that, a small journal in the app's per-user data folder records the batch
and which recordings are finished.

**What it holds in clear:** opaque ids only: a random batch id, a random id
per recording, how many there are and each one's state (``waiting``,
``done`` or ``failed``), plus a version number. No names, no paths, no dates.

**Everything else** (where each recording is, the new names, the shifts,
the channels to leave out, where the copies and the temporary files go) is
in one sealed blob. Original dates and names are NOT in it: on Continue they
are read again from the originals (``inspect``), for the date key. The blob
is encrypted with a SHA-256 keystream and authenticated with HMAC-SHA-256,
under two keys derived from one random 256-bit key (``HMAC(key, "enc")`` and
``HMAC(key, "mac")``). That key is in a separate file in the same app-data
folder (``journal.key``): on Windows protected with DPAPI
(``CryptProtectData``, this user only), elsewhere readable by its owner only
(0600). This keeps
names, paths and dates out of anything someone might copy, attach or back up
by accident; it is not protection against someone who has both files (the
same account on the same computer). **Passwords are never in the journal**:
on Continue the user types them again.

The journal is written under a temporary name and renamed (never half a
journal), and deleted when the batch ends (done, or stopped on purpose).
Plain Python, no Qt. Never logs anything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
from dataclasses import dataclass, field

#: Override the app-data folder (tests).
DATA_DIR_ENV = "MEF3IO_STUDIO_DATA_DIR"
APP_NAME = "mef3io-studio"
APP_AUTHOR = "BNEL"
VERSION = 2

WAITING = "waiting"
DONE = "done"
FAILED = "failed"


def data_dir() -> str:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        root = override
    else:
        from platformdirs import user_data_dir

        root = user_data_dir(APP_NAME, APP_AUTHOR)
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root


def journal_dir() -> str:
    path = os.path.join(data_dir(), "batches")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI: protect (or unprotect) bytes for this user."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not call(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("the key could not be protected")
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        ctypes.windll.kernel32.LocalFree(out.data)  # type: ignore[attr-defined]


class KeyUnavailable(Exception):
    """The app's sealing key exists but cannot be read (permissions, a
    damaged file, Windows could not unprotect it). Nothing sealed can be read
    or written until it can: the key is never replaced, because that would
    make every journal, the work-folder setting and the list of temporary
    files unreadable for good. Deliberately not an OSError."""


def _key() -> bytes:
    """The sealing key: created only when there is none yet. Any other
    problem fails closed (:class:`KeyUnavailable`), leaving the file as it
    is."""
    path = os.path.join(data_dir(), "journal.key")
    windows = sys.platform == "win32"
    try:
        with open(path, "rb") as handle:
            stored = handle.read()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        stored = _dpapi(key, True) if windows else key
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "wb") as handle:
            handle.write(stored)
        os.replace(path + ".tmp", path)
        return key
    except OSError as exc:
        raise KeyUnavailable("the key file cannot be read") from exc
    try:
        key = _dpapi(stored, False) if windows else stored
    except OSError as exc:
        raise KeyUnavailable("the key could not be unprotected") from exc
    if len(key) != 32:
        raise KeyUnavailable("the key file is damaged")
    return key


KEY_PROBLEM = (
    "The app could not open its own sealed notes (the key file in its data folder could not "
    "be read). Nothing was removed or changed: interrupted batches, the chosen work folder "
    "and the list of unfinished files are kept as they are. Check that your account can "
    "read the app's data folder, then start the app again."
)


def _keys() -> tuple[bytes, bytes]:
    """Separate keys for encrypting and for authenticating."""
    key = _key()
    return (
        hmac.new(key, b"enc", hashlib.sha256).digest(),
        hmac.new(key, b"mac", hashlib.sha256).digest(),
    )


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def seal(payload: dict) -> str:
    """Encrypt and authenticate ``payload`` (JSON) with the per-user key."""
    enc, mac = _keys()
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    nonce = secrets.token_bytes(16)
    body = bytes(a ^ b for a, b in zip(raw, _stream(enc, nonce, len(raw)), strict=True))
    tag = hmac.new(mac, nonce + body, hashlib.sha256).digest()
    return base64.b64encode(nonce + tag + body).decode("ascii")


def unseal(blob: str) -> dict | None:
    """The payload, or None when it cannot be read back (another key, or
    changed)."""
    try:
        data = base64.b64decode(blob.encode("ascii"), validate=True)
        nonce, tag, body = data[:16], data[16:48], data[48:]
        enc, mac = _keys()
        if not hmac.compare_digest(tag, hmac.new(mac, nonce + body, hashlib.sha256).digest()):
            return None
        raw = bytes(a ^ b for a, b in zip(body, _stream(enc, nonce, len(body)), strict=True))
        return json.loads(raw.decode("utf-8"))
    except (ValueError, OSError, UnicodeDecodeError):
        return None


@dataclass
class Journal:
    """One batch. ``states`` is in the batch's order; ``payload`` (sealed
    on disk) is whatever the pipeline needs to continue."""

    batch_id: str = field(default_factory=lambda: secrets.token_hex(8))
    item_ids: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict, repr=False)

    @property
    def path(self) -> str:
        return os.path.join(journal_dir(), f"{self.batch_id}.json")

    @property
    def done(self) -> int:
        return sum(1 for s in self.states if s != WAITING)

    @property
    def total(self) -> int:
        return len(self.states)

    @property
    def finished(self) -> bool:
        return all(s != WAITING for s in self.states)

    def mark(self, index: int, state: str) -> None:
        self.states[index] = state
        self.save()

    def save(self) -> None:
        record = {
            "version": VERSION,
            "batch": self.batch_id,
            "items": [
                {"id": i, "state": s} for i, s in zip(self.item_ids, self.states, strict=True)
            ],
            "sealed": seal(self.payload),
        }
        temporary = self.path + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def delete(self) -> None:
        for path in (self.path, self.path + ".tmp"):
            try:
                os.unlink(path)
            except OSError:
                pass

    @classmethod
    def new(cls, count: int, payload: dict) -> Journal:
        journal = cls(
            item_ids=[secrets.token_hex(6) for _ in range(count)],
            states=[WAITING] * count,
            payload=payload,
        )
        journal.save()
        return journal


def unfinished() -> list[Journal]:
    """Journals of batches that did not end, readable with this user's key.
    A journal that cannot be read back with a working key is removed (it can
    never be used). If the key itself cannot be read, :class:`KeyUnavailable`
    is raised and nothing is removed."""
    out = []
    try:
        names = sorted(os.listdir(journal_dir()))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(journal_dir(), name)
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
            _key()  # fails closed (KeyUnavailable) before anything is judged unreadable
            payload = unseal(record["sealed"])
            items = record["items"]
            journal = Journal(
                batch_id=str(record["batch"]),
                item_ids=[str(i["id"]) for i in items],
                states=[str(i["state"]) for i in items],
                payload=payload or {},
            )
        except (OSError, ValueError, KeyError, TypeError):
            payload, journal = None, None
        if payload is None or journal is None or record.get("version") != VERSION:
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        if not journal.finished:
            out.append(journal)
    return out


# --------------------------------------------------------------------------
# Temporary files at the destination, in one sealed list, so the app can
# always find and remove its own leftovers (even without a journal).


def _temporaries_path() -> str:
    return os.path.join(data_dir(), "temporaries.json")


def known_temporaries() -> list[str]:
    try:
        with open(_temporaries_path(), encoding="utf-8") as handle:
            value = unseal(json.load(handle)["sealed"])
    except (OSError, ValueError, KeyError, TypeError):
        return []
    # (KeyUnavailable propagates: the list is never rewritten without its key.)
    return list(value.get("paths", [])) if value else []


def _save_temporaries(paths: list[str]) -> None:
    path = _temporaries_path()
    if not paths:
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8") as handle:
        json.dump({"sealed": seal({"paths": paths})}, handle)
    os.replace(path + ".tmp", path)


def note_temporary(path: str) -> None:
    paths = known_temporaries()
    if path not in paths:
        _save_temporaries([*paths, path])


def forget_temporary(path: str) -> None:
    paths = known_temporaries()
    if path in paths:
        _save_temporaries([p for p in paths if p != path])


# Work copies KEPT on purpose because their archive did not match them:
# checked copies, told apart from never-checked leftovers at start.


def _kept_path() -> str:
    return os.path.join(data_dir(), "kept.json")


def kept_copies() -> list[str]:
    try:
        with open(_kept_path(), encoding="utf-8") as handle:
            value = unseal(json.load(handle)["sealed"])
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return list(value.get("paths", [])) if value else []


def _save_kept(paths: list[str]) -> None:
    path = _kept_path()
    if not paths:
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8") as handle:
        json.dump({"sealed": seal({"paths": paths})}, handle)
    os.replace(path + ".tmp", path)


def note_kept_copy(path: str) -> None:
    paths = kept_copies()
    if path not in paths:
        _save_kept([*paths, path])


def forget_kept_copy(path: str) -> None:
    paths = kept_copies()
    if path in paths:
        _save_kept([p for p in paths if p != path])
