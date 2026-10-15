"""The explorer's per-recording state, kept in memory only.

Nothing here is ever written to disk or the log: not the location, not the
password, not whether patient details were shown. Closing the app forgets it
all (docs/design/explorer.md, "Recovery artifacts" and SECURITY.md).

Plain Python, no Qt.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model.errors import UserMessage
from ..model.problems import CheckReport
from ..model.recording import InspectOutcome, location_key, recording_name


@dataclass(eq=False)
class RecordingEntry:
    location: str
    #: None while its details are being read.
    outcome: InspectOutcome | None = None
    #: The password that opened it, so "Check for problems" can use it again.
    #: Memory only; registered with the log filter so it is never logged.
    password: str = ""
    #: "Show" was chosen for this recording's patient details.
    revealed: bool = False
    check: CheckReport | None = None
    check_error: UserMessage | None = None
    #: A task for this recording is running ("Reading…", "Checking…").
    busy: str = ""

    @property
    def key(self) -> str:
        return location_key(self.location)

    @property
    def name(self) -> str:
        return recording_name(self.location)

    @property
    def details(self):
        return self.outcome.details if self.outcome is not None else None

    @property
    def needs_password(self) -> bool:
        return self.outcome is not None and self.outcome.needs_password
