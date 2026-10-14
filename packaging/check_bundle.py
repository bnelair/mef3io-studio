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
    (r"(^|[/\\])(setuptools|pkg_resources|_distutils_hack)([/\\]|$)", "build tools"),
    (r"opengl32sw", "software OpenGL (not needed)"),
]


def check_licences(folder: Path, root: Path) -> list[str]:
    """Every native library in the bundle is listed in MANIFEST.tsv, and every
    licence file the manifest names exists and is not empty."""
    problems = []
    for required in (
        "LGPL-3.0.txt",
        "GPL-3.0.txt",
        "Qt-and-PySide6-NOTICE.txt",
        "README.txt",
        "MANIFEST.tsv",
    ):
        if not (folder / required).is_file():
            problems.append(f"THIRD_PARTY_LICENSES/{required} missing")
    manifest = folder / "MANIFEST.tsv"
    if not manifest.is_file():
        return problems
    listed = set()
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, component, licence_files = (line.split("\t") + ["", ""])[:3]
        listed.add(Path(name).name.lower())
        paths = [p for p in licence_files.split(";") if p]
        if not paths:
            problems.append(f"{name}: no licence files listed for {component}")
        for rel in paths:
            path = folder / rel
            if not path.is_file() or path.stat().st_size == 0:
                problems.append(f"{name}: licence file {rel} missing or empty")
    native = re.compile(r"\.(so(\.\d+)*|dll|dylib|pyd)$", re.IGNORECASE)
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and native.search(path.name):
            if path.name.lower() not in listed:
                problems.append(f"{path.relative_to(root).as_posix()}: not in MANIFEST.tsv")
    return problems


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
        problems += check_licences(licences[0], root)
    for p in problems:
        print("NOT ALLOWED IN BUNDLE:", p)
    print(f"checked {len(files)} files: {'OK' if not problems else 'FAILED'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
