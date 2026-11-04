"""Steps that several tours share. Everything here talks to the real window
through what the user sees (button texts, pages in the list on the left)."""

from __future__ import annotations

from tourlib import HOUR, T0, Step, around, click, do, nav, pick_rows, type_in, wait


def utc_text(us: int) -> str:
    """A moment as the 'Date and time (UTC)' field wants it."""
    from mef3io_studio.model.timefmt import format_utc_us

    return format_utc_us(us)


def day(d: int, hour: int = 0) -> int:
    """A moment on the made-up calendar: day 6 = 6 January 2000."""
    return T0 + (d - 6) * 24 * HOUR + hour * HOUR


# -- Explore ------------------------------------------------------------------


def _open_recordings(ctx) -> None:
    page = ctx.window.explorer
    page.open_paths([str(p) for p in ctx.demo.paths.values()], interactive=False)
    ctx.wait_until(
        lambda: (
            len(page.entries()) == len(ctx.demo.paths) and all(not e.busy for e in page.entries())
        ),
        60,
        "the recordings to open",
    )


def open_demo_steps() -> list[Step]:
    return [
        Step(
            "Go to Explore",
            "Explore is the first page in the list on the left. It shows what is in "
            "your recordings.",
            do=[nav("Explore")],
            hold=2.5,
        ),
        Step(
            "Open your recordings",
            "Choose Open a recording to add one, or drag recordings from your file "
            "manager onto the window. Opening never changes a recording.",
            do=[do(_open_recordings, "Open a recording", "choose Open a recording")],
            focus=lambda ctx: ctx.window.explorer.table,
        ),
    ]


def select_and_join_steps(names: list[str]) -> list[Step]:
    return [
        Step(
            "Select the recordings",
            "Click the first recording, then hold Ctrl (Command on a Mac) and click "
            "each of the others. They stay selected.",
            do=[pick_rows(names)],
            then=[click("Join or trim these", "choose Join or trim these…")],
            focus=lambda ctx: ctx.window.explorer.table,
        ),
    ]


# -- Join or trim ---------------------------------------------------------------


def _channel_rect(name: str):
    def find(ctx):
        from PySide6.QtCore import QRect
        from tourlib import rect_in_window

        picker = ctx.window.joiner.picker
        picker.tree.expandAll()
        ctx.settle(0.1)
        model = picker.model
        for g in range(model.rowCount()):
            group = model.index(g, 0)
            for r in range(model.rowCount(group)):
                index = model.index(r, 0, group)
                if model.name_of(index) == name:
                    rect = picker.tree.visualRect(index)
                    return rect_in_window(
                        ctx.window,
                        picker.tree.viewport(),
                        QRect(rect.x(), rect.y(), 160, rect.height()),
                    )
        raise LookupError(f"No channel called {name!r} in the list.")

    return find


def leave_out(names: list[str]) -> list:
    acts = []
    for i, name in enumerate(names):

        def run(ctx, name=name):
            ctx.window.joiner.picker.click(name, False)

        acts.append(
            do(run, _channel_rect(name), "click the box of " + name if i == 0 else f"and {name}")
        )
    return acts


def channels_step(leave: list[str]) -> Step:
    if not leave:
        return Step(
            "Channels",
            "Every channel of every recording is kept unless you leave it out. Here "
            "we keep them all.",
            do=[do(lambda ctx: None, lambda ctx: ctx.window.joiner.picker.tree, "", click=False)],
            then=[click("Next")],
        )
    return Step(
        "Choose the channels",
        "Every channel is kept unless you leave it out. Click the box of "
        + " and ".join(leave)
        + " to leave them out; they will not be in the new recording.",
        do=[leave_out(leave)],
        then=[click("Next")],
        focus=lambda ctx: ctx.window.joiner.picker.tree,
    )


def new_recording_step(name: str, one_file: bool, extra_caption: str = "") -> Step:
    from mef3io_studio.joiner.page import SAVE_FILE, SAVE_FOLDER

    def set_folder(ctx) -> None:
        new = ctx.window.joiner.new
        new.folder = str(ctx.demo.out)
        new._update()

    acts = [
        type_in(lambda ctx: ctx.window.joiner.new.name, name, "type the name"),
        do(set_folder, "Choose the folder", "choose the folder where it is saved"),
        click(SAVE_FILE if one_file else SAVE_FOLDER, "choose how to save it"),
    ]
    how = (
        "One file (.mefd.tar) is easiest to move or email: the app opens it directly."
        if one_file
        else "A folder (.mefd) is a recording like the originals."
    )
    return Step(
        "Name the new recording",
        f"Type a name, choose the folder where it is saved, and how it is saved. {how} "
        f"{extra_caption}".strip(),
        do=[acts],
        then=[click("Check and save")],
        focus=around(
            lambda ctx: ctx.window.joiner.new.name,
            lambda ctx: ctx.window.joiner.new.folder_button,
            lambda ctx: ctx.window.joiner.new.save_folder,
            lambda ctx: ctx.window.joiner.new.save_file,
        ),
    )


def _finished(ctx) -> bool:
    page = ctx.window.joiner
    return page.result is not None and page.run_step.open_button.isVisibleTo(page)


def saved_step(extra: str = "") -> Step:
    return Step(
        "Wait until it is saved and checked",
        "The app first checks your choices (nothing is written yet), then makes the new "
        "recording and checks it. The originals are not changed. " + extra,
        do=[wait(_finished, "the new recording to be saved and checked")],
        focus=lambda ctx: ctx.window.joiner.run_step.result,
        hold=4.5,
    )
