"""Render the main window offscreen in the light and dark themes, with a few
synthetic background jobs in the activity panel, and the Explorer with a few
synthetic recordings (made-up names, no patient details shown), for docs and
PR review.

    QT_QPA_PLATFORM=offscreen python scripts/screenshots.py docs/img/screenshots
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mef3io_studio.app import build_main_window, create_application  # noqa: E402
from mef3io_studio.common.settings import Settings  # noqa: E402
from mef3io_studio.explorer.table import EntryRole  # noqa: E402


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    app = create_application()
    with tempfile.TemporaryDirectory() as tmp:
        window = build_main_window(app, Settings(Path(tmp) / "s.ini"))
        window.resize(1180, 760)
        window.show()

        def stepping(ctx, total, stop_at):
            for i in range(total):
                ctx.report(i, total, "Copying recording files")
                if i >= stop_at:
                    while not ctx.cancelled:
                        time.sleep(0.02)
                    ctx.check_cancelled()
                time.sleep(0.002)

        def unknown(ctx):
            ctx.report(0, None, "Scanning the folder")
            while not ctx.cancelled:
                time.sleep(0.02)
            ctx.check_cancelled()

        window.run_self_check()
        window.jobs.submit("Packing 3 recordings into one file", stepping, 100, 62)
        window.jobs.submit("Looking for recordings", unknown)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)

        for mode in ("light", "dark"):
            window.set_theme_mode(mode)
            for _ in range(10):
                app.processEvents()
                time.sleep(0.03)
            window.grab().save(str(out / f"main-window-{mode}.png"))
        window.jobs.shutdown()
        explorer_shots(app, out, Path(tmp))
    print("saved to", out)


def _write_demo(folder: Path) -> list[str]:
    import mef3io
    import numpy as np

    day = 86_400_000_000
    start = 946_684_800_000_000 + 9 * 3_600_000_000  # 2000-01-01 09:00 UTC, made up
    names = ["7f3c2a91", "0b44e8d2", "c9a1f077"]
    paths = []
    for i, name in enumerate(names):
        path = folder / f"{name}.mefd"
        t0 = start + (2 - i) * day
        with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
            for ch in range(6):
                w.write(f"LFP{ch + 1}", np.sin(np.arange(25_600) / 9.0), t0, 256.0)
                w.write(
                    f"LFP{ch + 1}",
                    np.sin(np.arange(25_600) / 9.0),
                    t0 + 160_000_000,
                    256.0,
                    new_segment=True,
                )
            w.write_annotations([{"time": t0 + 30_000_000, "text": "lights off"}])
        paths.append(str(path))
    return paths


def _write_more(folder: Path) -> tuple[list[str], str, str]:
    """For the combined timeline, a made-up day of monitoring: a recording
    in two parts, a copy of its first part, one where only some channels
    go on, one with a gap inside a part, and two password-protected ones
    (one opened with the basic password, so its times are unverified; one
    left locked). Low sampling rates keep the files small."""
    import mef3io
    import numpy as np

    hour = 3_600_000_000
    start = 946_684_800_000_000 + 5 * 86_400_000_000 + 7 * hour  # 2000-01-06 07:00 UTC
    fs = 2.0
    two_hours = np.sin(np.arange(int(2 * 3600 * fs)) / 9.0)
    paths = []

    def write(name, plan, channels=6, **kw):
        path = folder / f"{name}.mefd"
        with mef3io.Writer(str(path), overwrite=True, **kw) as w:
            for ch in range(channels):
                for at, new_part, only in plan:
                    if only is None or ch < only:
                        w.write(
                            f"LFP{ch + 1}",
                            two_hours,
                            start + int(at * hour),
                            fs,
                            new_segment=new_part,
                        )
        return str(path)

    paths.append(write("4a7d0c12", [(0, False, None), (2.5, True, None)]))
    paths.append(write("a83e5d10", [(0, False, None)]))  # the same time: a copy
    paths.append(write("5d2b9c44", [(5, False, None), (7, True, 3)]))
    paths.append(write("2e6a71b8", [(10, False, None), (12.5, False, None)], channels=4))
    basic = write(
        "e01f7b3a", [(15, False, None)], channels=4, password1="demo-basic", password2="demo-full"
    )
    paths.append(
        write(
            "93cc0a6e",
            [(0, False, None)],
            channels=1,
            password1="demo-basic",
            password2="demo-full",
        )
    )
    return paths, basic, "demo-basic"


def _settle(app, seconds: float = 0.4) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def explorer_shots(app, out: Path, tmp: Path) -> None:
    window = build_main_window(app, Settings(tmp / "e.ini"))
    window.resize(1180, 820)
    window.show()
    window.activity_dock.hide()
    window.set_time_zone("Europe/Prague")
    page = window.explorer
    page.open_paths(_write_demo(tmp), interactive=False)

    def wait_for(n):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (
            len(page.entries()) == n
            and all(e.outcome is not None and not e.busy for e in page.entries())
        ):
            app.processEvents()
            time.sleep(0.02)

    wait_for(3)
    window.activity_dock.hide()
    page.select_entry(page.entries()[0])
    for mode in ("light", "dark"):
        window.set_theme_mode(mode)
        for tab, name in ((0, "overview"), (2, "timeline")):
            page.tabs.setCurrentIndex(tab)
            _settle(app)
            window.grab().save(str(out / f"explorer-{name}-{mode}.png"))
    # The combined timeline, with a few more recordings.
    more, basic, password = _write_more(tmp)
    page.open_paths(more, interactive=False)
    page.open_paths([basic], interactive=False, password=password)
    wait_for(9)
    window.activity_dock.hide()
    # Select the new ones: the timeline shows the selection.
    page.table.clearSelection()
    chosen = {Path(p).name for p in [*more, basic]}
    model = page.table.selectionModel()
    for row in range(page.proxy.rowCount()):
        index = page.proxy.index(row, 0)
        entry = page.proxy.data(index, EntryRole)
        if Path(entry.location).name in chosen:
            model.select(index, model.SelectionFlag.Select | model.SelectionFlag.Rows)
    page.show_combined(True)
    _settle(app, 1.0)
    for mode in ("light", "dark"):
        window.set_theme_mode(mode)
        _settle(app)
        window.grab().save(str(out / f"explorer-combined-{mode}.png"))
    page.show_combined(False)
    window.jobs.shutdown()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "screenshots")
