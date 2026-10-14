"""Plain language everywhere in the UI (docs/ui-language.md): technical terms
appear only behind "Technical details"."""

import ast
import re
from pathlib import Path

import pytest

from mef3io_studio.model.errors import describe_error

SRC = Path(__file__).resolve().parents[1] / "src" / "mef3io_studio"

# Every module is scanned, so a new file is covered automatically. Only
# these are skipped, each for a stated reason:
NOT_UI = {
    "common/privacy.py": "regex patterns for the log filter (month names, path rules)",
    "common/theme.py": "the Qt style sheet",
    "common/icons.py": "icon file names",
    "common/logging_setup.py": "log file names and log records (never shown in the UI)",
    "workers/isolated.py": "helper-process plumbing; its errors reach users via model/errors",
    "model/cancel.py": "no text",
    "model/source.py": "data access; errors reach users via model/errors",
    "_version.py": "version lookup",
}
UI_FILES = sorted(
    p.relative_to(SRC).as_posix()
    for p in SRC.rglob("*.py")
    if p.relative_to(SRC).as_posix() not in NOT_UI
)

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
    # docs/ui-language.md: job/worker -> "task", cancel -> "stop",
    # records -> "notes and events", repair -> "fix", recover -> "rescue".
    # Qt's own standard buttons are not in our source, so this bans every
    # "Cancel" we would write ourselves.
    r"jobs?",
    r"workers?",
    r"cancel\w*",
    r"headers?",
    r"records?",
    r"repair\w*",
    r"recover\w*",
]
BANNED_RE = re.compile(r"(?<![\w.])(?:" + "|".join(BANNED) + r")(?!\w)", re.IGNORECASE)

LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
# Calls whose FIRST argument is an identifier, not text (a settings key, an
# object name, a logger name, ...). Only that first argument is skipped; any
# other argument (a default value, a lambda) is still scanned.
KEY_FIRST_CALLS = {
    "getLogger",
    "setObjectName",
    "get",
    "set",
    "setValue",
    "value",
    "Preference",
    "setStyle",
    "icon",
    "render",
    "startswith",
    "endswith",
    "replace",
    "split",
    "setenv",
    "import_module",
    "addPixmap",
    "setProperty",
}
# Calls whose text is never shown in the main UI.
WHOLE_CALL_SKIPS = {"technical", "Template", "compile", "QSettings"}


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

    def skip_tree(node):
        for sub in ast.walk(node):
            skip.add(id(sub))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            is_log = (
                isinstance(func, ast.Attribute)
                and name in LOG_METHODS
                and isinstance(func.value, ast.Name)
                and func.value.id in ("log", "logging", "logger")
            )
            if is_log or name in WHOLE_CALL_SKIPS:
                skip_tree(node)
            elif name in KEY_FIRST_CALLS and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant):
                    skip.add(id(first))
            for kw in node.keywords:  # detail="..." is the technical part
                if kw.arg == "detail":
                    skip_tree(kw.value)
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "detail" for t in node.targets
        ):
            skip_tree(node.value)
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            skip.add(id(node.slice))  # dict/env keys
        if isinstance(node, ast.Compare):  # comparisons against identifiers
            for sub in [node.left, *node.comparators]:
                if isinstance(sub, ast.Constant):
                    skip.add(id(sub))
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # all-lowercase Python identifiers ("cancelled", "ui_module") are
            # internal tokens; anything shown to people is capitalised or has
            # spaces
            if node.value.isidentifier() and node.value.islower():
                continue
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


@pytest.mark.parametrize(
    "code",
    [
        # (b) text inside a lambda passed to connect
        'button.clicked.connect(lambda: label.setText("Validate the session"))',
        # (c) text as the default of a .get(...)
        'title = options.get("title", "Session metadata")',
        # (d) new words from the glossary
        'QPushButton("Cancel job")',
        'label.setText("Repair the header")',
        'QAction("Recover records", self)',
        'status.showMessage("A worker is busy")',
        # a lambda inside a call whose first argument is a key
        'settings.get("ui/x", lambda: "Encrypted session")',
    ],
)
def test_scanner_has_no_bypasses(tmp_path, code):
    sample = tmp_path / "x.py"
    sample.write_text(code + "\n")
    assert [t for _, t in ui_strings(sample) if BANNED_RE.search(t)], code


def test_new_files_are_scanned_automatically():
    # (e) a module that is not in any hand-written list is still covered
    assert "app.py" in UI_FILES and "common/activity.py" in UI_FILES
    for rel in UI_FILES:
        assert rel not in NOT_UI
    assert set(NOT_UI) <= {p.relative_to(SRC).as_posix() for p in SRC.rglob("*.py")}


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
