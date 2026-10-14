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

from .cancel import CancelToken


@dataclass(frozen=True)
class ChannelSummary:
    name: str
    sampling_frequency: float


@dataclass(frozen=True)
class RecordingSummary:
    """A deliberately small summary. Fuller details come with mef3io.inspect()
    (mef3io#67) in the Explorer step."""

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


class LocalRecordingSource(RecordingSource):
    """Recordings on this computer (``.mefd`` folders or ``.mefd.tar`` files)."""

    display_name = "This computer"
    isolate = True  # until bnelair/mef3io#79 releases the GIL during an open

    def summarize(
        self, location: str | os.PathLike, password: str = "", cancel: CancelToken | None = None
    ) -> RecordingSummary:
        import mef3io

        cancel = cancel or CancelToken()
        cancel.raise_if_cancelled()
        channels: list[ChannelSummary] = []
        # warn_declarations=False: mef3io's warning text starts with the full
        # path, which must not reach the log. The same fact is returned as a
        # flag and logged by the caller in fixed, path-free words.
        with mef3io.Reader(
            os.fspath(location), password=password, warn_declarations=False
        ) as reader:
            for name in reader.channels:
                cancel.raise_if_cancelled()
                info = reader.info(name)
                channels.append(ChannelSummary(name, float(info["sampling_frequency"])))
            incomplete = bool(reader.declaration_issues)
        return RecordingSummary(tuple(channels), declarations_incomplete=incomplete)
