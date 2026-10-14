"""Plain language everywhere in the UI (docs/ui-language.md): technical terms
appear only behind "Technical details"."""

import ast
import re
from pathlib import Path

import pytest

from mef3io_studio.model.errors import describe_error

SRC = Path(__file__).resolve().parents[1] / "src" / "mef3io_studio"

# Files whose string literals are shown to users.
UI_FILES = [
    "app.py",
    "selftest.py",
    "common/pages.py",
    "common/activity.py",
    "common/dialogs.py",
    "model/errors.py",
    "model/progress.py",
    "model/jobs.py",
    "workers/tasks.py",
]

BANNED = [
    r"sessions?",
    r"metadata",
    r"encrypt\w*",
    r"decrypt\w*",
    r"L[12]",
    r"level[- ]?[12]",
    r"provenance",
    r"integrity",
    r"validat\w*",
    r"segments?",
    r"annotations?",
    r"uUTC",
    r"timestamps?",
    r"epoch",
    r"RED block",
    r"CRC",
    r"checksums?",
    r"t(?:dat|met|idx)",
    r"tracebacks?",
    r"exceptions?",
    r"GIL",
    r"threads?",
    r"subprocess",
    r"runnable",
    r"discontinuit\w*",
]
BANNED_RE = re.compile(r"(?<![\w.])(?:" + "|".join(BANNED) + r")(?!\w)", re.IGNORECASE)

LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
NON_UI_CALLS = {
    "technical",
    "getLogger",
    "setObjectName",
    "get",
    "set",
    "setValue",
    "value",
    "Signal",
    "QSettings",
    "Preference",
    "setStyle",
    "addPixmap",
    "icon",
    "render",
    "connect",
    "startswith",
    "replace",
    "split",
    "setenv",
    "import_module",
    "Template",
}


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def ui_strings(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            is_log = isinstance(func, ast.Attribute) and name in LOG_METHODS
            if is_log or name in NON_UI_CALLS:
                for sub in ast.walk(node):
                    skip.add(id(sub))
            for kw in node.keywords:  # detail="..." is the technical part
                if kw.arg == "detail":
                    for sub in ast.walk(kw.value):
                        skip.add(id(sub))
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "detail" for t in node.targets
        ):
            for sub in ast.walk(node.value):
                skip.add(id(sub))
        if isinstance(node, ast.Subscript):  # dict/env keys
            for sub in ast.walk(node.slice):
                skip.add(id(sub))
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


@pytest.mark.parametrize("rel", UI_FILES)
def test_user_facing_strings_use_everyday_words(rel):
    path = SRC / rel
    hits = [
        f"{rel}:{line}: {m.group(0)!r} in {text!r}"
        for line, text in ui_strings(path)
        if (m := BANNED_RE.search(text))
    ]
    assert not hits, "\n".join(hits)


def test_scanner_catches_jargon(tmp_path):
    sample = tmp_path / "x.py"
    sample.write_text('label = "Open session metadata"\nlog.info("session ok")\n')
    found = [t for _, t in ui_strings(sample) if BANNED_RE.search(t)]
    assert found == ["Open session metadata"]


def _errors():
    import errno

    import mef3io

    from mef3io_studio.model.cancel import JobCancelled

    return [
        mef3io.PasswordError("tmet is encrypted"),
        mef3io.CrcError("CRC mismatch in .tdat"),
        mef3io.FormatError("bad section 2"),
        mef3io.IoError("session path is not a directory"),
        PermissionError("x"),
        FileNotFoundError("x"),
        OSError(errno.ENOSPC, "full"),
        MemoryError(),
        JobCancelled(),
        ValueError("segment 3 traceback"),
    ]


@pytest.mark.parametrize("exc", _errors(), ids=lambda e: type(e).__name__)
def test_error_messages_are_plain_and_complete(exc):
    message = describe_error(exc)
    for text in (message.title, message.text):
        assert not BANNED_RE.search(text), text
    # what happened / what it means for the data / what to do next
    assert re.search(
        r"nothing was|not changed|were not|was not|untouched|removed|unaffected", message.text, re.I
    ), message.text
    if not isinstance(exc, (MemoryError,)) and type(exc).__name__ != "JobCancelled":
        assert re.search(r"\b(check|try|choose|use|free|ask|close)\b", message.text, re.I), (
            message.text
        )
