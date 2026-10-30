"""Opening the viewer from the app: one background task, one progress bar.

The explorer hands over its entries (paths, passwords, whether patient
details are shown); the recordings are opened by ``workers.tasks.open_viewer``
and the window appears when that succeeds. A refusal (recordings that cannot
share a timeline, different passwords, ...) arrives as a plain message.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import tzinfo

from PySide6.QtCore import QObject

from ..common.theme import Theme
from ..model.recording import location_key
from ..workers import tasks
from .window import ViewerWindow


def launch(
    jobs,
    source,
    entries: Sequence,
    *,
    settings=None,
    theme: Theme,
    zone: tzinfo,
    parent: QObject | None = None,
    on_window: Callable[[ViewerWindow], None] | None = None,
    on_failed: Callable[[object], None] | None = None,
    on_revealed: Callable[[object], None] | None = None,
    on_join: Callable[[list, int, int], None] | None = None,
):
    """Start opening ``entries`` (explorer ``RecordingEntry`` objects) in a
    viewer. ``on_revealed(entry)``: the viewer's Show button showed a
    recording's details (Explore's own switch, so the entry is told).
    ``on_join(keys, start, end)``: "Send to Join or trim". Returns the job
    handle."""
    paths = [e.location for e in entries]
    passwords = [e.password for e in entries]
    by_key = {location_key(e.location): e for e in entries}
    handle = jobs.submit("Opening the signal viewer", tasks.open_viewer, source, paths, passwords)

    def opened(data) -> None:
        def reveal(i: int) -> bool:
            entry = by_key.get(location_key(data.location_of(i)))
            return bool(entry is not None and entry.revealed)

        def show_details(i: int) -> None:
            entry = by_key.get(location_key(data.location_of(i)))
            if entry is not None and not entry.revealed:
                entry.revealed = True
                if on_revealed is not None:
                    on_revealed(entry)

        window = ViewerWindow(
            data,
            settings=settings,
            theme=theme,
            zone=zone,
            reveal=reveal,
            show_details=show_details,
            jobs=jobs,
            parent=parent,
        )
        if on_join is not None:
            window.join_requested.connect(
                lambda who, a, b: on_join(
                    [location_key(data.location_of(i)) for i in who], int(a), int(b)
                )
            )
        window.show()
        if on_window is not None:
            on_window(window)

    handle.succeeded.connect(opened)
    if on_failed is not None:
        handle.failed.connect(on_failed)
    return handle
