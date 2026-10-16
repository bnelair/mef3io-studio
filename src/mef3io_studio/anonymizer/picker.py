"""The channel picker: which channels go into the anonymized copies.

Built for recordings with hundreds of channels (mef3io-studio#5, the
256-channel comment). Every channel is kept by default; excluding is the
deliberate act. Channels are grouped by electrode name prefix with one
tri-state box per group, and the groups fold away. A click ticks or unticks
one channel; Shift+click applies the same to every channel between it and
the previous click; Ctrl/Cmd+click highlights rows, and Space ticks or
unticks the highlighted ones. A filter narrows the list, and "Exclude N
shown" / "Keep N shown" act on exactly what it shows. Undo and redo cover
every change. "Keeping N of M channels" is always visible.

The rules live in ``model.channels``; this is only the view of them.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..common.pages import scaled_font
from ..model.channels import OTHER_GROUP, ChannelGroup, ChannelSelection
from ..model.timefmt import format_count

_GROUP = 0  # internal id of a group row; channel rows carry group index + 1

COLUMNS = ("Channel", "Sampling rate", "Unit", "In recordings")


class ChannelTreeModel(QAbstractItemModel):
    """Groups and channels over a :class:`ChannelSelection`."""

    #: A channel's box was clicked: (name, keep). The picker decides what
    #: that means (one channel, or a Shift+click range).
    channel_clicked = Signal(str, bool)
    #: A group's box was clicked: (group index, keep).
    group_clicked = Signal(int, bool)

    def __init__(self, selection: ChannelSelection, recordings: int, parent=None):
        super().__init__(parent)
        self.selection = selection
        self.recordings = recordings
        self.groups: tuple[ChannelGroup, ...] = selection.groups

    # -- structure -------------------------------------------------------------
    def index(self, row, column, parent=QModelIndex()):  # noqa: B008
        if row < 0 or column < 0 or column >= self.columnCount():
            return QModelIndex()
        if not parent.isValid():
            if row >= len(self.groups):
                return QModelIndex()
            return self.createIndex(row, column, _GROUP)
        if parent.internalId() != _GROUP or row >= len(self.groups[parent.row()].rows):
            return QModelIndex()
        return self.createIndex(row, column, parent.row() + 1)

    def parent(self, index=QModelIndex()):  # noqa: B008
        if not index.isValid() or index.internalId() == _GROUP:
            return QModelIndex()
        return self.createIndex(index.internalId() - 1, 0, _GROUP)

    def rowCount(self, parent=QModelIndex()):  # noqa: B008, N802
        if not parent.isValid():
            return len(self.groups)
        if parent.internalId() == _GROUP:
            return len(self.groups[parent.row()].rows)
        return 0

    def columnCount(self, parent=QModelIndex()):  # noqa: B008, N802
        return len(COLUMNS) if self.recordings > 1 else len(COLUMNS) - 1

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
        return None

    def group_label(self, group: ChannelGroup) -> str:
        n = len(group.rows)
        if group.prefix == OTHER_GROUP:
            return f"Other channels ({n})"
        return f"{group.prefix} ({n})"

    def name_of(self, index: QModelIndex) -> str | None:
        if not index.isValid() or index.internalId() == _GROUP:
            return None
        return self.groups[index.internalId() - 1].rows[index.row()].name

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        col = index.column()
        if index.internalId() == _GROUP:
            group = self.groups[index.row()]
            if role == Qt.ItemDataRole.DisplayRole and col == 0:
                return self.group_label(group)
            if role == Qt.ItemDataRole.CheckStateRole and col == 0:
                state = self.selection.group_state(group)
                if state is None:
                    return Qt.CheckState.PartiallyChecked
                return Qt.CheckState.Checked if state else Qt.CheckState.Unchecked
            return None
        row = self.groups[index.internalId() - 1].rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return row.name
            if col == 1:
                return row.rate
            if col == 2:
                return row.unit
            if col == 3:
                missing = row.present_in < self.recordings
                text = f"{row.present_in} of {self.recordings}"
                return f"⚠ {text}" if missing else text
        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            kept = self.selection.is_kept(row.name)
            return Qt.CheckState.Checked if kept else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.ToolTipRole and col == 3 and row.present_in < self.recordings:
            return "This channel is not in every recording of the batch."
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):  # noqa: N802
        if role != Qt.ItemDataRole.CheckStateRole or not index.isValid():
            return False
        state = Qt.CheckState(value) if not isinstance(value, Qt.CheckState) else value
        if index.internalId() == _GROUP:
            group = self.groups[index.row()]
            # A partly ticked group becomes all kept on a click.
            keep = self.selection.group_state(group) is not True
            self.group_clicked.emit(index.row(), keep)
            return True
        name = self.name_of(index)
        self.channel_clicked.emit(name, state == Qt.CheckState.Checked)
        return True

    def refresh(self) -> None:
        """Every box may have changed. One signal for the groups; the
        channels' boxes are read again when the view repaints (see
        ``ChannelPicker._do``): a signal per channel row costs a visible
        pause on 256 channels."""
        if self.groups:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self.groups) - 1, 0),
                [Qt.ItemDataRole.CheckStateRole],
            )


class ChannelTree(QTreeView):
    """Space ticks or unticks the highlighted channels."""

    space_pressed = Signal()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Space:
            self.space_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ChannelPicker(QWidget):
    """The channel step's widget. ``changed`` fires after every change."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selection: ChannelSelection | None = None
        self.model: ChannelTreeModel | None = None
        self._anchor: str | None = None

        intro = QLabel(
            "Every channel goes into the copies unless you leave it out. Click a box to "
            "leave a channel out; Shift+click to do the same for every channel between it "
            "and the one you clicked before. Ctrl+click (Cmd+click on a Mac) highlights "
            "channels; Space then ticks or unticks the highlighted ones.",
            self,
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        self.count_label = QLabel("", self)
        self.count_label.setFont(scaled_font(self, 1.1, bold=True))
        self.filter = QLineEdit(self)
        self.filter.setPlaceholderText("Filter channels, e.g. LA or ECG")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        self.exclude_shown = QPushButton("Exclude shown", self)
        self.exclude_shown.clicked.connect(lambda: self._shown(False))
        self.keep_shown = QPushButton("Keep shown", self)
        self.keep_shown.clicked.connect(lambda: self._shown(True))
        self.keep_all = QPushButton("Keep all", self)
        self.keep_all.clicked.connect(lambda: self._do(lambda s: s.keep_all()))
        self.exclude_all = QPushButton("Exclude all", self)
        self.exclude_all.clicked.connect(lambda: self._do(lambda s: s.exclude_all()))
        self.invert = QPushButton("Invert", self)
        self.invert.clicked.connect(lambda: self._do(lambda s: s.invert()))
        self.undo_button = QPushButton("Undo", self)
        self.undo_button.clicked.connect(self.undo)
        self.redo_button = QPushButton("Redo", self)
        self.redo_button.clicked.connect(self.redo)
        self.expand_button = QPushButton("Open all groups", self)
        self.expand_button.clicked.connect(lambda: self.tree.expandAll())
        self.collapse_button = QPushButton("Fold all groups", self)
        self.collapse_button.clicked.connect(lambda: self.tree.collapseAll())
        self.tree = ChannelTree(self)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.space_pressed.connect(self.toggle_highlighted)
        self.missing_label = QLabel("", self)
        self.missing_label.setWordWrap(True)
        self.missing_label.setObjectName("Bad")
        self.empty_label = QLabel("", self)
        self.empty_label.setWordWrap(True)
        self.empty_label.setObjectName("Bad")

        filter_row = QHBoxLayout()
        filter_row.addWidget(self.filter, 1)
        filter_row.addWidget(self.exclude_shown)
        filter_row.addWidget(self.keep_shown)
        bulk = QHBoxLayout()
        for b in (self.keep_all, self.exclude_all, self.invert):
            bulk.addWidget(b)
        bulk.addStretch(1)
        for b in (self.expand_button, self.collapse_button, self.undo_button, self.redo_button):
            bulk.addWidget(b)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(intro)
        layout.addWidget(self.count_label)
        layout.addLayout(filter_row)
        layout.addLayout(bulk)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.missing_label)
        layout.addWidget(self.empty_label)

        for keys, slot in (
            (QKeySequence.StandardKey.Undo, self.undo),
            (QKeySequence.StandardKey.Redo, self.redo),
        ):
            shortcut = QShortcut(keys, self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)

    # -- setting up ------------------------------------------------------------
    def set_selection(self, selection: ChannelSelection, recordings: int) -> None:
        self.selection = selection
        self._anchor = None
        old = self.model
        self.model = ChannelTreeModel(selection, recordings, self)
        self.model.channel_clicked.connect(self._on_channel_clicked)
        self.model.group_clicked.connect(self._on_group_clicked)
        self.tree.setModel(self.model)
        if old is not None:
            old.deleteLater()
        # Column widths are measured once here, never per change: measuring
        # 256 rows on every tick would make each click visibly slow.
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        em = self.fontMetrics().height()
        for col in range(1, self.model.columnCount()):
            header.resizeSection(col, max(header.sectionSizeHint(col), int(em * 7)))
        # Short lists open; long ones fold so every group fits on screen.
        if selection.total <= 64:
            self.tree.expandAll()
        else:
            self.tree.collapseAll()
        self.filter.blockSignals(True)
        self.filter.clear()
        self.filter.blockSignals(False)
        self._apply_filter("")
        self._update()

    # -- what is shown -------------------------------------------------------------
    def visible_names(self) -> tuple[str, ...]:
        if self.selection is None:
            return ()
        return self.selection.matching(self.filter.text())

    def _apply_filter(self, text: str) -> None:
        if self.model is None or self.selection is None:
            return
        shown = set(self.selection.matching(text))
        self.tree.setUpdatesEnabled(False)
        try:
            for gi, group in enumerate(self.model.groups):
                parent = self.model.index(gi, 0)
                any_shown = False
                for ri, row in enumerate(group.rows):
                    hide = row.name not in shown
                    any_shown = any_shown or not hide
                    if self.tree.isRowHidden(ri, parent) != hide:
                        self.tree.setRowHidden(ri, parent, hide)
                self.tree.setRowHidden(gi, QModelIndex(), not any_shown)
                if text.strip() and any_shown:
                    self.tree.expand(parent)
        finally:
            self.tree.setUpdatesEnabled(True)
        self._update()

    # -- changing ------------------------------------------------------------------
    def _do(self, action) -> None:
        if self.selection is None:
            return
        if action(self.selection):
            self.model.refresh()
            self.tree.viewport().update()
            self._update()
            self.changed.emit()

    def _on_channel_clicked(self, name: str, keep: bool) -> None:
        shift = bool(QGuiApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
        self.click(name, keep, shift=shift)

    def click(self, name: str, keep: bool, shift: bool = False) -> None:
        """A click on a channel's box (Shift: the range from the last click)."""
        anchor = self._anchor
        if shift and anchor is not None:
            self._do(lambda s: s.set_range(anchor, name, keep, self.visible_names()))
        else:
            self._do(lambda s: s.set_kept([name], keep))
        self._anchor = name

    def _on_group_clicked(self, group_index: int, keep: bool) -> None:
        self.set_group(group_index, keep)

    def set_group(self, group_index: int, keep: bool) -> None:
        group = self.model.groups[group_index]
        self._do(lambda s: s.set_kept(group.names, keep))

    def _shown(self, keep: bool) -> None:
        names = self.visible_names()
        self._do(lambda s: s.set_kept(names, keep))

    def highlighted_names(self) -> list[str]:
        if self.model is None:
            return []
        names: list[str] = []
        for index in self.tree.selectionModel().selectedRows(0):
            name = self.model.name_of(index)
            if name is not None:
                names.append(name)
            else:  # a highlighted group stands for its channels
                names += list(self.model.groups[index.row()].names)
        return list(dict.fromkeys(names))

    def toggle_highlighted(self) -> None:
        names = self.highlighted_names()
        if not names or self.selection is None:
            return
        keep = not all(self.selection.is_kept(n) for n in names)
        self._do(lambda s: s.set_kept(names, keep))

    def undo(self) -> None:
        self._do(lambda s: s.undo())

    def redo(self) -> None:
        self._do(lambda s: s.redo())

    # -- the labels ----------------------------------------------------------------
    def count_text(self) -> str:
        s = self.selection
        if s is None:
            return ""
        return f"Keeping {len(s.kept)} of {format_count(s.total, 'channel')}"

    def _update(self) -> None:
        s = self.selection
        if s is None:
            return
        self.count_label.setText(self.count_text())
        shown = len(self.visible_names())
        filtering = bool(self.filter.text().strip())
        self.exclude_shown.setText(f"Exclude {shown} shown")
        self.keep_shown.setText(f"Keep {shown} shown")
        for b in (self.exclude_shown, self.keep_shown):
            b.setVisible(filtering)
            b.setEnabled(shown > 0)
        self.undo_button.setEnabled(s.can_undo)
        self.redo_button.setEnabled(s.can_redo)
        missing = s.missing_from()
        if missing:
            names = ", ".join(list(missing)[:8]) + (" …" if len(missing) > 8 else "")
            self.missing_label.setText(
                f"⚠ {format_count(len(missing), 'channel name')} "
                f"{'is' if len(missing) == 1 else 'are'} not in every recording: {names}. "
                "Your choice applies wherever the name exists."
            )
        self.missing_label.setVisible(bool(missing))
        if not s.kept:
            self.empty_label.setText("Keep at least one channel to continue.")
        elif s.recordings_left_empty():
            n = len(s.recordings_left_empty())
            self.empty_label.setText(
                f"{format_count(n, 'recording')} would keep no channel at all. Keep at least "
                "one of its channels to continue."
            )
        self.empty_label.setVisible(not s.can_continue)
