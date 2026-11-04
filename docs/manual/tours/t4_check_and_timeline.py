"""T4: look at a recording and check it for problems (short Explore tour)."""

from _common import open_demo_steps
from tourlib import NIGHTS, Step, Tour, click, pick_rows, wait


def _checked(ctx) -> bool:
    page = ctx.window.explorer
    entry = page.current_entry()
    return entry is not None and entry.check is not None and not entry.busy


def build() -> Tour:
    steps = [
        *open_demo_steps(),
        Step(
            "Select a recording",
            "Click a recording in the list to see its details on the right.",
            do=[pick_rows([NIGHTS[1]])],
            focus=lambda ctx: ctx.window.explorer.table,
        ),
        Step(
            "Look at the timeline",
            "The Timeline tab shows when the recording has data. The gap in the middle "
            "of the night is a stretch with no data.",
            do=[click("Timeline")],
            focus=lambda ctx: ctx.window.explorer.tabs,
        ),
        Step(
            "Notes and events",
            "Notes and events lists what was written down during the recording.",
            do=[click("Notes and events")],
            focus=lambda ctx: ctx.window.explorer.tabs,
        ),
        Step(
            "Check for problems",
            "Open the Check for problems tab and choose Check now. The app compares "
            "what the recording says about itself with its data. It never changes the "
            "recording.",
            do=[
                click("Check for problems", "open the Check for problems tab"),
                click("Check now"),
                wait(_checked, "the check to finish", 120),
            ],
            focus=lambda ctx: ctx.window.explorer.check_panel,
            hold=4.5,
        ),
        Step(
            "Several recordings on one timeline",
            "Select several recordings and choose Combined timeline to see them on one "
            "time axis: gaps and recordings that overlap in time are easy to spot.",
            do=[
                pick_rows(list(NIGHTS)),
                click("Combined timeline"),
            ],
            focus=lambda ctx: ctx.window.explorer.combined,
            hold=4.5,
        ),
    ]
    return Tour(
        key="t4",
        title="Check a recording for problems and look at the timeline",
        video_title="check-and-timeline",
        summary=(
            "Look at what is in a recording, check it for problems, and put several "
            "recordings on one timeline."
        ),
        steps=steps,
    )


TOUR = build()
