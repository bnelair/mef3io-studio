"""Fail if a built bundle contains something that must not ship, or lacks the
licence folder. Run in CI after PyInstaller:

    python packaging/check_bundle.py dist/mef3io-studio        # or the .app
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

FORBIDDEN = [
    (r"network", "Qt networking (the app makes no network connections)"),
    (r"qvnc", "the VNC display plugin (a network server)"),
    (r"(^|[/\\])(lib)?q(eglfs|linuxfb|minimal|minimalegl|vkkhrdisplay)\.", "kiosk display plugins"),
    (r"qtuiotouch", "the TUIO touch plugin (listens on a UDP port)"),
    (r"qgtk3", "the GTK3 theme plugin (pulls in the GTK stack)"),
    (r"(^|[/\\])lib(gtk|gdk)-3", "the GTK stack"),
    (r"readline", "GNU readline (GPLv3)"),
    (r"(^|[/\\])_?curses", "curses"),
    (r"(^|[/\\])_ssl\.", "TLS support (no network code path exists)"),
]


def main(bundle: str) -> int:
    root = Path(bundle)
    if not root.exists():
        print(f"no bundle at {root}")
        return 1
    problems = []
    files = [p for p in root.rglob("*")]
    for path in files:
        rel = path.relative_to(root).as_posix()
        for pattern, why in FORBIDDEN:
            if re.search(pattern, rel, re.IGNORECASE):
                problems.append(f"{rel}: {why}")
        if sys.platform.startswith("linux") and re.search(r"(^|/)lib(stdc\+\+|gcc_s)\.so", rel):
            problems.append(f"{rel}: libstdc++/libgcc must come from the user's system")
    licences = [p for p in files if p.name == "THIRD_PARTY_LICENSES" and p.is_dir()]
    if not licences:
        problems.append("THIRD_PARTY_LICENSES folder missing")
    else:
        names = {p.name for p in licences[0].iterdir()}
        for required in ("LGPL-3.0.txt", "GPL-3.0.txt", "Qt-and-PySide6-NOTICE.txt", "README.txt"):
            if required not in names:
                problems.append(f"THIRD_PARTY_LICENSES/{required} missing")
    for p in problems:
        print("NOT ALLOWED IN BUNDLE:", p)
    print(f"checked {len(files)} files: {'OK' if not problems else 'FAILED'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
