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

    @property
    def channel_count(self) -> int:
        return len(self.channels)


class RecordingSource(abc.ABC):
    """Interface to a place recordings live."""

    #: Shown to users, e.g. "This computer".
    display_name: str = ""

    @abc.abstractmethod
    def summarize(
        self, location: str, password: str = "", cancel: CancelToken | None = None
    ) -> RecordingSummary:
        """Return a summary of the recording at ``location`` without reading
        its signal."""


class LocalRecordingSource(RecordingSource):
    """Recordings on this computer (``.mefd`` folders or ``.mefd.tar`` files)."""

    display_name = "This computer"

    def summarize(
        self, location: str | os.PathLike, password: str = "", cancel: CancelToken | None = None
    ) -> RecordingSummary:
        import mef3io

        cancel = cancel or CancelToken()
        cancel.raise_if_cancelled()
        channels: list[ChannelSummary] = []
        with mef3io.Reader(os.fspath(location), password=password) as reader:
            for name in reader.channels:
                cancel.raise_if_cancelled()
                info = reader.info(name)
                channels.append(ChannelSummary(name, float(info["sampling_frequency"])))
        return RecordingSummary(tuple(channels))
