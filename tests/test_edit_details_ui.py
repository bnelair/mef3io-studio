"""Explore's "Edit details (makes a new copy)", offscreen: hidden until
Show, only changed details sent, limits, times read-only, the passwords
choice, and a real copy through the helper process."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import mef3io
import pytest
from explorer_sessions import (
    FIRST,
    LAST,
    PASSWORD_BASIC,
    PASSWORD_FULL,
    PATIENT_ID,
    write_encrypted,
    write_plain,
)
from test_anonymizer_ui import wait_loaded

from mef3io_studio.explorer.edit_details import (
    COL_DESCRIPTION,
    COL_REFERENCE,
    EditDetailsDialog,
)

pytestmark = pytest.mark.usefixtures("clean_identities")

NEW_BASIC, NEW_FULL = "new-basic-77", "new-full-88"


def tree_hash(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def open_entry(qtbot, window, path, password=""):
    explorer = window.explorer
    explorer.open_paths([str(path)], interactive=False, password=password)
    wait_loaded(qtbot, explorer, len(explorer.entries()))
    entry = explorer.entries()[-1]
    explorer.select_entry(entry)
    return entry


def open_dialog(qtbot, window, entry) -> EditDetailsDialog:
    dialog = window.explorer.edit_details(entry)
    assert dialog is not None
    qtbot.addWidget(dialog)
    return dialog


def finish(qtbot, dialog, timeout=60_000):
    handle = dialog.check_and_save()
    assert handle is not None
    qtbot.waitUntil(lambda: dialog.result is not None or dialog.findings.count(), timeout=timeout)
    qtbot.waitUntil(lambda: not dialog._busy, timeout=timeout)


@pytest.fixture
def shown_window(window):
    window._closing = True
    window.show()
    return window


def test_the_button_is_in_the_patient_tab_and_needs_a_recording(shown_window, qtbot, tmp_path):
    panel = shown_window.explorer.patient
    assert panel.edit_button.text() == "Edit details (makes a new copy)…"
    assert not panel.edit_button.isEnabled()
    assert shown_window.explorer.edit_details(None) is None
    entry = open_entry(qtbot, shown_window, write_plain(tmp_path / "a.mefd"))
    assert panel.edit_button.isEnabled()
    assert entry.details is not None


def test_details_stay_hidden_until_show(shown_window, qtbot, tmp_path):
    entry = open_entry(qtbot, shown_window, write_plain(tmp_path / "a.mefd"))
    assert not entry.revealed
    dialog = open_dialog(qtbot, shown_window, entry)
    boxes = dialog.boxes
    assert all(b.text() == "" and b.initial is None for b in boxes.values())
    assert boxes["subject_name_1"].box.placeholderText() == "Hidden until you choose Show"
    everything = " ".join(b.text() for b in boxes.values()) + dialog.times.text()
    assert FIRST not in everything and PATIENT_ID not in everything
    # Leaving hidden boxes empty keeps what is stored: nothing to save.
    assert dialog.new_details() == {}
    assert "Change at least one detail" in dialog.problem.text()
    assert not dialog.save_button.isEnabled()
    # Show: the stored text appears, and the Explore list follows.
    dialog.show_button.click()
    assert entry.revealed
    assert boxes["subject_name_1"].text() == FIRST and boxes["subject_name_2"].text() == LAST
    assert boxes["subject_id"].text() == PATIENT_ID
    assert dialog.new_details() == {}
    assert not dialog.hidden_note.isVisible() and not dialog.show_button.isVisible()


def test_text_typed_while_hidden_is_kept_when_shown(shown_window, qtbot, tmp_path):
    entry = open_entry(qtbot, shown_window, write_plain(tmp_path / "a.mefd"))
    dialog = open_dialog(qtbot, shown_window, entry)
    dialog.boxes["recording_location"].set_text("Room 4")
    assert dialog.new_details() == {"recording_location": "Room 4"}
    dialog.show_button.click()
    assert dialog.boxes["recording_location"].text() == "Room 4"
    assert dialog.new_details() == {"recording_location": "Room 4"}


def test_only_changed_details_are_sent_and_empty_clears(shown_window, qtbot, tmp_path):
    entry = open_entry(qtbot, shown_window, write_plain(tmp_path / "a.mefd"))
    entry.revealed = True
    dialog = open_dialog(qtbot, shown_window, entry)
    dialog.boxes["subject_id"].set_text("")
    dialog.boxes["subject_name_1"].set_text("Alex")
    dialog.table.item(0, COL_DESCRIPTION).setText("frontal")
    dialog.table.item(1, COL_REFERENCE).setText("linked ears")
    assert dialog.new_details() == {
        "subject_id": "",
        "subject_name_1": "Alex",
        "channel_descriptions": {"a1": "frontal"},
        "reference_descriptions": {"a2": "linked ears"},
    }


def test_a_text_that_is_too_long_blocks_saving_and_is_never_cut(shown_window, qtbot, tmp_path):
    entry = open_entry(qtbot, shown_window, write_plain(tmp_path / "a.mefd"))
    entry.revealed = True
    dialog = open_dialog(qtbot, shown_window, entry)
    box = dialog.boxes["subject_id"]
    box.set_text("x" * 127)
    assert box.room.text() == "0 left" and dialog.save_button.isEnabled()
    box.set_text("x" * 130)
    assert box.room.text() == "Too long by 3" and box.too_long()
    assert "never cuts" in dialog.problem.text() and "patient ID" in dialog.problem.text()
    assert not dialog.save_button.isEnabled()
    assert box.text() == "x" * 130


def test_times_are_shown_but_cannot_be_edited(shown_window, qtbot, tmp_path):
    entry = open_entry(qtbot, shown_window, write_plain(tmp_path / "a.mefd"))
    dialog = open_dialog(qtbot, shown_window, entry)
    assert "2000-01-01" in dialog.times.text()
    assert "cannot be changed" in dialog.times_note.text()
    # No box, cell or table column for a time.
    assert [dialog.table.horizontalHeaderItem(i).text() for i in range(3)][0] == "Channel"
    assert set(dialog.boxes) == {
        "subject_name_1",
        "subject_name_2",
        "subject_id",
        "recording_location",
        "session_description",
    }


def test_a_real_copy_original_untouched_and_history_says_so(shown_window, qtbot, tmp_path):
    src = write_plain(tmp_path / "a.mefd")
    before = tree_hash(src)
    entry = open_entry(qtbot, shown_window, src)
    entry.revealed = True
    dialog = open_dialog(qtbot, shown_window, entry)
    dialog.boxes["recording_location"].set_text("Room 12")
    dialog.table.item(0, COL_DESCRIPTION).setText("frontal, left")
    dialog.name.setText("a-edited")
    dialog.folder = str(tmp_path)
    dialog._update()
    assert dialog.problem.text() == "" and dialog.save_button.isEnabled()
    finish(qtbot, dialog)
    assert dialog.result.ok, dialog.result.message
    assert "saved and checked" in dialog.result_label.text()
    assert dialog.open_button.isVisible()
    assert tree_hash(src) == before
    dst = tmp_path / "a-edited.mefd"
    assert mef3io.Reader(str(dst)).metadata.subject.recording_location == "Room 12"
    assert mef3io.Reader(str(dst)).metadata.subject.name_1 == FIRST
    # The copy opens in Explore, and its History names the change.
    opened = []
    dialog.open_requested.connect(lambda p, pw: opened.append((p, pw)))
    dialog.open_button.click()
    assert opened == [(str(dst), "")]
    shown_window.explorer.open_and_show(str(dst))
    wait_loaded(qtbot, shown_window.explorer, 2)
    copy = shown_window.explorer.current_entry()
    history, _parts = copy.details.history_groups()[0]
    assert history.last_operation == "transform"
    texts = " ".join(shown_window.explorer.history.texts)
    assert "copied with changes" in texts


def test_a_refusal_is_shown_in_words_and_nothing_is_written(shown_window, qtbot, tmp_path):
    src = write_plain(tmp_path / "a.mefd")
    entry = open_entry(qtbot, shown_window, src)
    dialog = open_dialog(qtbot, shown_window, entry)
    dialog.boxes["recording_location"].set_text("Room 12")
    dialog.name.setText("b")
    dialog.folder = str(tmp_path)
    dialog._update()
    assert dialog.problem.text() == ""
    # Taken between the form and the check.
    write_plain(tmp_path / "b.mefd")
    dialog.problem.setText("")
    request = dialog.make_request()
    from mef3io_studio.model import changes

    assert [f.code for f in changes.check_changes(request)] == ["dst-exists"]
    assert "already in the chosen folder" in dialog.problem_text()


# --------------------------------------------------------------------------
# Passwords


def test_the_passwords_choice_is_the_shared_widget(shown_window, qtbot, tmp_path):
    from mef3io_studio.anonymizer.page import PasswordsStep as AnonymizerSteps
    from mef3io_studio.common.passwords import PasswordsStep

    entry = open_entry(qtbot, shown_window, write_plain(tmp_path / "a.mefd"))
    dialog = open_dialog(qtbot, shown_window, entry)
    assert type(dialog.passwords) is PasswordsStep is AnonymizerSteps
    assert dialog.passwords.passwords() is None
    dialog.passwords.both.setChecked(True)
    dialog.boxes["recording_location"].set_text("x")
    assert "Type both passwords" in dialog.problem.text() and not dialog.save_button.isEnabled()
    dialog.passwords.basic.setText(NEW_BASIC)
    dialog.passwords.basic_again.setText(NEW_BASIC)
    dialog.passwords.full.setText(NEW_FULL)
    dialog.passwords.full_again.setText("different")
    assert "not the same" in dialog.problem.text()
    dialog.passwords.full_again.setText(NEW_FULL)
    assert dialog.passwords.passwords() == (NEW_BASIC, NEW_FULL)
    # A password other programs may not accept is warned about, not refused.
    dialog.passwords.full.setText("x" * 20)
    assert dialog.passwords.interop.text()


def test_a_protected_recording_to_a_new_pair(shown_window, qtbot, tmp_path, caplog):
    src = write_encrypted(tmp_path / "locked.mefd")
    before = tree_hash(src)
    entry = open_entry(qtbot, shown_window, src, password=PASSWORD_FULL)
    entry.revealed = True
    dialog = open_dialog(qtbot, shown_window, entry)
    dialog.passwords.both.setChecked(True)
    for field, text in (
        (dialog.passwords.basic, NEW_BASIC),
        (dialog.passwords.basic_again, NEW_BASIC),
        (dialog.passwords.full, NEW_FULL),
        (dialog.passwords.full_again, NEW_FULL),
    ):
        field.setText(text)
    dialog.name.setText("locked-new")
    dialog.folder = str(tmp_path)
    dialog._update()
    assert dialog.problem.text() == ""
    with caplog.at_level(logging.DEBUG):
        finish(qtbot, dialog)
    assert dialog.result.ok, dialog.result.message
    assert tree_hash(src) == before
    dst = tmp_path / "locked-new.mefd"
    assert mef3io.Reader(str(dst), password=NEW_FULL).metadata.subject.name_1 == FIRST
    for old in (PASSWORD_FULL, PASSWORD_BASIC):
        with pytest.raises(mef3io.PasswordError):
            mef3io.Reader(str(dst), password=old)
    secrets_ = (PASSWORD_FULL, NEW_BASIC, NEW_FULL)
    assert not any(s in caplog.text for s in secrets_)
    assert not any(s in repr(dialog.request) for s in secrets_)
    # The window keeps nothing of them once it is closed.
    dialog.reject()
    assert dialog.passwords.passwords() is None


def test_a_protected_recording_without_passwords_in_the_copy(shown_window, qtbot, tmp_path):
    src = write_encrypted(tmp_path / "locked.mefd")
    entry = open_entry(qtbot, shown_window, src, password=PASSWORD_FULL)
    entry.revealed = True
    dialog = open_dialog(qtbot, shown_window, entry)
    assert dialog.protected_note.isVisible()
    assert "NOT" in dialog.protected_note.text()
    dialog.boxes["recording_location"].set_text("Open ward")
    dialog.name.setText("unlocked")
    dialog.folder = str(tmp_path)
    dialog._update()
    finish(qtbot, dialog)
    assert dialog.result.ok, dialog.result.message
    reader = mef3io.Reader(str(tmp_path / "unlocked.mefd"))
    assert reader.metadata.subject.recording_location == "Open ward"


def test_basic_access_cannot_edit(shown_window, qtbot, tmp_path):
    src = write_encrypted(tmp_path / "locked.mefd")
    entry = open_entry(qtbot, shown_window, src, password=PASSWORD_BASIC)
    dialog = open_dialog(qtbot, shown_window, entry)
    assert dialog.access_note.isVisible() and "full-access password" in dialog.access_note.text()
    assert not dialog.save_button.isEnabled()


def test_the_window_never_imports_mef3io():
    import ast

    path = Path(__file__).resolve().parents[1] / "src/mef3io_studio/explorer/edit_details.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        assert not any(n.split(".")[0] == "mef3io" for n in names)


def fill_pair(dialog):
    dialog.passwords.both.setChecked(True)
    for field, text in (
        (dialog.passwords.basic, NEW_BASIC),
        (dialog.passwords.basic_again, NEW_BASIC),
        (dialog.passwords.full, NEW_FULL),
        (dialog.passwords.full_again, NEW_FULL),
    ):
        field.setText(text)


def test_a_lone_surrogate_is_counted_and_left_for_mef3io_to_refuse(shown_window, qtbot, tmp_path):
    from mef3io_studio.model import changes

    assert changes.remaining("\ud800", "subject_id") == 127 - 3
    entry = open_entry(qtbot, shown_window, write_plain(tmp_path / "a.mefd"))
    dialog = open_dialog(qtbot, shown_window, entry)
    dialog.boxes["subject_id"].set_text("\ud800")
    assert dialog.boxes["subject_id"].room.text()  # no exception in the slot


def test_closing_in_any_way_wipes_the_passwords_and_the_run(shown_window, qtbot, tmp_path):
    entry = open_entry(qtbot, shown_window, write_plain(tmp_path / "a.mefd"))
    dialog = open_dialog(qtbot, shown_window, entry)
    dialog.boxes["recording_location"].set_text("x")
    fill_pair(dialog)
    dialog.name.setText("w")
    dialog.folder = str(tmp_path)
    dialog._update()
    dialog.check_and_save()  # busy: checking
    assert dialog._busy
    dialog.close()
    assert dialog.passwords.passwords() is None
    assert not dialog.passwords.basic.text() and not dialog.passwords.full_again.text()
    qtbot.waitUntil(lambda: not dialog._busy, timeout=60_000)


def test_the_run_keeps_no_password_after_saving_or_closing(shown_window, qtbot, tmp_path):
    entry = open_entry(qtbot, shown_window, write_plain(tmp_path / "a.mefd"))
    dialog = open_dialog(qtbot, shown_window, entry)
    dialog.boxes["recording_location"].set_text("x")
    fill_pair(dialog)
    dialog.name.setText("w")
    dialog.folder = str(tmp_path)
    dialog._update()
    finish(qtbot, dialog)
    assert dialog.result.ok and dialog.request.output_password is None
    opened = []
    dialog.open_requested.connect(lambda p, pw: opened.append(pw))
    dialog.open_button.click()
    assert opened == [NEW_FULL]
    dialog.open_button.click()
    assert opened == [NEW_FULL, ""]
    dialog.reject()
    assert dialog.request is None and dialog.result is None and dialog.handle is None


def test_stop_during_the_copy_leaves_nothing(shown_window, qtbot, tmp_path):
    src = write_plain(tmp_path / "a.mefd")
    entry = open_entry(qtbot, shown_window, src)
    dialog = open_dialog(qtbot, shown_window, entry)
    dialog.boxes["recording_location"].set_text("x")
    dialog.name.setText("stopped")
    dialog.folder = str(tmp_path)
    dialog._update()
    dialog.check_and_save()
    qtbot.waitUntil(lambda: dialog.handle is not None, timeout=60_000)
    dialog.stop()
    qtbot.waitUntil(lambda: not dialog._busy, timeout=60_000)
    assert dialog._stopped and dialog.result is None
    assert "Stopped" in dialog.result_label.text()
    assert not (tmp_path / "stopped.mefd").exists()
    assert not (tmp_path / "stopped.mefd.partial").exists()


def test_a_check_that_fails_as_a_task_is_shown(shown_window, qtbot, tmp_path, monkeypatch):
    from mef3io_studio.explorer import edit_details

    def broken(ctx, request):
        raise RuntimeError("boom")

    monkeypatch.setattr(edit_details, "check_changes", broken)
    entry = open_entry(qtbot, shown_window, write_plain(tmp_path / "a.mefd"))
    dialog = open_dialog(qtbot, shown_window, entry)
    dialog.boxes["recording_location"].set_text("x")
    dialog.name.setText("f")
    dialog.folder = str(tmp_path)
    dialog._update()
    dialog.check_and_save()
    qtbot.waitUntil(lambda: dialog.findings.count() > 0 and not dialog._busy, timeout=30_000)
    assert dialog.result is None and dialog.save_button.isEnabled()
    assert not (tmp_path / "f.mefd").exists()
