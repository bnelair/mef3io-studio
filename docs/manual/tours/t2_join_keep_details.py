"""T2: join recordings as they are, keeping the original details, as a folder."""

from _common import (
    channels_step,
    new_recording_step,
    open_demo_steps,
    saved_step,
    select_and_join_steps,
)
from tourlib import NIGHTS, Step, Tour, click


def build() -> Tour:
    steps = [
        *open_demo_steps(),
        *select_and_join_steps(list(NIGHTS)),
        channels_step([]),
        Step(
            "Keep every recording whole",
            "The recordings are listed in time order. Choose for each recording what "
            "to keep. We keep the whole of each one, so the nights simply follow each "
            "other. The map shows the gap that was in the second night.",
            do=[click("Choose for each recording")],
            then=[click("Next")],
            focus=lambda ctx: ctx.window.joiner.keep.table,
        ),
        Step(
            "Keep the patient details",
            "Keep the patient details (as in the originals) is the choice that is "
            "already made. The new recording says who the patient is, like the "
            "originals do, so keep it where such details are allowed.",
            do=[click("Keep the patient details")],
            then=[click("Next")],
            focus=lambda ctx: ctx.window.joiner.privacy,
        ),
        new_recording_step("nights-joined", one_file=False),
        saved_step("The new recording is a folder next to the others you saved."),
    ]
    return Tour(
        key="t2",
        title="Keep the original details: join without anonymizing, as a folder",
        video_title="join-keep-details-as-folder",
        summary=(
            "Join recordings into one new recording that keeps the patient details and "
            "the real dates, saved as a folder like the originals."
        ),
        before=[
            "Joining never moves anything in time. Two parts that run at the same time "
            "may be joined only if they share no channel you keep."
        ],
        steps=steps,
    )


TOUR = build()
