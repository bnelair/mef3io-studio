"""Explore's "Make new recordings…", offscreen: the steps, saved channel
choices (apply, save, share), recordings that lack channels or would keep
none, the details and passwords steps, and a real batch through the shared
runner. Made-up signals only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import mef3io
import pytest
from explorer_sessions import write_plain
from PySide6.QtCore import Qt
from test_anonymizer_ui import wait_loaded

from mef3io_studio.makenew import MakeNewDialog
from mef3io_studio.makenew import dialog as dlg
from mef3io_studio.model import presets
from mef3io_studio.workers import tasks

PW = ("basic-pw-1357", "full-pw-2468")


def tree_hash(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def shown_window(window):
    window._closing = True
    window.show()
    return window


def open_all(qtbot, window, paths):
    explorer = window.explorer
    explorer.open_paths([str(p) for p in paths], interactive=False)
    wait_loaded(qtbot, explorer, len(paths))
    return explorer.entries()


def make_dialog(qtbot, window, entries, tmp_path) -> MakeNewDialog:
    dialog = MakeNewDialog(
        entries, window.jobs, store=presets.PresetStore(str(tmp_path / "store")), parent=window
    )
    qtbot.addWidget(dialog)
    return dialog


def test_the_menus_need_recordings(shown_window, qtbot, tmp_path):
    explorer = shown_window.explorer
    said = []
    explorer.status.connect(said.append)
    assert shown_window.make_new_action.text() == "&Make new recordings from the selected…"
    assert explorer.make_new_selected() is None
    assert said == ["Select recordings to make new ones from."]
    entries = open_all(qtbot, shown_window, [write_plain(tmp_path / "a.mefd")])
    explorer.select_entry(entries[0])
    dialog = explorer.make_new_selected()
    assert isinstance(dialog, MakeNewDialog)
    qtbot.addWidget(dialog)
    assert dialog.step_label.text() == "Step 1 of 6: Recordings"
    dialog.reject()


def test_the_steps_block_until_they_can_continue(shown_window, qtbot, tmp_path):
    entries = open_all(
        qtbot, shown_window, [write_plain(tmp_path / f"r{i}.mefd") for i in range(2)]
    )
    dialog = make_dialog(qtbot, shown_window, entries, tmp_path)
    assert len(dialog.ticked()) == 2 and dialog.next_button.isEnabled()
    for i in range(dialog.recording_list.count()):
        dialog.recording_list.item(i).setCheckState(Qt.CheckState.Unchecked)
    assert not dialog.next_button.isEnabled() and "Tick at least one" in dialog.problem.text()
    for i in range(dialog.recording_list.count()):
        dialog.recording_list.item(i).setCheckState(Qt.CheckState.Checked)
    dialog.next()
    assert dialog.step == dlg.STEP_CHANNELS
    dialog.next()  # nothing left out yet: fine, channels are all kept
    dialog.next()
    dialog.next()
    assert dialog.step == dlg.STEP_WHERE
    assert "Choose a folder" in dialog.problem.text()  # a folder first
    dialog.folder = str(tmp_path)
    dialog._changed()
    assert "just be copies" in dialog.problem.text()  # nothing would change
    assert not dialog.start_button.isEnabled()
    dialog.back()
    assert dialog.step == dlg.STEP_PASSWORDS
    dialog.reject()


def test_saved_choices_apply_save_share_and_report_missing(shown_window, qtbot, tmp_path):
    full = write_plain(tmp_path / "full.mefd")  # a1 a2 a3
    short = write_plain(tmp_path / "short.mefd", channels=("a1", "a2"), notes=False)
    other = write_plain(tmp_path / "other.mefd", channels=("x1", "x2"), notes=False)
    entries = open_all(qtbot, shown_window, [full, short, other])
    dialog = make_dialog(qtbot, shown_window, entries, tmp_path)
    dialog.next()
    # Save what the picker shows, leaving a3 out (the keep list is a1, a2, x1, x2).
    dialog.picker.click("a3", False)
    assert dialog.save_preset("Without a3") == ""
    assert dialog.save_preset("without A3").startswith("A saved choice with that name")
    assert dialog.save_preset("") == "Give the saved choice a name."
    assert dialog.preset_combo.currentText() == "Without a3"
    # Apply it from a clean picker: the same channels are left out.
    dialog.picker.keep_all.click()
    assert dialog.selection.excluded == ()
    assert dialog.apply_preset("Without a3") == ()
    assert dialog.selection.excluded == ("a3",)
    # A keep-list: a1 and a3. "short" lacks a3, "other" has neither: blocked.
    dialog.store.add(presets.make_preset("Keep a1 a3", ["a1", "a3"]))
    dialog._fill_presets("Keep a1 a3")
    dialog.apply_preset("Keep a1 a3")
    assert dialog.selection.kept == frozenset({"a1", "a3"})
    rows = dialog.rows()
    assert [r.kept for r in rows] == [("a1", "a3"), ("a1",), ()]
    assert rows[1].missing == ("a3",) and rows[2].empty
    table = dialog.preview
    assert table.item(1, 3).text() == "a3"  # shown, not an error
    assert "would keep no channel" in dialog.channel_problem.text()
    assert not dialog.next_button.isEnabled()
    assert "keep no channel" in dialog.problem.text()
    # Leave the empty one out of the batch: the choice is fine for the rest.
    dialog.back()
    other_item = next(
        dialog.recording_list.item(i)
        for i in range(dialog.recording_list.count())
        if dialog.recording_list.item(i).text() == "other"
    )
    other_item.setCheckState(Qt.CheckState.Unchecked)
    dialog.next()
    dialog.apply_preset("Keep a1 a3")
    assert dialog.next_button.isEnabled() and dialog.selection.recordings_left_empty() == ()
    # Share: a plain file, and add it again from there.
    path = tmp_path / "shared.json"
    assert dialog.export_presets(str(path)) == ""
    assert json.loads(path.read_text())["presets"][0]["name"] == "Without a3"
    assert dialog.import_presets(str(path)) == ""
    assert "Without a3 (2)" in dialog.store.names()
    assert "could not be read" in dialog.import_presets(str(tmp_path / "x.json"))
    path.write_text("nope")
    assert "does not hold saved channel choices" in dialog.import_presets(str(path))
    assert dialog.delete_preset("Without a3 (2)")
    dialog.reject()


def test_a_whole_batch_through_the_dialog(shown_window, qtbot, tmp_path):
    sources = [write_plain(tmp_path / f"rec{i}.mefd") for i in range(3)]
    before = [tree_hash(s) for s in sources]
    out = tmp_path / "out"
    out.mkdir()
    entries = open_all(qtbot, shown_window, sources)
    dialog = make_dialog(qtbot, shown_window, entries, tmp_path)
    dialog.store.add(presets.make_preset("Drop a3", ["a3"], exclude=True))
    dialog.next()
    dialog._fill_presets("Drop a3")
    dialog.apply_preset("Drop a3")
    dialog.next()
    assert dialog.step == dlg.STEP_DETAILS
    dialog.change_details.setChecked(True)
    dialog.boxes["recording_location"].set_text("Quiet Room 12")
    assert dialog.details() == {"recording_location": "Quiet Room 12"}
    dialog.boxes["session_description"].set_text("x" * 3000)
    assert "never cuts a text short" in dialog.problem.text()
    assert not dialog.next_button.isEnabled()
    dialog.boxes["session_description"].set_text("")
    dialog.next()
    dialog.passwords.both.setChecked(True)
    dialog.passwords.basic.setText(PW[0])
    dialog.passwords.basic_again.setText(PW[0])
    dialog.passwords.full.setText(PW[1])
    dialog.passwords.full_again.setText("different")
    assert "not the same" in dialog.problem.text()
    dialog.passwords.full_again.setText(PW[1])
    dialog.next()
    assert dialog.step == dlg.STEP_WHERE
    dialog.naming.setCurrentIndex(1)  # a new name and a number
    dialog.naming_text.setText("Study-")
    assert dialog.new_names() == ["Study-001", "Study-002", "Study-003"]
    assert dialog.names_preview.item(0).text() == "1. Study-001"
    dialog.folder = str(out)
    dialog._changed()
    assert dialog.problem.text() == "" and dialog.start_button.isEnabled()
    handle = dialog.start()
    assert handle is not None
    qtbot.waitUntil(lambda: dialog.finished_run, timeout=180_000)
    assert [dialog.results_table.item(n, 1).text() for n in range(3)] == ["Saved and checked"] * 3
    assert dialog.summary.text() == "Finished: 3 of 3 new recordings saved and checked."
    assert sorted(p.name for p in out.iterdir()) == [f"Study-00{i}.mefd" for i in (1, 2, 3)]
    for i in (1, 2, 3):
        reader = mef3io.Reader(str(out / f"Study-00{i}.mefd"), password=PW[1])
        assert sorted(reader.channels) == ["a1", "a2"]
        assert reader.metadata.subject.recording_location == "Quiet Room 12"
    assert [tree_hash(s) for s in sources] == before
    # No password is kept: not in the form, not in what the window holds.
    assert not dialog.passwords.both.isChecked() and dialog.passwords.full.text() == ""
    assert all(i.request.output_password is None for i in dialog.batch.items)
    assert all(i.request.password == "" for i in dialog.batch.items)
    assert not dialog.open_button.isHidden()
    opened = []
    dialog.open_requested.connect(lambda paths, pw: opened.append((paths, pw)))
    dialog.open_button.click()
    assert len(opened[0][0]) == 3 and opened[0][1] == PW[1]
    dialog.reject()


def test_a_taken_name_is_found_before_anything_runs(shown_window, qtbot, tmp_path):
    sources = [write_plain(tmp_path / f"rec{i}.mefd") for i in range(2)]
    out = tmp_path / "out"
    out.mkdir()
    entries = open_all(qtbot, shown_window, sources)
    dialog = make_dialog(qtbot, shown_window, entries, tmp_path)
    dialog.next()
    dialog.picker.click("a3", False)
    dialog.next()
    dialog.next()
    dialog.next()
    dialog.folder = str(out)
    (out / "rec1-new.mefd").mkdir()
    dialog._changed()
    assert (
        "Recording 2 of 2" in dialog.problem.text()
        and "already in the chosen folder" in dialog.problem.text()
    )
    assert not dialog.start_button.isEnabled()
    (out / "rec1-new.mefd").rmdir()
    dialog._changed()
    assert dialog.problem.text() == ""
    dialog.reject()


def test_an_interrupted_batch_of_new_recordings_continues_in_this_window(
    shown_window, qtbot, tmp_path
):
    """The startup offer routes a journal of copies with changes here (not
    to the anonymizer): the finished one is skipped, the rest is made."""
    from test_make_new_model import change_batch

    from mef3io_studio.model import journal, pipeline

    sources = [write_plain(tmp_path / f"rec{i}.mefd") for i in range(2)]
    batch, dest, _work = change_batch(tmp_path, sources, output_password=PW)
    # The first one is really made (with the same choices) before the interruption.
    first = shown_window.jobs.submit(
        "first", tasks.run_batch, replace(batch, items=batch.items[:1]), None
    )
    with qtbot.waitSignal(first.finished, timeout=120_000):
        pass
    assert (dest / "New1.mefd").is_dir()
    pending = journal.Journal.new(2, pipeline.to_payload(batch))
    pending.mark(0, journal.DONE)
    try:
        # Passwords are asked for again; the test answers like the user would.
        shown_window.ask_resume_passwords = lambda sources_, total, protected, names=None: (
            {},
            PW,
        )
        dialog_handle = shown_window.continue_batch(pending)
        assert dialog_handle is not None
        dialog = shown_window.explorer._dialogs[-1]
        assert isinstance(dialog, MakeNewDialog)
        qtbot.waitUntil(lambda: dialog.finished_run, timeout=180_000)
        assert [dialog.results_table.item(n, 1).text() for n in range(2)] == [
            "Saved and checked (earlier)",
            "Saved and checked",
        ]
        assert sorted(p.name for p in dest.iterdir()) == ["New1.mefd", "New2.mefd"]
        assert sorted(mef3io.Reader(str(dest / "New2.mefd"), password=PW[1]).channels) == [
            "a1",
            "a2",
        ]
        assert journal.unfinished() == []
    finally:
        pending.delete()


def test_shared_files_that_are_not_text_or_too_big_are_refused(shown_window, qtbot, tmp_path):
    entries = open_all(qtbot, shown_window, [write_plain(tmp_path / "a.mefd")])
    dialog = make_dialog(qtbot, shown_window, entries, tmp_path)
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe\x00\x80 not utf-8 \xc3\x28")
    assert "does not hold saved channel choices" in dialog.import_presets(str(binary))
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (presets.MAX_FILE_BYTES + 1))
    assert "too large" in dialog.import_presets(str(big))
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 300_000, encoding="utf-8")
    assert "does not hold" in dialog.import_presets(str(deep))
    assert dialog.store.names() == []
    dialog.reject()


def test_an_unavailable_key_is_said_plainly_everywhere(shown_window, qtbot, tmp_path, monkeypatch):
    from mef3io_studio.model import journal

    store = presets.PresetStore(str(tmp_path / "store"))
    store.add(presets.make_preset("Mine", ["a1"]))
    before = Path(store.path).read_bytes()

    def no_key():
        raise journal.KeyUnavailable("no key")

    monkeypatch.setattr(journal, "_key", no_key)
    entries = open_all(qtbot, shown_window, [write_plain(tmp_path / "a.mefd")])
    dialog = MakeNewDialog(entries, shown_window.jobs, store=store, parent=shown_window)
    qtbot.addWidget(dialog)  # (the constructor read the store: no crash)
    assert dialog.key_problem and journal.KEY_PROBLEM in dialog.preset_note.text()
    dialog.next()
    assert dialog.save_preset("X") == journal.KEY_PROBLEM
    assert dialog.apply_preset("Mine") == ()
    assert dialog.delete_preset("Mine") is False
    assert dialog.export_presets(str(tmp_path / "e.json")) == journal.KEY_PROBLEM
    path = tmp_path / "in.json"
    path.write_text(presets.export_text([presets.make_preset("S", ["a"])]), encoding="utf-8")
    assert dialog.import_presets(str(path)) == journal.KEY_PROBLEM
    assert Path(store.path).read_bytes() == before  # nothing replaced
    dialog.reject()


def test_the_missing_column_follows_the_choice_on_screen(shown_window, qtbot, tmp_path):
    entries = open_all(
        qtbot,
        shown_window,
        [
            write_plain(tmp_path / "full.mefd"),
            write_plain(tmp_path / "short.mefd", channels=("a1", "a2"), notes=False),
        ],
    )
    dialog = make_dialog(qtbot, shown_window, entries, tmp_path)
    dialog.store.add(presets.make_preset("Up", ["A1", "A3"]))
    dialog._fill_presets("Up")
    dialog.next()
    dialog.apply_preset("Up")  # exact: no channel matches
    assert dialog.applied is not None and dialog.rows()[0].kept == ()
    dialog.ignore_case.setChecked(True)  # re-applied
    assert dialog.rows()[0].kept == ("a1", "a3") and dialog.rows()[1].missing == ("A3",)
    assert dialog.preview.item(1, 3).text() == "A3"
    dialog.picker.click("a1", False)  # a hand edit: no longer "that saved choice"
    assert dialog.applied is None and dialog.rows()[1].missing == ()
    assert dialog.preview.item(1, 3).text() == ""
    dialog.reject()
