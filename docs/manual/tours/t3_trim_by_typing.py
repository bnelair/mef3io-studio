"""T3: trim one recording by typing exact times."""

from _common import day, new_recording_step, open_demo_steps, saved_step, utc_text
from tourlib import Step, Tour, click, pick_rows, type_in


def build() -> Tour:
    start = day(8, 1)
    end = day(8, 4)
    steps = [
        *open_demo_steps(),
        Step(
            "Select one recording",
            "Click the recording you want to trim, then choose Join or trim these…",
            do=[pick_rows(["study-tuesday"])],
            then=[click("Join or trim these", "choose Join or trim these…")],
            focus=lambda ctx: ctx.window.explorer.table,
        ),
        Step(
            "Keep all channels",
            "We keep every channel here, so just go on.",
            then=[click("Next")],
            hold=2.0,
        ),
        Step(
            "Use a window on the map",
            "Choose the window set on the map. It has a Start and an End.",
            do=[click("The same window for all recordings")],
            focus=lambda ctx: ctx.window.joiner.keep.map,
        ),
        Step(
            "Type Start as a date and time",
            "Click the Start field under Date and time (UTC) and type the time, for "
            "example " + utc_text(start) + ". Press Enter. The line on the map, and the "
            "number next to it, move to the same moment.",
            do=[
                type_in(
                    lambda ctx: ctx.window.joiner.keep.date_edit["start"],
                    utc_text(start),
                    "type the Start time",
                )
            ],
            focus=lambda ctx: ctx.window.joiner.keep.window_box,
        ),
        Step(
            "Type End as a number",
            "You can also type the same moment as a whole number: microseconds since "
            "1 January 1970, UTC (the way the recording file stores times). Type it in "
            "the End field of the number column and press Enter.",
            do=[
                type_in(
                    lambda ctx: ctx.window.joiner.keep.number_edit["end"],
                    str(end),
                    "type the End as a number",
                )
            ],
            then=[click("Next")],
            focus=lambda ctx: ctx.window.joiner.keep.window_box,
        ),
        Step(
            "Keep the patient details",
            "This tour makes a plain trimmed copy, so we keep the patient details as in "
            "the original.",
            then=[click("Next")],
            hold=2.5,
        ),
        new_recording_step("tuesday-3-hours", one_file=False),
        saved_step(),
    ]
    return Tour(
        key="t3",
        title="Trim one recording by typing exact times",
        video_title="trim-by-typing-exact-times",
        summary=(
            "Keep only one stretch of a recording by typing its start and end, either as "
            "a date and time in UTC or as a whole number (the time the way the recording "
            "file stores it)."
        ),
        before=[
            "Times on the map are the real times of the recording, in UTC. Start is "
            "always before End; a time outside the recording is pulled back to its "
            "first or last moment."
        ],
        steps=steps,
    )


TOUR = build()
