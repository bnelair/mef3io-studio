"""The bundle checks that run in CI (packaging/check_bundle.py) and the
licence classification (packaging/collect_licenses.py)."""

import sys
from pathlib import Path

import pytest

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"
sys.path.insert(0, str(PACKAGING))

import check_bundle  # noqa: E402
import collect_licenses  # noqa: E402


def _bundle(tmp_path, manifest_rows, licence_files):
    root = tmp_path / "bundle"
    lic = root / "_internal" / "THIRD_PARTY_LICENSES"
    lic.mkdir(parents=True)
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt", "Qt-and-PySide6-NOTICE.txt", "README.txt"):
        (lic / name).write_text("text")
    for rel, text in licence_files.items():
        (lic / rel).parent.mkdir(parents=True, exist_ok=True)
        (lic / rel).write_text(text)
    (lic / "MANIFEST.tsv").write_text(
        "# header\n" + "\n".join("\t".join(r) for r in manifest_rows) + "\n"
    )
    return root, lic


def test_complete_manifest_passes(tmp_path):
    root, lic = _bundle(tmp_path, [("libfoo.so.1", "foo", "foo/COPYING")], {"foo/COPYING": "MIT"})
    (root / "_internal" / "libfoo.so.1").write_text("elf")
    assert check_bundle.check_licences(lic, root) == []


def test_library_missing_from_manifest_fails(tmp_path):
    root, lic = _bundle(tmp_path, [], {})
    (root / "_internal" / "libbar.so.2").write_text("elf")
    assert any("not in MANIFEST" in p for p in check_bundle.check_licences(lic, root))


def test_empty_licence_fails(tmp_path):
    root, lic = _bundle(tmp_path, [("libfoo.so.1", "foo", "foo/COPYING")], {"foo/COPYING": ""})
    (root / "_internal" / "libfoo.so.1").write_text("elf")
    assert any("missing or empty" in p for p in check_bundle.check_licences(lic, root))


@pytest.mark.parametrize(
    "dest,component",
    [
        ("PySide6/Qt/lib/libQt6Core.so.6", "Qt-and-PySide6"),
        ("PySide6/Qt/lib/libicui18n.so.73", "ICU"),
        ("PySide6/icuuc.dll", "ICU"),
        ("PySide6/plugins/platforms/qwindows.dll", "Qt-and-PySide6"),
        ("numpy.libs/libscipy_openblas64_-x.so", "numpy"),
        ("mef3io/_mef3io.cpython-312-x86_64-linux-gnu.so", "mef3io"),
        ("libcrypto-3.dll", "OpenSSL"),
        ("libffi-8.dll", "libffi"),
        ("VCRUNTIME140.dll", "Microsoft-VC-Runtime"),
        ("api-ms-win-crt-heap-l1-1-0.dll", "Microsoft-VC-Runtime"),
        ("PySide6/MSVCP140.dll", "Microsoft-VC-Runtime"),
        ("numpy.libs/msvcp140-a4c2229b.dll", "Microsoft-VC-Runtime"),
        ("python312.dll", "Python"),
        ("_ctypes.pyd", "Python"),
        ("python3.12/lib-dynload/_bz2.cpython-312-x86_64-linux-gnu.so", "Python"),
    ],
)
def test_bundled_files_map_to_a_licence(dest, component):
    assert collect_licenses._component(dest) == component


def test_unknown_library_is_not_silently_accepted():
    assert collect_licenses._component("libmystery.dll") is None
