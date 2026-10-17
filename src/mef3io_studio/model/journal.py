"""The batch journal: what lets an interrupted anonymizing batch continue.

A batch can run for hours, unattended. If the app is closed, crashes or the
power goes, the next start offers "Continue the batch: 4 of 7 done". For
that, a small journal in the app's per-user data folder records the batch
and which recordings are finished.

**What it holds in clear:** opaque ids only: a random batch id, a random id
per recording, how many there are and each one's state (``waiting``,
``done`` or ``failed``), plus a version number. No names, no paths, no dates.

**Everything else** (where each recording is, the new names, the shifts,
the channels to leave out, where the copies go, the original dates for the
date key) is in one sealed blob: encrypted with a random 256-bit key kept in
a separate file in the same app-data folder (``journal.key``, readable by
its owner only on POSIX), as a SHA-256 keystream, and sealed with an
HMAC-SHA-256, so the app can read it back and notices any change. This keeps
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
from dataclasses import dataclass, field

#: Override the app-data folder (tests).
DATA_DIR_ENV = "MEF3IO_STUDIO_DATA_DIR"
APP_NAME = "mef3io-studio"
APP_AUTHOR = "BNEL"
VERSION = 1

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


def _key() -> bytes:
    path = os.path.join(data_dir(), "journal.key")
    try:
        with open(path, "rb") as handle:
            key = handle.read()
        if len(key) == 32:
            return key
    except OSError:
        pass
    key = secrets.token_bytes(32)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "wb") as handle:
        handle.write(key)
    os.replace(path + ".tmp", path)
    return key


def _stream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def seal(payload: dict) -> str:
    """Encrypt and authenticate ``payload`` (JSON) with the per-user key."""
    key = _key()
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    nonce = secrets.token_bytes(16)
    body = bytes(a ^ b for a, b in zip(raw, _stream(key, nonce, len(raw)), strict=True))
    tag = hmac.new(key, nonce + body, hashlib.sha256).digest()
    return base64.b64encode(nonce + tag + body).decode("ascii")


def unseal(blob: str) -> dict | None:
    """The payload, or None when it cannot be read back (another key, or
    changed)."""
    try:
        data = base64.b64decode(blob.encode("ascii"), validate=True)
        nonce, tag, body = data[:16], data[16:48], data[48:]
        key = _key()
        if not hmac.compare_digest(tag, hmac.new(key, nonce + body, hashlib.sha256).digest()):
            return None
        raw = bytes(a ^ b for a, b in zip(body, _stream(key, nonce, len(body)), strict=True))
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
    A journal that cannot be read back is removed (it can never be used)."""
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
