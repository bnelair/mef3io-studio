"""The Rename window (#12), offscreen: the preview in time order, names from a
pattern, the checks in plain words, the confirmation, the result, Undo,
passwords, a folder renamed elsewhere, and nothing identifying in the log."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import mef3io
import numpy as np
import pytest
from explorer_sessions import PASSWORD_BASIC, PASSWORD_FULL, SECOND, T0, write_encrypted
from PySide6.QtWidgets import QMessageBox

from mef3io_studio.common import privacy
from mef3io_studio.common.logging_setup import setup_logging, teardown_logging
from mef3io_studio.explorer import rename as ui
from mef3io_studio.explorer.rename import COL_NOTE, COL_START
from mef3io_studio.model import rename as rn

DAY = 86_400 * SECOND


def write_at(path: Path, start: int) -> Path:
    with mef3io.Writer(str(path), overwrite=True) as w:
        w.write("c", np.zeros(512), start, 256.0)
        w.write_annotations([{"time": start + SECOND, "text": "marker"}])
    for p in [path, *path.rglob("*")]:
        os.utime(p, (1e9, 1e9))
    return path


@pytest.fixture
def page(window):
    window._closing = True
    window.show()
    return window.explorer


def open_all(qtbot, page, paths, password=""):
    page.open_paths([str(p) for p in paths], interactive=False, password=password)
    qtbot.waitUntil(
        lambda: len(page.entries()) == len(paths) and all(not e.busy for e in page.entries()),
        timeout=60_000,
    )
    page.table.selectAll()


def opened_dialog(qtbot, page):
    dialog = page.rename_selected()
    qtbot.waitUntil(lambda: dialog._loading <= 0, timeout=60_000)
    return dialog


def wait_idle(qtbot, dialog):
    qtbot.waitUntil(lambda: not dialog.busy, timeout=60_000)


def listed_names(page):
    return sorted(e.name for e in page.entries())


def settle(qtbot, page):
    qtbot.waitUntil(
        lambda: page.entries() and all(not e.busy and e.outcome for e in page.entries()),
        timeout=60_000,
    )


@pytest.fixture
def three(tmp_path):
    return [
        write_at(tmp_path / "c3d1.mefd", T0 + 2 * DAY),
        write_at(tmp_path / "a9f0.mefd", T0 + 3 * DAY),
        write_at(tmp_path / "b442.mefd", T0),
    ]


def test_rename_in_time_order_from_a_pattern_then_undo(qtbot, page, three, tmp_path, monkeypatch):
    open_all(qtbot, page, three)
    dialog = opened_dialog(qtbot, page)
    # Listed by time, not by name; nothing is renamed yet.
    assert [r.old_name for r in dialog.rows] == ["b442", "c3d1", "a9f0"]
    assert dialog.table.item(0, COL_START).text() != ui.UNVERIFIED_START
    assert not dialog.rename_button.isEnabled()
    assert "Nothing to rename" in dialog.summary.text()
    dialog.pattern.setText("{index:03d}_{start:%Y%m%d}")
    dialog.apply_pattern()
    assert dialog.new_names() == ["001_20000101", "002_20000103", "003_20000104"]
    assert dialog.rename_button.isEnabled() and "Ready to rename 3" in dialog.summary.text()
    assert all(dialog.table.item(n, COL_NOTE).text() == "" for n in range(3))
    # The confirmation has no default "Yes".
    seen = {}

    def fake_exec(self):
        seen["default"] = self.defaultButton().text()
        seen["text"] = self.text()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    dialog.start()
    assert seen["default"] == "Don't rename" and "3 recordings" in seen["text"]
    assert not dialog.busy and all(p.exists() for p in three)
    # Confirmed.
    dialog.confirm = lambda text, more: True
    dialog.start()
    wait_idle(qtbot, dialog)
    qtbot.waitUntil(lambda: dialog._done, timeout=60_000)
    assert "3 recordings renamed" in dialog.status.text()
    assert sorted(p.name for p in tmp_path.glob("*.mefd")) == [
        "001_20000101.mefd",
        "002_20000103.mefd",
        "003_20000104.mefd",
    ]
    settle(qtbot, page)
    assert listed_names(page) == ["001_20000101", "002_20000103", "003_20000104"]
    for entry in page.entries():  # read again at the new place, notes found
        assert entry.details is not None and len(entry.details.notes) >= 1
    # Done: a second press cannot rename again; Undo is offered.
    assert not dialog.rename_button.isEnabled() and dialog.undo_button.isEnabled()
    dialog.start_undo()
    qtbot.waitUntil(lambda: dialog.batch is None, timeout=60_000)
    assert "3 recordings put back" in dialog.status.text()
    assert sorted(p.name for p in tmp_path.glob("*.mefd")) == [
        "a9f0.mefd",
        "b442.mefd",
        "c3d1.mefd",
    ]
    settle(qtbot, page)
    assert listed_names(page) == ["a9f0", "b442", "c3d1"]
    assert page._last_rename is None


def test_undo_comes_back_in_a_new_window_and_asks_before_doing_it(qtbot, page, three, monkeypatch):
    open_all(qtbot, page, three[:1])
    dialog = opened_dialog(qtbot, page)
    dialog.set_new_name(0, "renamed-one")
    dialog.confirm = lambda text, more: True
    dialog.start()
    qtbot.waitUntil(lambda: dialog._done, timeout=60_000)
    settle(qtbot, page)
    dialog.close()
    page.table.selectAll()
    again = page.rename_selected()
    qtbot.waitUntil(lambda: again._loading <= 0, timeout=60_000)
    assert again.undo_button.isEnabled()
    declined = []
    again.confirm = lambda text, more: bool(declined.append(text))
    again.start_undo()
    qtbot.waitUntil(lambda: bool(declined), timeout=60_000)
    assert Path(three[0].parent / "renamed-one.mefd").exists()  # asked, then left alone
    again.confirm = lambda text, more: True
    again.start_undo()
    qtbot.waitUntil(lambda: again.batch is None, timeout=60_000)
    assert three[0].exists()


def test_names_that_cannot_be_used_say_why_and_block_the_rename(qtbot, page, three):
    open_all(qtbot, page, three)
    dialog = opened_dialog(qtbot, page)
    taken = dialog.rows[1].old_name  # another recording's name
    dialog.set_new_name(0, taken)
    assert not dialog.rename_button.isEnabled()
    assert "already in the folder" in dialog.table.item(0, COL_NOTE).text()
    dialog.set_new_name(0, "n" * 251)
    assert "too long" in dialog.table.item(0, COL_NOTE).text()
    dialog.set_new_name(0, "bad/name")
    assert "not allow" in dialog.table.item(0, COL_NOTE).text()
    dialog.set_new_name(0, "CON")
    assert "Windows keeps" in dialog.table.item(0, COL_NOTE).text()
    dialog.set_new_name(0, "fine-1")
    dialog.set_new_name(1, "FINE-1")  # capitals do not make it different
    assert "same name" in dialog.table.item(1, COL_NOTE).text()
    assert not dialog.rename_button.isEnabled()
    dialog.set_new_name(1, "fine-2")
    assert dialog.rename_button.isEnabled()
    # A typed ending is not doubled.
    dialog.set_new_name(1, "fine-3.mefd")
    assert dialog.new_names()[1] == "fine-3"
    # Nothing was touched while the names were wrong.
    assert all(p.exists() for p in three)


def test_two_recordings_swapping_names_is_refused_in_plain_words(qtbot, page, three):
    open_all(qtbot, page, three[:2])
    dialog = opened_dialog(qtbot, page)
    a, b = dialog.rows[0].old_name, dialog.rows[1].old_name
    dialog.set_new_name(0, b)
    dialog.set_new_name(1, a)
    assert "swap names" in dialog.table.item(0, COL_NOTE).text()
    assert not dialog.rename_button.isEnabled()


def test_a_chain_of_names_is_renamed_in_the_right_order(qtbot, page, three, tmp_path):
    open_all(qtbot, page, three[:2])
    dialog = opened_dialog(qtbot, page)
    first, second = dialog.rows[0].old_name, dialog.rows[1].old_name
    dialog.set_new_name(0, second)  # takes the second one's name ...
    dialog.set_new_name(1, "elsewhere")  # ... after that one moved away
    assert dialog.rename_button.isEnabled()
    dialog.confirm = lambda text, more: True
    dialog.start()
    qtbot.waitUntil(lambda: dialog._done, timeout=60_000)
    assert sorted(p.name for p in tmp_path.glob("*.mefd")) == sorted(
        [f"{second}.mefd", "elsewhere.mefd", "b442.mefd"]
    )
    assert first not in [p.stem for p in tmp_path.glob("*.mefd")]


def test_a_protected_recording_is_renamed_with_its_password(qtbot, page, tmp_path):
    locked = write_encrypted(tmp_path / "locked.mefd")
    for p in [locked, *locked.rglob("*")]:
        os.utime(p, (1e9, 1e9))
    open_all(qtbot, page, [locked])  # no password: it stays locked
    dialog = opened_dialog(qtbot, page)
    dialog.set_new_name(0, "unlocked-name")
    assert "password" in dialog.table.item(0, COL_NOTE).text().lower()
    assert not dialog.rename_button.isEnabled()
    # The start time of a protected recording is not confirmed: no pattern from it.
    assert dialog.table.item(0, COL_START).text() == ui.UNVERIFIED_START
    dialog.close()
    entry = page.entries()[0]
    page._unlock_with(entry, PASSWORD_BASIC)
    settle(qtbot, page)
    page.table.selectAll()
    dialog = opened_dialog(qtbot, page)
    dialog.set_new_name(0, "unlocked-name")
    assert dialog.rename_button.isEnabled()
    dialog.confirm = lambda text, more: True
    dialog.start()
    qtbot.waitUntil(lambda: dialog._done, timeout=60_000)
    assert (tmp_path / "unlocked-name.mefd").exists()
    settle(qtbot, page)
    assert page.entries()[0].password == PASSWORD_BASIC  # the same password carries over
    # Undo asks for the password again when the app does not hold it.
    assert dialog.undo_button.isEnabled()


def test_unverified_times_stay_out_of_the_order_and_cannot_name_from_the_start(
    qtbot, page, tmp_path
):
    plain = write_at(tmp_path / "plain.mefd", T0 + 5 * DAY)
    locked = write_encrypted(tmp_path / "locked.mefd")
    for p in [locked, *locked.rglob("*")]:
        os.utime(p, (1e9, 1e9))
    open_all(qtbot, page, [plain, locked])
    dialog = opened_dialog(qtbot, page)
    assert [r.old_name for r in dialog.rows] == ["plain", "locked"]  # not placed by a guess
    assert dialog.rows[0].index == 1 and dialog.rows[1].index is None
    dialog.apply_template("{start:%Y%m%d}")
    assert dialog.rows[0].new_name == "20000106"
    assert dialog.rows[1].new_name == "locked"  # unchanged
    assert "start time" in dialog.table.item(1, COL_NOTE).text()
    dialog.apply_template("{index:02d}")
    assert dialog.rows[1].new_name == "locked"
    assert "bad/pattern" not in dialog.new_names()
    dialog.apply_template("{nonsense}")
    assert "pattern cannot be used" in dialog.table.item(0, COL_NOTE).text()


def test_a_folder_renamed_by_another_program_is_put_in_step_first(qtbot, page, three, tmp_path):
    os.rename(three[0], tmp_path / "moved.mefd")
    drifted = tmp_path / "moved.mefd"
    open_all(qtbot, page, [drifted])
    dialog = opened_dialog(qtbot, page)
    assert dialog.drift_box.isVisible() or dialog.drift_box.isVisibleTo(dialog)
    assert rn.DRIFT_NOTE in dialog.table.item(0, COL_NOTE).text()
    # Not renaming, only fixing:
    assert "Ready to rename 1" in dialog.summary.text()
    dialog.drift_box.setChecked(False)
    dialog.set_new_name(0, "final")
    assert not dialog.rename_button.isEnabled()  # not while the names disagree
    dialog.drift_box.setChecked(True)
    assert dialog.rename_button.isEnabled()
    dialog.confirm = lambda text, more: True
    dialog.start()
    qtbot.waitUntil(lambda: dialog._done, timeout=60_000)
    drift = mef3io.name_drift(tmp_path / "final.mefd")
    assert drift.consistent and set(drift.stored_names) == {"final"}


def test_a_recording_open_in_a_viewer_or_written_a_moment_ago_is_not_renamed(
    qtbot, page, three, tmp_path
):
    fresh = tmp_path / "fresh.mefd"
    with mef3io.Writer(str(fresh), overwrite=True) as w:
        w.write("c", np.zeros(512), T0, 256.0)
    open_all(qtbot, page, [three[0], fresh])
    in_use = page.entries()[0].key
    page.in_use_keys = lambda: {in_use}
    dialog = opened_dialog(qtbot, page)
    by_name = {r.old_name: n for n, r in enumerate(dialog.rows)}
    for name in by_name:
        dialog.set_new_name(by_name[name], name + "-x")
    notes = {name: dialog.table.item(n, COL_NOTE).text() for name, n in by_name.items()}
    assert "signal viewer" in notes[three[0].stem]
    assert "last few minutes" in notes["fresh"]
    assert not dialog.rename_button.isEnabled()


def test_an_interrupted_rename_is_explained_and_put_back(qtbot, page, three, tmp_path):
    from mef3io_studio.model import rename_ops

    with pytest.raises(mef3io.IoError):
        mef3io.rename_session(
            str(three[0]), "never-finished", backup_dir=rename_ops.backup_dir(), _crash_at=3
        )
    left = next(p for p in tmp_path.glob("*.mefd") if p.stem not in {"a9f0", "b442"})
    open_all(qtbot, page, [left])
    dialog = opened_dialog(qtbot, page)
    assert rn.UNFINISHED_NOTE in dialog.table.item(0, COL_NOTE).text()
    assert "Nothing is lost" in rn.UNFINISHED_NOTE
    assert dialog.rename_button.isEnabled()  # leaving the name: only puts it back
    dialog.confirm = lambda text, more: True
    dialog.start()
    qtbot.waitUntil(lambda: dialog._done, timeout=60_000)
    assert "put back" in dialog.status.text()
    assert three[0].exists()


def test_an_archive_is_renamed_as_a_file_and_the_window_says_the_inside_keeps_its_name(
    qtbot, page, three, tmp_path
):
    tar = tmp_path / "packed.mefd.tar"
    mef3io.archive_session(str(three[0]), str(tar), False)
    os.utime(tar, (1e9, 1e9))
    open_all(qtbot, page, [tar])
    dialog = opened_dialog(qtbot, page)
    assert dialog.archive_note.isVisibleTo(dialog)
    assert "old name" in dialog.archive_note.text()
    dialog.set_new_name(0, "n" * 247)
    assert "too long" in dialog.table.item(0, COL_NOTE).text()
    dialog.set_new_name(0, "n" * 246)
    assert dialog.rename_button.isEnabled()
    dialog.set_new_name(0, "new-archive")
    dialog.confirm = lambda text, more: True
    dialog.start()
    qtbot.waitUntil(lambda: dialog._done, timeout=60_000)
    assert (tmp_path / "new-archive.mefd.tar").exists() and not tar.exists()
    settle(qtbot, page)
    assert listed_names(page) == ["new-archive"]


def test_renaming_never_logs_names_or_the_password(qtbot, window, tmp_path):
    log_file = setup_logging(tmp_path / "logs", level=logging.DEBUG)
    privacy.identities.clear()
    window._closing = True
    page = window.explorer
    try:
        path = write_at(tmp_path / "Secret-Study-Name.mefd", T0)
        open_all(qtbot, page, [path])
        dialog = opened_dialog(qtbot, page)
        dialog.set_new_name(0, "Brand-New-Label")
        dialog.confirm = lambda text, more: True
        dialog.start()
        qtbot.waitUntil(lambda: dialog._done, timeout=60_000)
        dialog.start_undo()
        qtbot.waitUntil(lambda: dialog.batch is None, timeout=60_000)
    finally:
        teardown_logging()
        privacy.identities.clear()
    text = log_file.read_text(encoding="utf-8")
    assert "rename(s) done" in text  # it did log, by count
    for secret in ("Secret-Study-Name", "Brand-New-Label", str(tmp_path), PASSWORD_FULL):
        assert secret not in text, secret
