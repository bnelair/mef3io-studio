"""Fetch Qt's third-party attributions for the pinned Qt version into
packaging/licenses/qt-third-party/ (committed; the build copies it into
THIRD_PARTY_LICENSES). Run by a developer when the PySide6 pin changes:

    python scripts/fetch_qt_attributions.py 6.12.0

Qt bundles third-party code (libjpeg, libpng, pcre2, md4c, harfbuzz,
double-conversion, ...). Its licence notices are listed in the
qt_attribution.json files of the Qt sources, which the PySide6 wheels do not
ship. The ICU libraries the wheels bundle come with the ICU licence, fetched
here too. This script needs the network; the app never does.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "packaging" / "licenses" / "qt-third-party"
# The Qt modules whose code is in PySide6-Essentials' libraries we ship.
REPOS = ["qtbase", "qtsvg", "qtimageformats", "qtwayland"]
ICU = ("ICU 73", "https://raw.githubusercontent.com/unicode-org/icu/release-73-2/icu4c/LICENSE")


def tree(repo: str, tag: str) -> list[str]:
    out = subprocess.run(
        ["gh", "api", f"repos/qt/{repo}/git/trees/{tag}?recursive=1", "-q", ".tree[].path"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.split()


def raw(repo: str, tag: str, path: str) -> bytes:
    url = f"https://raw.githubusercontent.com/qt/{repo}/{tag}/{path}"
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - dev script
        return response.read()


def main(version: str) -> int:
    tag = f"v{version}"
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    index = [f"Third-party code inside Qt {version} (from the qt_attribution.json files)", ""]
    for repo in REPOS:
        paths = tree(repo, tag)
        for jpath in sorted(p for p in paths if p.endswith("qt_attribution.json")):
            entries = json.loads(raw(repo, tag, jpath), strict=False)
            entries = entries if isinstance(entries, list) else [entries]
            base = jpath.rsplit("/", 1)[0]
            for entry in entries:
                if "sbom-test" in (entry.get("Id") or ""):
                    continue  # Qt's own build-system test fixtures
                ident = entry.get("Id") or entry.get("Name", "unknown").replace(" ", "-")
                files = entry.get("LicenseFiles") or (
                    [entry["LicenseFile"]] if entry.get("LicenseFile") else []
                )
                target = OUT / f"{repo}-{ident}"
                target.mkdir(exist_ok=True)
                saved = []
                for rel in files:
                    src = f"{base}/{rel}".replace("/./", "/")
                    parts = []
                    for part in src.split("/"):
                        if part == "..":
                            parts.pop()
                        else:
                            parts.append(part)
                    src = "/".join(parts)
                    try:
                        data = raw(repo, tag, src)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  missing {repo}:{src}: {exc}")
                        continue
                    name = Path(src).name
                    (target / name).write_bytes(data)
                    saved.append(name)
                if not saved:
                    # Public-domain components (md5, sha1, sqlite, ...) carry
                    # their terms in the notice itself.
                    saved.append("NOTICE.txt")
                notice = [
                    f"Name: {entry.get('Name', ident)}",
                    f"Qt module: {repo} ({entry.get('QtUsage', '').strip()[:300]})",
                    f"License: {entry.get('License', '')} ({entry.get('LicenseId', '')})",
                    f"Copyright: {entry.get('Copyright', '')}",
                    f"Version: {entry.get('Version', '')}",
                    f"Homepage: {entry.get('Homepage', '')}",
                ]
                (target / "NOTICE.txt").write_text("\n".join(notice) + "\n", encoding="utf-8")
                index.append(
                    f"{repo}-{ident}: {entry.get('Name', ident)} — {entry.get('LicenseId', '')}"
                )
                print(index[-1], saved)
    icu_dir = OUT / "ICU"
    icu_dir.mkdir()
    with urllib.request.urlopen(ICU[1], timeout=60) as response:  # noqa: S310
        (icu_dir / "LICENSE").write_bytes(response.read())
    index += ["", "ICU: the Unicode/ICU licence (libicu*.so / icu*.dll bundled with PySide6)"]
    (OUT / "INDEX.txt").write_text("\n".join(index) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "6.12.0"))
