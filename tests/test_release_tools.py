"""The version bump (scripts/bump_version.py) and the release-notes gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("bump_version", ROOT / "scripts" / "bump_version.py")
bump = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bump)


@pytest.mark.parametrize(
    ("current", "kind", "expected"),
    [
        ("0.1.0.dev3", "dev", "0.1.0.dev4"),
        ("0.1.0", "dev", "0.1.1.dev1"),
        ("0.1.0.dev4", "release", "0.1.0"),
        ("0.2.0rc1", "release", "0.2.0"),
        ("0.1.0", "patch", "0.1.1"),
        ("0.1.3", "minor", "0.2.0"),
        ("0.9.3", "major", "1.0.0"),
        ("0.1.0.dev3", "0.2.0.dev1", "0.2.0.dev1"),
        ("0.1.0.dev3", "0.1.0rc1", "0.1.0rc1"),
        ("0.1.0rc1", "0.1.0", "0.1.0"),
    ],
)
def test_next_version(current, kind, expected):
    assert bump.next_version(current, kind) == expected


@pytest.mark.parametrize(
    ("current", "kind"),
    [
        ("0.1.0", "release"),  # already finished
        ("0.1.0.dev3", "patch"),  # ambiguous from a test version
        ("0.1.0rc1", "dev"),
        ("0.1.0.dev3", "0.1.0.dev3"),  # not newer
        ("0.1.0.dev3", "0.1.0.dev2"),
        ("0.1.0", "0.1.0rc1"),  # a pre-release of a version already finished
        ("0.1.0", "v0.2.0"),  # not a version
        ("0.1.0", "0.2"),
    ],
)
def test_next_version_refuses(current, kind):
    with pytest.raises(ValueError):
        bump.next_version(current, kind)


def test_ordering_follows_pep440():
    from packaging.version import Version

    versions = [
        "0.1.0.dev1",
        "0.1.0.dev10",
        "0.1.0a1",
        "0.1.0b2",
        "0.1.0rc1",
        "0.1.0",
        "0.1.1.dev1",
    ]
    assert sorted(versions, key=bump.sort_key) == sorted(versions, key=Version)


def test_bump_writes_version_and_a_stub(tmp_path, monkeypatch):
    (tmp_path / "docs" / "release-notes").mkdir(parents=True)
    (tmp_path / "VERSION").write_text("0.1.0.dev3\n")
    monkeypatch.setattr(bump, "ROOT", tmp_path)
    assert bump.main(["dev"]) == 0
    assert (tmp_path / "VERSION").read_text() == "0.1.0.dev4\n"
    stub = (tmp_path / "docs" / "release-notes" / "0.1.0.dev4.md").read_text()
    assert bump.STUB_MARKER in stub
    assert bump.main(["patch"]) == 1  # refused, nothing changed
    assert (tmp_path / "VERSION").read_text() == "0.1.0.dev4\n"


def test_current_version_has_written_release_notes():
    """A release PR cannot pass CI until its notes are written: build.yml
    publishes docs/release-notes/<VERSION>.md as the release description and
    refuses a missing file or the stub."""
    version = (ROOT / "VERSION").read_text().strip()
    bump.parse(version)
    notes = ROOT / "docs" / "release-notes" / f"{version}.md"
    assert notes.is_file(), f"write {notes.relative_to(ROOT)}"
    assert bump.STUB_MARKER not in notes.read_text(encoding="utf-8"), (
        "the release notes are still the stub"
    )


def test_workflows_share_the_stub_marker():
    build = (ROOT / ".github" / "workflows" / "build.yml").read_text()
    assert bump.STUB_MARKER in build
