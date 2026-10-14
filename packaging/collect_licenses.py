"""Gather the licence texts of everything a packaged build ships into one
THIRD_PARTY_LICENSES folder. Called from mef3io-studio.spec after PyInstaller
has worked out which files go into the bundle.

What goes in:
* the app's own licence (Apache-2.0);
* LGPL-3.0 / GPL-3.0 texts and the Qt/PySide6 notice (how to replace Qt,
  where the source is);
* the licence files of every bundled Python distribution that ships them
  (mef3io, numpy incl. OpenBLAS/libgfortran, platformdirs, PyInstaller's
  bootloader exception, ...);
* the Python (PSF) licence;
* Linux: for each system library copied into the bundle, its package's
  licence files (rpm or dpkg), with a manifest saying which is which;
* Windows: the Inno Setup licence (its uninstaller ships with the app).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# Python distributions whose files end up in the bundle.
DISTRIBUTIONS = [
    "mef3io",
    "numpy",
    "platformdirs",
    "PySide6-Essentials",
    "shiboken6",
    "pyinstaller",
]


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


def _python_licence() -> Path | None:
    candidates = [
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt",
        Path(sys.base_prefix)
        / "lib"
        / f"python{sys.version_info[0]}.{sys.version_info[1]}"
        / "LICENSE.txt",
        Path(sys.base_prefix)
        / "share"
        / "doc"
        / f"python{sys.version_info[0]}.{sys.version_info[1]}"
        / "LICENSE",
        Path("/usr/share/licenses/python3.12/LICENSE"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _dist_licences(name: str) -> list[tuple[str, Path]]:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return []
    out = []
    for f in dist.files or []:
        parts = [p.lower() for p in Path(str(f)).parts]
        if any(p in ("licenses", "license_files") for p in parts) or any(
            k in parts[-1] for k in ("license", "licence", "copying", "notice")
        ):
            path = Path(dist.locate_file(f))
            if path.is_file() and ".dist-info" in str(f):
                out.append((str(f).split(".dist-info/", 1)[-1], path))
    return out


def _owner_licences(lib: str) -> tuple[str, list[Path]]:
    """Package owning a system library and its licence files (Linux)."""
    if shutil.which("rpm"):
        r = subprocess.run(
            ["rpm", "-qf", "--qf", "%{NAME}\\n", lib], capture_output=True, text=True
        )
        if r.returncode == 0 and r.stdout.strip():
            pkg = r.stdout.split()[0]
            files = subprocess.run(
                ["rpm", "-qL", pkg], capture_output=True, text=True
            ).stdout.split()
            docs = [Path(p) for p in files if Path(p).is_file()]
            if not docs:
                docs = sorted(Path(f"/usr/share/licenses/{pkg}").glob("*"))
            return pkg, docs
    if shutil.which("dpkg"):
        r = subprocess.run(["dpkg", "-S", lib], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            pkg = r.stdout.split(":")[0]
            copyright_file = Path(f"/usr/share/doc/{pkg}/copyright")
            return pkg, [copyright_file] if copyright_file.is_file() else []
    return "", []


def collect(dest: Path, binaries: list[tuple[str, str, str]]) -> Path:
    """Write the licence folder to ``dest`` and return it."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    lines = [
        "Third-party components bundled with mef3io-studio",
        "=================================================",
        "",
        "mef3io-studio itself: Apache License 2.0 (mef3io-studio-LICENSE.txt).",
        "Qt 6 / PySide6 / shiboken6: LGPL-3.0 (see Qt-and-PySide6-NOTICE.txt,",
        "  LGPL-3.0.txt and GPL-3.0.txt). You may replace the Qt libraries.",
        "",
        "Python packages (licence files in the folder named after each):",
    ]
    _copy(ROOT / "LICENSE", dest / "mef3io-studio-LICENSE.txt")
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt", "Qt-and-PySide6-NOTICE.txt"):
        _copy(HERE / "licenses" / name, dest / name)
    for name in DISTRIBUTIONS:
        try:
            version = metadata.version(name)
            licence = metadata.metadata(name).get("License-Expression") or metadata.metadata(
                name
            ).get("License", "")
        except metadata.PackageNotFoundError:
            continue
        files = _dist_licences(name)
        for rel, path in files:
            _copy(path, dest / name / rel)
        lines.append(f"  {name} {version}: {(licence or 'see folder').splitlines()[0][:80]}")
    python_licence = _python_licence()
    lines += ["", f"Python {sys.version.split()[0]}: PSF License (Python-LICENSE.txt)."]
    if python_licence:
        _copy(python_licence, dest / "Python-LICENSE.txt")
    else:
        lines.append("  (licence text: https://docs.python.org/3/license.html)")

    if sys.platform == "win32":
        _copy(HERE / "licenses" / "InnoSetup-license.txt", dest / "InnoSetup-license.txt")
        lines += ["", "Installer and uninstaller: Inno Setup (InnoSetup-license.txt)."]

    if sys.platform.startswith("linux"):
        lines += ["", "System libraries copied into the bundle (library -> package):"]
        seen: dict[str, str] = {}
        for dest_name, src, kind in binaries:
            if kind != "BINARY" or "/" in dest_name.replace("\\", "/"):
                continue  # only top-level libraries come from the system
            if "site-packages" in src:
                continue
            pkg, docs = _owner_licences(os.path.realpath(src))
            if not pkg:
                lines.append(f"  {dest_name} -> (owner unknown: {src})")
                continue
            lines.append(f"  {dest_name} -> {pkg}")
            if pkg in seen:
                continue
            seen[pkg] = dest_name
            for doc in docs:
                _copy(doc, dest / "system-libraries" / pkg / doc.name)
    (dest / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest
