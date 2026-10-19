"""Compute and write the next version (used by .github/workflows/bump-version.yml).

The repo-root ``VERSION`` file is the single source of truth for the app's
version; everything else (the app, pyproject, the installers, the release tag)
reads it. This script is the only thing that changes it in a release:

    python scripts/bump_version.py dev        # 0.1.0.dev3 -> 0.1.0.dev4;  0.1.0 -> 0.1.1.dev1
    python scripts/bump_version.py release    # 0.1.0.dev4 -> 0.1.0  (the finished version)
    python scripts/bump_version.py patch      # 0.1.0 -> 0.1.1   (from a finished version only)
    python scripts/bump_version.py minor      # 0.1.0 -> 0.2.0
    python scripts/bump_version.py major      # 0.1.0 -> 1.0.0
    python scripts/bump_version.py 0.2.0.dev1 # an explicit version, newer than the current one

It writes ``VERSION`` and, when missing, a release-notes stub
``docs/release-notes/<version>.md`` that the release refuses to publish until
it has been written (``STUB_MARKER``). It prints the new version.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUB_MARKER = "<!-- RELEASE NOTES NOT WRITTEN YET -->"

# The subset of PEP 440 this project uses: X.Y.Z, X.Y.Z.devN, X.Y.Z{a,b,rc}N.
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+)|(a|b|rc)(\d+))?$")
_STAGE_RANK = {"dev": 0, "a": 1, "b": 2, "rc": 3, "final": 4}


def parse(version: str) -> tuple[int, int, int, str, int]:
    m = VERSION_RE.match(version)
    if not m:
        raise ValueError(
            f"{version!r} is not a version this project uses "
            "(X.Y.Z, X.Y.Z.devN, X.Y.ZaN, X.Y.ZbN or X.Y.ZrcN)"
        )
    major, minor, patch = int(m[1]), int(m[2]), int(m[3])
    if m[4] is not None:
        return major, minor, patch, "dev", int(m[4])
    if m[5] is not None:
        return major, minor, patch, m[5], int(m[6])
    return major, minor, patch, "final", 0


def sort_key(version: str) -> tuple[int, int, int, int, int]:
    major, minor, patch, stage, n = parse(version)
    return major, minor, patch, _STAGE_RANK[stage], n


def next_version(current: str, bump: str) -> str:
    major, minor, patch, stage, n = parse(current)
    base = f"{major}.{minor}.{patch}"
    if bump == "dev":
        if stage == "dev":
            return f"{base}.dev{n + 1}"
        if stage == "final":
            return f"{major}.{minor}.{patch + 1}.dev1"
        raise ValueError(f"{current} is a release candidate; use 'release' or an explicit version")
    if bump == "release":
        if stage == "final":
            raise ValueError(f"{current} is already a finished version")
        return base
    if bump in ("patch", "minor", "major"):
        if stage != "final":
            raise ValueError(
                f"{current} is a test version: use 'release' to publish {base}, "
                "or give an explicit version"
            )
        if bump == "patch":
            return f"{major}.{minor}.{patch + 1}"
        if bump == "minor":
            return f"{major}.{minor + 1}.0"
        return f"{major + 1}.0.0"
    parse(bump)  # an explicit version: must be valid ...
    if sort_key(bump) <= sort_key(current):
        raise ValueError(f"{bump} is not newer than the current version {current}")
    return bump  # ... and newer


def notes_stub(version: str) -> str:
    return (
        f"{STUB_MARKER}\n"
        f"Write the release notes for {version} here before merging: plain words "
        "for testers and users (what is new, what to try, known limitations, how to "
        "report a problem without sharing patient data). The release is refused "
        "while this marker is in the file.\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    current = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    try:
        new = next_version(current, argv[0])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    (ROOT / "VERSION").write_text(new + "\n", encoding="utf-8")
    notes = ROOT / "docs" / "release-notes" / f"{new}.md"
    if not notes.exists():
        notes.write_text(notes_stub(new), encoding="utf-8")
    print(new)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
