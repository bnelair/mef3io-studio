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


def explorer_shots(app, out: Path, tmp: Path) -> None:
    window = build_main_window(app, Settings(tmp / "e.ini"))
    window.resize(1180, 820)
    window.show()
    window.activity_dock.hide()
    window.set_time_zone("Europe/Prague")
    page = window.explorer
    page.open_paths(_write_demo(tmp), interactive=False)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not (
        len(page.entries()) == 3 and all(e.outcome is not None for e in page.entries())
    ):
        app.processEvents()
        time.sleep(0.02)
    window.activity_dock.hide()
    page.select_entry(page.entries()[0])
    for mode in ("light", "dark"):
        window.set_theme_mode(mode)
        for tab, name in ((0, "overview"), (2, "timeline")):
            page.tabs.setCurrentIndex(tab)
            for _ in range(10):
                app.processEvents()
                time.sleep(0.03)
            window.grab().save(str(out / f"explorer-{name}-{mode}.png"))
    window.jobs.shutdown()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "screenshots")
