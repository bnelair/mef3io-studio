"""The --self-test entry points, start-up hardening, the version source, and
the project rules (mef3io public API only, nothing published)."""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import mef3io
import pytest

import mef3io_studio
from mef3io_studio._version import app_version
from mef3io_studio.app import forbidden_argument, main, sanitize_qt_environment

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "mef3io_studio"


def test_self_test_module_entry(tmp_path):
    report = tmp_path / "report.txt"
    out = subprocess.run(
        [sys.executable, "-m", "mef3io_studio", "--self-test", "--self-test-report", str(report)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert re.search(r"^mef3io-studio self-test OK: wrote and read 2 channels", out.stdout, re.M)
    assert report.read_text().startswith("mef3io-studio self-test OK")


def test_self_test_gui_refuses_the_offscreen_platform(tmp_path):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run(
        [sys.executable, "-m", "mef3io_studio", "--self-test-gui"],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert out.returncode == 1
    assert "needs a real display" in out.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ["-platform", "vnc:port=5987"],
        ["-platform=vnc"],
        ["--platform", "vnc"],
        ["-plugin", "evdevmouse"],
        ["-platformpluginpath", "/tmp"],
    ],
)
def test_platform_arguments_are_rejected(argv, capsys):
    assert forbidden_argument(argv)
    assert main(argv) == 2
    assert "not supported" in capsys.readouterr().err


@pytest.mark.parametrize(
    "value,kept",
    [
        ("vnc:addr=127.0.0.1:port=5987", False),
        ("vnc", False),
        ("linuxfb", False),
        ("eglfs", False),
        ("minimal", False),
        ("offscreen", True),
    ],
)
def test_unsupported_qpa_platform_is_ignored(monkeypatch, value, kept):
    monkeypatch.setenv("QT_QPA_PLATFORM", value)
    sanitize_qt_environment()
    assert (os.environ.get("QT_QPA_PLATFORM") == value) is kept


def test_multiprocessing_child_never_opens_the_app():
    assert main(["--multiprocessing-fork", "tracker_fd=3"]) == 0


def test_version_comes_from_the_version_file():
    assert app_version() == (ROOT / "VERSION").read_text().strip()
    assert mef3io_studio.__version__ == app_version()


def test_mef3io_is_pinned_exactly_and_installed_matches():
    text = (ROOT / "pyproject.toml").read_text()
    pin = re.search(r'"mef3io==([\d.]+)"', text)
    assert pin, "mef3io must be pinned with =="
    assert mef3io.__version__ == pin.group(1)


def test_lock_files_match_the_pins():
    text = (ROOT / "pyproject.toml").read_text()
    pins = dict(re.findall(r'"([A-Za-z0-9_.-]+)==([\d.]+)"', text))
    for lock in ("requirements/build.lock", "requirements/dev.lock"):
        body = (ROOT / lock).read_text().lower()
        assert "--hash=sha256:" in body
        for name, version in pins.items():
            if name.lower() == "pyinstaller" and "dev" in lock:
                continue
            assert f"{name.lower()}=={version}" in body, (lock, name)


def test_no_publish_step_anywhere():
    text = (ROOT / "pyproject.toml").read_text()
    assert "Private :: Do Not Upload" in text
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        body = wf.read_text().lower()
        for marker in ("twine", "gh-action-pypi-publish", "upload.pypi.org", "pypi_token"):
            assert marker not in body, (wf.name, marker)


# --------------------------------------------------------------------------
# The mef3io boundary: public API only, and the app never handles MEF files.
# A static guard — it catches the ways this has been (or could plausibly be)
# bypassed: aliases, getattr/importlib/__import__ with "_" names, string
# constants naming private modules, and file extensions built from pieces.
# --------------------------------------------------------------------------

MEF_FILE_WORDS = ("tdat", "tmet", "tidx", "rdat", "ridx", "segd", "timd")


def _pieces(node):
    """String constants of a `+` chain or f-string, with None for anything
    computed, so 'p + "." + "tm" + "et"' gives [None, ".", "tm", "et"]."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _pieces(node.left) + _pieces(node.right)
    if isinstance(node, ast.JoinedStr):
        return [v.value if isinstance(v, ast.Constant) else None for v in node.values]
    return [None]


def _const_str(node):
    """The constant text of a node: runs of adjacent constant pieces joined
    with a space where something computed sits between them."""
    pieces = _pieces(node)
    if all(p is None for p in pieces):
        return None
    out, run = [], ""
    for p in pieces:
        if p is None:
            if run:
                out.append(run)
            run = ""
        else:
            run += p
    if run:
        out.append(run)
    return " ".join(out)


# The log filter names MEF file suffixes only to REDACT them from log text;
# it never opens a file.
MEF_NAMES_ALLOWED_IN = {"privacy.py"}


def boundary_offences(source: str, name: str = "<src>") -> list[str]:
    tree = ast.parse(source)
    aliases = {"mef3io"}
    offences = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "mef3io" and a.asname:
                    aliases.add(a.asname)
                parts = a.name.split(".")
                if parts[0] == "mef3io" and any(p.startswith("_") for p in parts[1:]):
                    offences.append(f"{name}: import {a.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            parts = node.module.split(".")
            if parts[0] == "mef3io":
                if any(p.startswith("_") for p in parts[1:]):
                    offences.append(f"{name}: from {node.module}")
                for a in node.names:
                    if a.name.startswith("_"):
                        offences.append(f"{name}: from mef3io import {a.name}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if (
                node.value.id in aliases
                and node.attr.startswith("_")
                and node.attr != "__version__"
            ):
                offences.append(f"{name}: {node.value.id}.{node.attr}")
        if isinstance(node, ast.Call):
            func = node.func
            fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            args = [
                a.value if isinstance(a, ast.Constant) and isinstance(a.value, str) else None
                for a in node.args
            ]
            if fname in ("getattr", "hasattr", "setattr") and len(node.args) >= 2:
                target = node.args[0]
                attr = args[1]
                if (
                    isinstance(target, ast.Name)
                    and target.id in aliases
                    and attr
                    and attr.startswith("_")
                    and attr != "__version__"
                ):
                    offences.append(f"{name}: {fname}({target.id}, {attr!r})")
                if attr is None and isinstance(target, ast.Name) and target.id in aliases:
                    offences.append(f"{name}: {fname}({target.id}, <computed name>)")
            if fname in ("import_module", "__import__") and args and args[0]:
                if args[0].startswith("mef3io") and "._" in args[0]:
                    offences.append(f"{name}: {fname}({args[0]!r})")
        text = (
            _const_str(node) if isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr)) else None
        )
        if text:
            lowered = text.lower()
            if "mef3io._" in lowered:
                offences.append(f"{name}: string {text!r}")
            for word in MEF_FILE_WORDS if name not in MEF_NAMES_ALLOWED_IN else ():
                if re.search(rf"\.{word}\b", lowered) or lowered.strip(". ") == word:
                    offences.append(f"{name}: MEF file name {text!r}")
    return offences


def test_only_mef3io_public_api_and_no_mef_files():
    offences = []
    for path in SRC.rglob("*.py"):
        offences += boundary_offences(path.read_text(encoding="utf-8"), path.name)
    assert not offences, offences


@pytest.mark.parametrize(
    "snippet",
    [
        "import mef3io._mef3io",
        "from mef3io import _mef3io",
        "from mef3io._reader import Reader",
        "import mef3io as m\nm._mef3io",
        "import mef3io\ngetattr(mef3io, '_mef3io')",
        "import mef3io as m\ngetattr(m, name)",
        "import importlib\nimportlib.import_module('mef3io._mef3io')",
        "__import__('mef3io._writer')",
        "x = 'mef3io._mef3io'",
        "open(p + '.tdat')",
        "open(p + '.' + 'tm' + 'et')",
        "ext = 'tidx'",
        "f'{base}.tdat'",
    ],
)
def test_boundary_guard_catches_bypasses(snippet):
    assert boundary_offences(snippet), snippet
