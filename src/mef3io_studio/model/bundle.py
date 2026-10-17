"""One ``.tar`` bundle that holds several anonymized recordings, for sharing.

The bundle is a CONTAINER, not MEF bytes: each anonymized recording goes in
as its own ``<name>.mefd/`` folder, exactly as on disk, and any tar tool
(7-Zip, the Windows and macOS built-ins, ``tar -xf``) gives back ready-to-use
recordings. mef3io cannot open the bundle itself (it opens one recording per
``.mefd.tar``); it must be extracted first. It is NOT a merge of recordings
into one.

Written with Python's ``tarfile``, following mef3io's ``archive_session``
rules so the result is deterministic: members sorted, every time 0, owner and
group 0 with no user or group names, folders 0755 and files 0644, plain
ustar headers (a pax header only where a name does not fit ustar). Files are
streamed in 1 MB chunks: a signal file is never read whole. The bundle is
written under a temporary name next to its final one and renamed only when
every recording is in it and its member list has been checked.

Plain Python, no Qt. Never logs anything (member names are recording names).
"""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import sys
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field

from .cancel import CancelToken

CHUNK = 1 << 20
#: Temporary bundles start with this (opaque: no name of a recording).
TEMP_PREFIX = ".mef3io-studio-bundle-"


#: Called with (path, start, end) just before a recording's bytes are
#: checked in any bundle (tests use it to damage what was written).
before_check = None


class BundleMismatch(RuntimeError):
    """The bundle does not hold what was written into it."""


@dataclass(frozen=True)
class Member:
    name: str  # path inside the bundle, "/" separated
    size: int  # 0 for a folder
    is_dir: bool


def members_of(folder: str) -> list[Member]:
    """What adding ``folder`` (a ``<name>.mefd`` recording) puts in the
    bundle, sorted: the folder itself, its sub-folders and files (sizes
    from ``stat``; nothing is read)."""
    root = os.path.basename(os.path.normpath(folder))
    out = [Member(root, 0, True)]
    for here, dirs, files in os.walk(folder):
        dirs.sort()
        rel = os.path.relpath(here, folder)
        base = root if rel == "." else f"{root}/{rel.replace(os.sep, '/')}"
        for d in dirs:
            out.append(Member(f"{base}/{d}", 0, True))
        for f in sorted(files):
            path = os.path.join(here, f)
            if os.path.islink(path):
                continue  # never follow links out of a recording
            out.append(Member(f"{base}/{f}", os.stat(path).st_size, False))
    return sorted(out, key=lambda m: m.name)


def _info(member: Member) -> tarfile.TarInfo:
    info = tarfile.TarInfo(member.name)
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    if member.is_dir:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.size = 0
    else:
        info.type = tarfile.REGTYPE
        info.mode = 0o644
        info.size = member.size
    return info


class _Chunked:
    """A file read in chunks, with Stop and progress between chunks."""

    def __init__(self, handle, cancel: CancelToken, on_bytes: Callable[[int], None] | None):
        self.handle = handle
        self.cancel = cancel
        self.on_bytes = on_bytes
        #: SHA-256 of exactly the bytes handed to the bundle.
        self.digest = hashlib.sha256()

    def read(self, n: int = -1) -> bytes:
        self.cancel.raise_if_cancelled()
        data = self.handle.read(min(n, CHUNK) if n and n > 0 else CHUNK)
        self.digest.update(data)
        if self.on_bytes is not None and data:
            self.on_bytes(len(data))
        return data


