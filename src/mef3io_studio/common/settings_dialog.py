"""Settings: the anonymizer's work folder, and keeping the computer awake.

The work folder is where each anonymized copy is made and checked before it
is archived (one recording at a time). By default it is a folder in the
app's own data area; a fast local disk is a good choice when the recordings
are on a network drive. It is checked when chosen: the app must be able to
write there, and it must not be inside a recording. It is stored sealed, so
the preferences never hold a path in clear.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..model.journal import KeyUnavailable, seal, unseal
from ..model.pipeline import default_work_folder, work_folder_problem
from .pages import scaled_font
from .settings import Settings


def work_folder(settings: Settings) -> str:
    sealed = settings.get("anonymizer/work_folder")
    if sealed:
        try:
            value = unseal(sealed)
        except KeyUnavailable:
            return default_work_folder()  # the setting itself is kept as it is
        if value and value.get("path"):
            return str(value["path"])
    return default_work_folder()


def set_work_folder(settings: Settings, folder: str | None) -> None:
    """Remember ``folder`` (None or "" = the default), and add it to the
    folders ever used (looked through for leftovers at start)."""
    settings.set("anonymizer/work_folder", seal({"path": folder}) if folder else "")
    remember_work_folder(settings, folder or default_work_folder())


def work_folders_used(settings: Settings) -> list[str]:
    sealed = settings.get("anonymizer/work_folders_used")
    try:
        value = unseal(sealed) if sealed else None
    except KeyUnavailable:
        value = None
    folders = list(value.get("paths", [])) if value else []
    for folder in (default_work_folder(), work_folder(settings)):
        if folder not in folders:
            folders.append(folder)
    return folders


def remember_work_folder(settings: Settings, folder: str) -> None:
    sealed = settings.get("anonymizer/work_folders_used")
    value = unseal(sealed) if sealed else None
    folders = list(value.get("paths", [])) if value else []
    if folder not in folders:
        folders.append(folder)
        settings.set("anonymizer/work_folders_used", seal({"paths": folders}))


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        em = self.fontMetrics().height()
        self.setMinimumWidth(em * 30)
        heading = QLabel("Anonymizing", self)
        heading.setFont(scaled_font(self, 1.1, bold=True))
        intro = QLabel(
            "When the copies are saved as archives, each one is first made and checked in "
            "the work folder, then archived and deleted from there. The work folder only "
            "ever holds the recording being worked on. A fast local disk is a good choice "
            "when your recordings are on a network drive.",
            self,
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        self.folder = work_folder(settings)
        self.folder_label = QLabel("", self)
        self.folder_label.setWordWrap(True)
        self.choose = QPushButton("Choose another work folder…", self)
        self.choose.clicked.connect(self._choose)
        self.default = QPushButton("Use the default", self)
        self.default.clicked.connect(lambda: self.set_folder(""))
        self.problem = QLabel("", self)
        self.problem.setObjectName("Bad")
        self.problem.setWordWrap(True)
        self.problem.setVisible(False)
        self.awake = QCheckBox("Keep the computer awake while a batch runs", self)
        self.awake.setChecked(settings.get("anonymizer/keep_awake"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(self.choose)
        row.addWidget(self.default)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(intro)
        layout.addWidget(self.folder_label)
        layout.addLayout(row)
        layout.addWidget(self.problem)
        layout.addWidget(self.awake)
        layout.addWidget(buttons)
        self.awake.toggled.connect(lambda on: settings.set("anonymizer/keep_awake", bool(on)))
        self._show()

    def _show(self) -> None:
        self.folder_label.setText(f"Work folder: {self.folder}")

    def choose_folder(self) -> str:
        return QFileDialog.getExistingDirectory(self, "Choose the work folder")

    def _choose(self) -> None:
        folder = self.choose_folder()
        if folder:
            self.set_folder(folder)

    def set_folder(self, folder: str) -> bool:
        """Check and remember a work folder ("" = the default). Returns
        whether it was accepted."""
        target = folder or default_work_folder()
        problem = work_folder_problem(target)
        self.problem.setText(problem or "")
        self.problem.setVisible(bool(problem))
        if problem:
            return False
        try:
            set_work_folder(self.settings, folder or None)
        except KeyUnavailable:
            from ..model.journal import KEY_PROBLEM

            self.problem.setText(KEY_PROBLEM)
            self.problem.setVisible(True)
            return False
        self.folder = target
        self._show()
        return True
