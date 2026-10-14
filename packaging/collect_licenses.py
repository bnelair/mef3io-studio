"""Gather the licence texts of everything a packaged build ships into one
THIRD_PARTY_LICENSES folder, and FAIL the build if any bundled native
library cannot be matched to a licence. Called from mef3io-studio.spec after
PyInstaller has worked out which files go into the bundle.

Layout of the folder:
* README.txt          plain overview;
* MANIFEST.tsv        every bundled native library -> component -> licence
                      files (relative paths); check_bundle.py verifies each
                      listed licence file exists and is not empty;
* one folder per component with its licence texts.

How a library is matched, in order:
1. curated rules for files that come from Python wheels or the Python
   runtime (Qt/PySide6, ICU, numpy + OpenBLAS, mef3io, Python, OpenSSL,
   libffi, the Microsoft C runtime);
2. Linux system libraries: the rpm (or dpkg) package that owns the file,
   with fall-backs for licence files packaged elsewhere: other packages
   built from the same source rpm (e.g. libX11-common), then
   /usr/share/licenses/<name>*;
3. otherwise the build fails, naming the library.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import sysconfig
from functools import cache
from importlib import metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
LIC = HERE / "licenses"

# Python distributions whose files end up in the bundle.
DISTRIBUTIONS = [
    "mef3io",
    "numpy",
    "platformdirs",
    "PySide6-Essentials",
    "shiboken6",
    "pyinstaller",
]


class MissingLicence(RuntimeError):
    pass


def _copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)


def _python_licence() -> Path | None:
    v = f"{sys.version_info[0]}.{sys.version_info[1]}"
    candidates = [
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt",
        Path(sys.base_prefix) / "lib" / f"python{v}" / "LICENSE.txt",
        Path(f"/usr/share/licenses/python{v}/LICENSE"),
        Path(f"/usr/share/licenses/python{v}-libs/LICENSE"),
    ]
    return next((c for c in candidates if c.is_file()), None)


def _dist_licences(name: str) -> list[tuple[str, Path]]:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return []
    out = []
    for f in dist.files or []:
        text = str(f)
        if ".dist-info/" not in text:
            continue
        rel = text.split(".dist-info/", 1)[1]
        low = rel.lower()
        if low.startswith(("licenses/", "license_files/")) or any(
            k in Path(low).name for k in ("license", "licence", "copying", "notice")
        ):
            path = Path(dist.locate_file(f))
            if path.is_file():
                out.append((rel, path))
    return out


# --------------------------------------------------------------------------
# Linux system libraries
# --------------------------------------------------------------------------


def _run(*cmd: str) -> str:
    r = subprocess.run(list(cmd), capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


@cache
def _rpm_by_source() -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for line in _run("rpm", "-qa", "--qf", "%{NAME} %{SOURCERPM}\\n").splitlines():
        parts = line.split()
        if len(parts) == 2:
            groups.setdefault(parts[1], []).append(parts[0])
    return groups


_LICENCE_NAME = re.compile(r"copying|licen[cs]e|notice|copyright", re.IGNORECASE)


def _licence_files_of(pkg: str) -> list[Path]:
    """%license files, else licence-like %doc files (older packages such as
    libX11-common ship COPYING as documentation)."""
    files = [Path(p) for p in _run("rpm", "-qL", pkg).split() if Path(p).is_file()]
    if not files:
        files = [
            Path(p)
            for p in _run("rpm", "-qd", pkg).split()
            if _LICENCE_NAME.search(Path(p).name) and Path(p).is_file()
        ]
    if not files:
        folder = Path(f"/usr/share/licenses/{pkg}")
        files = sorted(p for p in folder.glob("*") if p.is_file()) if folder.is_dir() else []
    return files


def _system_owner(lib: str) -> tuple[str, list[Path]]:
    if shutil.which("rpm"):
        out = _run("rpm", "-qf", "--qf", "%{NAME} %{SOURCERPM}\\n", lib).split()
        if len(out) >= 2:
            pkg, srpm = out[0], out[1]
            files = _licence_files_of(pkg)
            if not files:  # e.g. libX11's licence lives in libX11-common
                for sibling in sorted(_rpm_by_source().get(srpm, [])):
                    files = _licence_files_of(sibling)
                    if files:
                        break
            if not files:
                base = re.sub(r"-\d.*$", "", srpm)
                for folder in sorted(Path("/usr/share/licenses").glob(f"{base}*")):
                    files = sorted(p for p in folder.glob("*") if p.is_file())
                    if files:
                        break
            if not files:  # curated texts for packages that ship none
                curated = LIC / "linux" / pkg
                files = sorted(p for p in curated.glob("*") if p.is_file())
            return pkg, files
    if shutil.which("dpkg"):
        out = _run("dpkg", "-S", lib)
        if out:
            pkg = out.split(":")[0]
            copyright_file = Path(f"/usr/share/doc/{pkg}/copyright")
            return pkg, [copyright_file] if copyright_file.is_file() else []
    return "", []


# --------------------------------------------------------------------------
# Curated components
# --------------------------------------------------------------------------

QT = "Qt-and-PySide6"
COMPONENT_RULES: list[tuple[str, str]] = [
    # Most specific first: the C runtime and OpenSSL/libffi also sit inside
    # the PySide6 / numpy / Python folders, and must not be attributed to them.
    (r"vcruntime|msvcp|ucrtbase|api-ms-win|concrt", "Microsoft-VC-Runtime"),
    (r"icu(uc|in|dt|i18n|data)|libicu", "ICU"),
    (r"libcrypto|libssl", "OpenSSL"),
    (r"libffi", "libffi"),
    (r"(^|/)(PySide6|shiboken6)/|Qt6|pyside6|shiboken6|(^|/)q\w+\.(dll|dylib)$|libqt", QT),
    (r"numpy|openblas|gfortran|quadmath|libgcc_s\.1\.1\.dylib", "numpy"),
    (r"(^|/)mef3io/", "mef3io"),
    (r"python3|libpython|lib-dynload/|\.cpython-|(^|/)_\w+\.pyd$|(^|/)\w+\.pyd$", "Python"),
]


def _component(dest: str) -> str | None:
    for pattern, name in COMPONENT_RULES:
        if re.search(pattern, dest, re.IGNORECASE):
            return name
    return None


def collect(dest: Path, binaries: list[tuple[str, str, str]]) -> Path:
    """Write the licence folder to ``dest`` and return it. Raises
    MissingLicence if a bundled library has no licence."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    # Fixed components -------------------------------------------------------
    _copy(ROOT / "LICENSE", dest / "mef3io-studio" / "LICENSE.txt")
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt", "Qt-and-PySide6-NOTICE.txt"):
        _copy(LIC / name, dest / QT / name)
    # Legacy top-level copies: the About text and check_bundle refer to them.
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt", "Qt-and-PySide6-NOTICE.txt"):
        _copy(LIC / name, dest / name)
    shutil.copytree(LIC / "qt-third-party", dest / QT / "third-party")
    shutil.copytree(LIC / "qt-third-party" / "ICU", dest / "ICU")
    _copy(LIC / "extra" / "OpenSSL-LICENSE.txt", dest / "OpenSSL" / "LICENSE.txt")
    _copy(LIC / "extra" / "OpenSSL-NOTICE.txt", dest / "OpenSSL" / "NOTICE.txt")
    _copy(LIC / "extra" / "libffi-LICENSE.txt", dest / "libffi" / "LICENSE.txt")
    _copy(
        LIC / "extra" / "Microsoft-VC-Runtime-NOTICE.txt",
        dest / "Microsoft-VC-Runtime" / "NOTICE.txt",
    )
    python_licence = _python_licence()
    if python_licence is None:
        raise MissingLicence("the Python (PSF) licence text was not found on this machine")
    _copy(python_licence, dest / "Python" / "LICENSE.txt")
    if sys.platform == "win32":
        _copy(LIC / "InnoSetup-license.txt", dest / "Inno-Setup" / "LICENSE.txt")

    lines = [
        "Third-party components bundled with mef3io-studio",
        "=================================================",
        "",
        "mef3io-studio itself: Apache License 2.0 (mef3io-studio/).",
        "Qt 6 / PySide6 / shiboken6: LGPL-3.0 (Qt-and-PySide6/ — includes how to",
        "  replace the Qt libraries, where to get the source, and the licences of",
        "  the third-party code inside Qt in Qt-and-PySide6/third-party/).",
        "ICU (Unicode support used by Qt): ICU licence (ICU/).",
        f"Python {sys.version.split()[0]}: PSF License (Python/).",
        "",
        "Python packages (licence files in the folder named after each):",
    ]
    for name in DISTRIBUTIONS:
        try:
            version = metadata.version(name)
            meta = metadata.metadata(name)
        except metadata.PackageNotFoundError:
            continue
        licence = meta.get("License-Expression") or meta.get("License") or "see folder"
        for rel, path in _dist_licences(name):
            _copy(path, dest / name / rel)
        lines.append(f"  {name} {version}: {licence.splitlines()[0][:80]}")
    if sys.platform == "win32":
        lines.append("Installer and uninstaller: Inno Setup (Inno-Setup/).")

    # Every native library -> a licence -------------------------------------
    manifest = ["# bundled file\tcomponent\tlicence files (relative to this folder)"]
    missing = []
    for dest_name, src, kind in sorted(binaries):
        if kind not in ("BINARY", "EXTENSION"):
            continue
        rel_dest = dest_name.replace("\\", "/")
        component = _component(rel_dest)
        if component is None and "site-packages" not in src and sys.platform.startswith("linux"):
            pkg, docs = _system_owner(str(Path(src).resolve()))
            if pkg and docs:
                component = f"system-libraries/{pkg}"
                for doc in docs:
                    _copy(doc, dest / component / doc.name)
        if component is None and rel_dest.startswith("mef3io_studio"):
            component = "mef3io-studio"
        if component is None:
            missing.append(f"{rel_dest} (from {src})")
            continue
        files = sorted(
            p.relative_to(dest).as_posix() for p in (dest / component).rglob("*") if p.is_file()
        )
        manifest.append(f"{rel_dest}\t{component}\t{';'.join(files[:20])}")
    if missing:
        raise MissingLicence(
            "no licence found for these bundled libraries (add a rule to "
            "packaging/collect_licenses.py, or keep them out of the bundle):\n  "
            + "\n  ".join(missing)
        )
    lines += ["", "Every bundled native library and its licence: MANIFEST.tsv."]
    (dest / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (dest / "MANIFEST.tsv").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    return dest
