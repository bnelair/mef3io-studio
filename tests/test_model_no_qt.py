"""model/ must stay free of Qt, so its logic is testable without a GUI."""

import ast
import pkgutil
import subprocess
import sys
from pathlib import Path

import mef3io_studio.model as model

MODEL_DIR = Path(model.__file__).parent
QT_ROOTS = {"PySide6", "PyQt5", "PyQt6", "PySide2", "shiboken6", "qtpy"}


def test_model_sources_import_no_qt():
    offenders = []
    for path in MODEL_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            elif isinstance(node, ast.ImportFrom) and node.level > 0:
                # relative imports must stay inside model/
                if node.level > 1:
                    offenders.append(f"{path.name}: relative import leaves model/")
            offenders += [f"{path.name}: {n}" for n in names if n.split(".")[0] in QT_ROOTS]
    assert not offenders, offenders


def test_importing_model_loads_no_qt():
    modules = [f"mef3io_studio.model.{m.name}" for m in pkgutil.iter_modules([str(MODEL_DIR)])]
    code = (
        "import sys, importlib\n"
        f"for m in {modules!r}: importlib.import_module(m)\n"
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        f"{sorted(QT_ROOTS)!r}]\n"
        "assert not bad, bad\n"
        "print('ok')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout
