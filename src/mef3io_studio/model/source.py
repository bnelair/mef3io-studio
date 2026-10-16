"""Where recordings come from.

The UI and the job layer talk to a :class:`RecordingSource`, never to mef3io
directly. Today there is one implementation, :class:`LocalRecordingSource`
(files on this computer, via mef3io's public API). A server-backed source
(mef3io-server, issue #10) can implement the same interface later without the
screens changing.

Rules every source follows: it only asks for what it needs (metadata, never a
whole recording), and it never logs locations or recording contents.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass

from .cancel import CancelToken, stopped_by_library
from .problems import CheckReport
from .progress import byte_progress
from .quiet import library_warnings_caught
from .recording import InspectOutcome


@dataclass(frozen=True)
class ChannelSummary:
    name: str
    sampling_frequency: float


@dataclass(frozen=True)
class RecordingSummary:
    """A deliberately small summary (from ``mef3io.inspect()``). The
    Explorer's fuller details come from :mod:`.inspect`."""

    channels: tuple[ChannelSummary, ...]
    #: The recording leaves some size declarations unset (common in files
    #: from older tools). Reading is unaffected; other programs may struggle.
    declarations_incomplete: bool = False

    @property
    def channel_count(self) -> int:
        return len(self.channels)


class RecordingSource(abc.ABC):
    """Interface to a place recordings live."""

    #: Shown to users, e.g. "This computer".
    display_name: str = ""
    #: Run calls in a separate helper process. Needed while mef3io holds the
    #: Python GIL for a whole open (bnelair/mef3io#79): on a thread that would
    #: freeze the window. The source object must then be picklable.
    isolate: bool = False

    @abc.abstractmethod
    def summarize(
        self, location: str, password: str = "", cancel: CancelToken | None = None
    ) -> RecordingSummary:
        """Return a summary of the recording at ``location`` without reading
        its signal."""

    @abc.abstractmethod
    def inspect_many(
        self, requests: list[tuple[str, str]], send=None, cancel: CancelToken | None = None
    ) -> list[InspectOutcome]:
        """Everything the explorer shows about each ``(location, password)``,
        without reading any signal (see :mod:`.inspect`). With ``send``, each
        outcome is passed to it as soon as it is ready, and so is progress
        (:class:`~.progress.ByteProgress`) for the one being read. ``cancel``
        stops it from inside the library."""

    @abc.abstractmethod
    def check(
        self, location: str, password: str = "", send=None, cancel: CancelToken | None = None
    ) -> CheckReport:
        """Check the recording for problems. Reads only. ``send`` gets
        :class:`~.progress.ByteProgress`; ``cancel`` stops it."""


class LocalRecordingSource(RecordingSource):
    """Recordings on this computer (``.mefd`` folders or ``.mefd.tar`` files)."""

    display_name = "This computer"
    isolate = True  # until bnelair/mef3io#79 releases the GIL during an open / inspect

    def summarize(
        self, location: str | os.PathLike, password: str = "", cancel: CancelToken | None = None
    ) -> RecordingSummary:
        import mef3io

        cancel = cancel or CancelToken()
        cancel.raise_if_cancelled()
        # mef3io.inspect reads the details only. Strict: a summary is
        # all-or-nothing (the Explorer uses inspect_many, which is lenient).
        # Its warnings are caught: their text starts with the full path,
        # which must not reach the log. The declarations fact is returned as
        # a flag and logged by the caller in fixed, path-free words.
        with stopped_by_library(), library_warnings_caught():
            summary = mef3io.inspect(os.fspath(location), password, cancel=cancel.for_library())
        encryption = summary.encryption
        if encryption.get("encrypted") and encryption.get("access") == "none":
            raise mef3io.PasswordError("the recording needs its password")
        channels = tuple(
            ChannelSummary(c.name, float(c.sampling_frequency or 0.0)) for c in summary.channels
        )
        return RecordingSummary(channels, declarations_incomplete=bool(summary.declaration_issues))

    def inspect_many(
        self, requests: list[tuple[str, str]], send=None, cancel: CancelToken | None = None
    ) -> list[InspectOutcome]:
        from .inspect import inspect_many

        return inspect_many(requests, send=send, cancel=cancel)

    def check(
        self, location: str, password: str = "", send=None, cancel: CancelToken | None = None
    ) -> CheckReport:
        from .problems import check_recording

        progress = byte_progress(send) if send is not None else None
        return check_recording(os.fspath(location), password, cancel=cancel, progress=progress)
