"""Asking for passwords again when an interrupted batch continues.

Passwords are never written into the batch journal, so continuing a batch
asks for them again: the full-access password of EACH original that needs
one (originals may have different passwords), and the two passwords of the
copies, each typed twice. Every one is then tried before the batch goes on
(the originals are opened with theirs, a finished copy with the new one).
Each original is named by its folder name and position, on the user's own
screen only (never logged, never in the journal's plain part); originals
that share a password can be given it once.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)


class ResumePasswordsDialog(QDialog):
    def __init__(
        self,
        sources: list[int],
        total: int,
        protected: bool,
        parent=None,
        names: dict[int, str] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Passwords for the batch")
        intro = QLabel(
            "Passwords are never stored, so type them again to continue the batch. Each one "
            "is tried before anything is made.",
            self,
        )
        intro.setWordWrap(True)
        names = names or {}
        self.sources: dict[int, QLineEdit] = {}
        form = QFormLayout()
        self.same = QCheckBox("They all use the same full-access password", self)
        self.same.setVisible(len(sources) > 1)
        self.same.toggled.connect(self._same_toggled)
        if len(sources) > 1:
            form.addRow("", self.same)
        self._labels: dict[int, QLabel] = {}
        for n in sources:
            field = QLineEdit(self)
            self.sources[n] = field
            # The original's folder name, on the user's own screen only: it
            # is never logged and never in the journal's plain part.
            name = names.get(n, "")
            where = (
                f"{name} (recording {n + 1} of {total})"
                if name
                else (f"recording {n + 1} of {total}")
            )
            label = QLabel(f"Full-access password of {where}:", self)
            self._labels[n] = label
            form.addRow(label, field)
        self.protected = protected
        self.basic = QLineEdit(self)
        self.basic_again = QLineEdit(self)
        self.full = QLineEdit(self)
        self.full_again = QLineEdit(self)
        if protected:
            form.addRow("Basic password of the copies:", self.basic)
            form.addRow("Basic password of the copies, again:", self.basic_again)
            form.addRow("Full-access password of the copies:", self.full)
            form.addRow("Full-access password of the copies, again:", self.full_again)
        for field in (
            *self.sources.values(),
            self.basic,
            self.basic_again,
            self.full,
            self.full_again,
        ):
            field.setEchoMode(QLineEdit.EchoMode.Password)
            field.textChanged.connect(self._update)
        self.problem = QLabel("", self)
        self.problem.setObjectName("Bad")
        self.problem.setWordWrap(True)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Not now")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)
        self._first_label = self._labels[sources[0]].text() if sources else ""
        self._update()

    def _same_toggled(self, same: bool) -> None:
        first, *others = list(self.sources)
        for n in others:
            self.sources[n].setVisible(not same)
            self._labels[n].setVisible(not same)
        self._labels[first].setText(
            "Full-access password of the originals:" if same else self._first_label
        )
        self._update()

    def problem_text(self) -> str:
        shown = [f for n, f in self.sources.items() if not self._grouped(n)]
        if any(not f.text() for f in shown):
            return "Type the full-access password of every original listed."
        if self.protected:
            if not self.basic.text() or not self.full.text():
                return "Type both passwords of the copies."
            if self.basic.text() != self.basic_again.text():
                return "The two basic passwords are not the same."
            if self.full.text() != self.full_again.text():
                return "The two full-access passwords are not the same."
        return ""

    def _update(self, *_args) -> None:
        text = self.problem_text()
        self.problem.setText(text)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not text)

    def values(self) -> tuple[dict[int, str], tuple[str, str] | None]:
        output = (self.basic.text(), self.full.text()) if self.protected else None
        if self.same.isChecked() and self.sources:
            first = next(iter(self.sources.values())).text()
            return {n: first for n in self.sources}, output
        return {n: f.text() for n, f in self.sources.items()}, output

    def _grouped(self, n: int) -> bool:
        """Hidden behind "the same password" (only the first one is typed)."""
        return self.same.isChecked() and n != next(iter(self.sources))
