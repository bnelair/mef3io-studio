"""Render the main window offscreen in the light and dark themes, with a few
synthetic background jobs in the activity panel, for docs and PR review.

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
    print("saved to", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "screenshots")