def hash_folder(
    folder: str,
    cancel: CancelToken | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> dict[str, str]:
    """SHA-256 of every file of a recording folder, by its path inside the
    folder ("/" separated), read in 1 MB chunks."""
    cancel = cancel or CancelToken()
    out = {}
    root = os.path.basename(os.path.normpath(folder))
    parent = os.path.dirname(os.path.normpath(folder))
    for m in members_of(folder):
        if m.is_dir:
            continue
        digest = hashlib.sha256()
        with open(os.path.join(parent, *m.name.split("/")), "rb") as handle:
            while True:
                cancel.raise_if_cancelled()
                data = handle.read(CHUNK)
                if not data:
                    break
                digest.update(data)
                if on_bytes is not None:
                    on_bytes(len(data))
        out[m.name[len(root) + 1 :]] = digest.hexdigest()
    return out


def tar_member_hashes(
    path: str,
    start: int = 0,
    end: int | None = None,
    cancel: CancelToken | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> dict[str, str]:
    """SHA-256 of every file member of a tar (or of the members between two
    offsets), by member name, re-read from the file in 1 MB chunks."""
    cancel = cancel or CancelToken()
    out = {}
    with open(path, "rb") as raw:
        raw.seek(start)
        view = _Limited(raw, end if end is not None else os.path.getsize(path))
        with tarfile.open(fileobj=view, mode="r:") as tar:
            for info in tar:
                if not info.isfile():
                    continue
                digest = hashlib.sha256()
                member = tar.extractfile(info)
                while True:
                    cancel.raise_if_cancelled()
                    data = member.read(CHUNK)
                    if not data:
                        break
                    digest.update(data)
                    if on_bytes is not None:
                        on_bytes(len(data))
                out[info.name] = digest.hexdigest()
    return out


def without_root(hashes: dict[str, str]) -> dict[str, str]:
    """Member names without their first folder ("S1.mefd/a/b" -> "a/b")."""
    return {n.split("/", 1)[1]: h for n, h in hashes.items() if "/" in n}


def hide_file(path: str) -> None:
    """Mark a temporary file hidden on Windows (elsewhere its name starts
    with a dot, which does the same)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a convenience
        pass


@dataclass
class BundleWriter:
    """Writes the bundle, one recording at a time. ``offset`` is where the
    next recording starts: after a stop or a crash the temporary bundle is
    cut back there, so a half-added recording never stays in it."""

    target: str
    temporary: str = ""
    offset: int = 0
    #: Every member written so far, for the final check.
    written: list[Member] = field(default_factory=list)
    #: Called with (path, start, end) just before the byte check (tests
    #: use it to damage what was written).
    before_check: Callable | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.temporary:
            folder = os.path.dirname(os.path.abspath(self.target)) or "."
            self.temporary = os.path.join(folder, f"{TEMP_PREFIX}{secrets.token_hex(16)}.tar")

    def _open(self):
        new = not os.path.exists(self.temporary)
        handle = open(self.temporary, "w+b" if new else "r+b")  # noqa: SIM115 - closed in add()
        if new:
            hide_file(self.temporary)
        handle.truncate(self.offset)
        handle.seek(self.offset)
        tar = tarfile.open(fileobj=handle, mode="w", format=tarfile.PAX_FORMAT)
        tar.copybufsize = CHUNK
        return handle, tar

    def add(
        self,
        folder: str,
        cancel: CancelToken | None = None,
        on_bytes: Callable[[int], None] | None = None,
    ) -> list[Member]:
        """Add one recording folder; check what landed (member names, types
        and sizes, read back from the headers only); return its members. On
        any failure or Stop, the bundle is cut back to before this recording."""
        cancel = cancel or CancelToken()
        members = members_of(folder)
        parent = os.path.dirname(os.path.normpath(folder))
        handle, tar = self._open()
        hashes: dict[str, str] = {}
        try:
            for m in members:
                cancel.raise_if_cancelled()
                info = _info(m)
                if m.is_dir:
                    tar.addfile(info)
                    continue
                with open(os.path.join(parent, *m.name.split("/")), "rb") as f:
                    chunked = _Chunked(f, cancel, on_bytes)
                    tar.addfile(info, chunked)
                hashes[m.name] = chunked.digest.hexdigest()
            # Members only: the end-of-archive blocks come at finish().
            tar.fileobj = None  # keep tarfile from writing them on close
            handle.flush()
            os.fsync(handle.fileno())
            end = handle.tell()
        except BaseException:
            handle.close()
            self._cut_back()
            raise
        handle.close()
        found = self._read_members(self.offset, end)
        if [(m.name, m.size, m.is_dir) for m in found] != [
            (m.name, m.size, m.is_dir) for m in members
        ]:
            self._cut_back()
            raise BundleMismatch("the bundle does not hold the recording's files as written")
        # Every byte, read back from the bundle on disk, against what was
        # read from the checked copy: only then may the copy be deleted.
        hook = self.before_check or before_check
        if hook is not None:
            hook(self.temporary, self.offset, end)
        if tar_member_hashes(self.temporary, self.offset, end, cancel) != hashes:
            self._cut_back()
            raise BundleMismatch("the bundle's bytes do not match the checked copy")
        self.offset = end
        self.written += members
        return members

    def _cut_back(self) -> None:
        try:
            with open(self.temporary, "r+b") as handle:
                handle.truncate(self.offset)
        except OSError:
            pass

    def _read_members(self, start: int, end: int) -> list[Member]:
        """The members between two offsets, from their headers only (the
        file data is skipped by seeking)."""
        out = []
        with open(self.temporary, "rb") as handle:
            handle.seek(start)
            limited = _Limited(handle, end)
            with tarfile.open(fileobj=limited, mode="r:") as tar:
                for info in tar:
                    out.append(Member(info.name, info.size if info.isfile() else 0, info.isdir()))
        return out

    def finish(self) -> str:
        """End the archive, check its whole member list, and give it its
        final name without replacing anything. Returns the final path."""
        with open(self.temporary, "r+b") as handle:
            handle.truncate(self.offset)
            handle.seek(self.offset)
            # Two zero blocks end a tar; pad to tarfile's record size.
            end = self.offset + 2 * tarfile.BLOCKSIZE
            padded = -(-end // tarfile.RECORDSIZE) * tarfile.RECORDSIZE
            handle.write(b"\0" * (padded - self.offset))
            handle.flush()
            os.fsync(handle.fileno())
        found = self._read_members(0, os.path.getsize(self.temporary))
        expected = [(m.name, m.size, m.is_dir) for m in self.written]
        if [(m.name, m.size, m.is_dir) for m in found] != expected:
            raise BundleMismatch("the finished bundle does not list what was written into it")
        return publish(self.temporary, self.target)

    def discard(self) -> None:
        try:
            os.unlink(self.temporary)
        except OSError:
            pass


class _Limited:
    """A read-only view of a file that ends at ``end`` (headers past the
    last checked recording are not looked at)."""

    def __init__(self, handle, end: int):
        self.handle = handle
        self.end = end

    def read(self, n: int = -1) -> bytes:
        left = self.end - self.handle.tell()
        if left <= 0:
            return b""
        return self.handle.read(left if n is None or n < 0 else min(n, left))

    def seek(self, pos: int, whence: int = 0) -> int:
        return self.handle.seek(pos, whence)

    def tell(self) -> int:
        return self.handle.tell()


def publish(temporary: str, target: str) -> str:
    """Rename a finished file to its final name, never replacing anything
    there (a link where the file system has them, else a reserved name)."""
    try:
        os.link(temporary, target)
    except FileExistsError:
        raise FileExistsError(errno.EEXIST, "the target already exists") from None
    except OSError:
        if sys.platform == "win32":
            os.rename(temporary, target)  # never replaces on Windows
            return target
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        os.rename(temporary, target)
        return target
    os.unlink(temporary)
    return target
