"""The --self-test entry point, the version source, and the project rules."""

import ast
import re
import subprocess
import sys
from pathlib import Path

import mef3io

import mef3io_studio
from mef3io_studio._version import app_version

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


def test_version_comes_from_the_version_file():
    assert app_version() == (ROOT / "VERSION").read_text().strip()
    assert mef3io_studio.__version__ == app_version()


def test_mef3io_is_pinned_exactly_and_installed_matches():
    text = (ROOT / "pyproject.toml").read_text()
    pin = re.search(r'"mef3io==([\d.]+)"', text)
    assert pin, "mef3io must be pinned with =="
    assert mef3io.__version__ == pin.group(1)


def test_no_publish_step_anywhere():
    text = (ROOT / "pyproject.toml").read_text()
    assert "Private :: Do Not Upload" in text
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        body = wf.read_text().lower()
        for marker in ("twine", "gh-action-pypi-publish", "upload.pypi.org", "pypi_token"):
            assert marker not in body, (wf.name, marker)


def test_only_mef3io_public_api_and_no_mef_bytes():
    offenders = []
    for path in SRC.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("mef3io")
            ):
                if node.module.startswith("mef3io.") and node.module.split(".")[1].startswith("_"):
                    offenders.append(f"{path.name}: from {node.module}")
                for alias in node.names:
                    if node.module == "mef3io" and alias.name.startswith("_"):
                        offenders.append(f"{path.name}: {alias.name}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if parts[0] == "mef3io" and len(parts) > 1 and parts[1].startswith("_"):
                        offenders.append(f"{path.name}: import {alias.name}")
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if (
                    node.value.id == "mef3io"
                    and node.attr.startswith("_")
                    and node.attr != "__version__"
                ):
                    offenders.append(f"{path.name}: mef3io.{node.attr}")
        # The app never opens MEF files itself.
        for ext in (".tmet", ".tidx", ".tdat", ".rdat", ".ridx"):
            if ext in source:
                offenders.append(f"{path.name}: mentions {ext}")
    assert not offenders, offenders
