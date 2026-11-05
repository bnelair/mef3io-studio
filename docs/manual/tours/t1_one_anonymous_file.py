"""T1: the main use case, start to finish."""

from _common import (
    channels_step,
    day,
    new_recording_step,
    open_demo_steps,
    saved_step,
    select_and_join_steps,
    utc_text,
)
from tourlib import NIGHTS, Step, Tour, around, click, do, drag_line, type_in


def _anonymous_bits():
    from mef3io_studio.joiner.page import MODE_WINDOW, PRIVACY_ANONYMOUS  # noqa: F401

    return PRIVACY_ANONYMOUS


def _set_new_date(ctx) -> None:
    from PySide6.QtCore import QDate

    ctx.window.joiner.privacy.dates.new_date.setDate(QDate(2010, 3, 1))


def _open_result(ctx) -> None:
    ctx.window.joiner.open_result()
    explorer = ctx.window.explorer
    ctx.wait_until(
        lambda: (
            ctx.window.pages.currentWidget() is explorer
            and any(e.name == "overnight-set" and not e.busy for e in explorer.entries())
        ),
        60,
        "the new recording to open in Explore",
    )


def build() -> Tour:
    steps = [
        *open_demo_steps(),
        *select_and_join_steps(list(NIGHTS)),
        channels_step(["ECG", "EMG"]),
        Step(
            "Use one window for all recordings",
            "The map shows when each recording has data. To cut the same stretch out of "
            "all of them, choose the window set on the map. The Start and End lines now "
            "mark the whole time span.",
            do=[click("The same window for all recordings")],
            focus=lambda ctx: ctx.window.joiner.keep.map,
        ),
        Step(
            "Drag the Start line",
            "Drag the Start line to where the new recording should begin. Data before "
            "it is shaded and will not be kept.",
            do=[drag_line("start", day(6, 23))],
            focus=lambda ctx: ctx.window.joiner.keep.map,
        ),
        Step(
            "Type the End time",
            "Or type the time. Type the end as a date and time in UTC, then press "
            "Enter: the End line moves there. Every recording keeps only its own data "
            "between Start and End.",
            do=[
                type_in(
                    lambda ctx: ctx.window.joiner.keep.date_edit["end"],
                    utc_text(day(8, 23)),
                    "type the End time",
                )
            ],
            then=[click("Next")],
            focus=lambda ctx: ctx.window.joiner.keep.window_box,
        ),
        Step(
            "Make it anonymous",
            "Choose Make it anonymous so the new recording no longer says who the "
            "patient is: names, ID and recording place are cleared, and the text of "
            "notes and events is removed. Keep the time of day (UTC), so the dates can "
            "be moved in the next step.",
            do=[click(_anonymous_bits()), click("Keep the UTC time of day")],
            focus=around(lambda ctx: ctx.window.joiner.privacy.anonymous),
        ),
        Step(
            "Move the dates",
            "Dates identify a patient too. Every date moves by the same whole number of "
            "days, so the order and spacing of the nights stay as they were. Pick the "
            "new date for the first night; the preview shows the new dates.",
            do=[
                do(
                    _set_new_date,
                    lambda ctx: ctx.window.joiner.privacy.dates.new_date,
                    "pick the new date",
                ),
            ],
            focus=around(
                lambda ctx: ctx.window.joiner.privacy.dates_move,
                lambda ctx: ctx.window.joiner.privacy.dates,
            ),
        ),
        Step(
            "Notes and events",
            "Notes and events can hold text that names the patient. Under Notes and "
            "events, Keep them is chosen: they stay in the new recording, with the text "
            "removed because you chose Make it anonymous. Choose Leave them out to drop "
            "every note and event instead; the text choice then disappears, and the "
            'result says "All notes and events were left out." This tour keeps them.',
            do=[click("Keep them")],
            then=[click("Next")],
            focus=around(
                lambda ctx: ctx.window.joiner.privacy.notes_keep,
                lambda ctx: ctx.window.joiner.privacy.notes_leave_out,
            ),
        ),
        new_recording_step(
            "overnight-set",
            one_file=True,
            extra_caption="The name must not contain the patient's details or an original's name.",
        ),
        saved_step(
            "Everything is in one file. One progress bar goes through the stages: joining "
            "the chosen parts of the recordings into one and checking, packing the new "
            "recording into one file, and checking that the file holds the same recording. "
            "Nothing is saved in between."
        ),
        Step(
            "Open it in Explore",
            "Choose Open it in Explore to look at the result. Its History says it was "
            "made anonymous, and its patient details are gone.",
            do=[do(_open_result, "Open it in Explore", "choose Open it in Explore")],
            focus=lambda ctx: ctx.window.explorer.table,
        ),
    ]
    return Tour(
        key="t1",
        title="Make one anonymous file from several recordings",
        video_title="make-one-anonymous-file",
        summary=(
            "Take several recordings, keep only some of their channels, keep only one "
            "stretch of time, and join them into ONE anonymous recording saved as a "
            "single file. The originals are never changed."
        ),
        before=[
            "Open the recordings in Explore (they can be folders ending in .mefd or "
            "single files ending in .mefd.tar).",
            "Join or trim writes a new recording. Nothing is written until you choose "
            "Check and save.",
        ],
        steps=steps,
    )


TOUR = build()
