"""The anonymizer wizard, offscreen (mef3io-studio#5): every step, the
256-channel picker, the early name check, a batch with one failing copy,
Stop, the result page and the date key.

The app is pinned to mef3io 1.4.0, whose ``anonymize_session`` and
``anonymize.check`` every test can rely on.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import mef3io
import numpy as np
import pytest
from explorer_sessions import (
    LAST,
    PATIENT_ID,
    SECOND,
    T0,
    metadata,
    write_plain,
)
from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QApplication

from mef3io_studio.anonymizer.page import (
    STEP_CHANNELS,
    STEP_DATES,
    STEP_IDENTITY,
    STEP_OUTPUT,
    STEP_PASSWORDS,
    STEP_RECORDINGS,
    STEP_REVIEW,
    STEP_RUN,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "mef3io_studio"


def wait_loaded(qtbot, explorer, n, timeout=60_000):
    qtbot.waitUntil(
        lambda: len(explorer.entries()) == n and all(not e.busy for e in explorer.entries()),
        timeout=timeout,
    )


def damage_signal(path: Path) -> Path:
    """Flip a byte inside the first block's compressed data: the details
    and the index stay intact, so only reading the signal finds it."""
    tdat = sorted(Path(path).glob("*.timd/*.segd/*.tdat"))[0]
    raw = bytearray(tdat.read_bytes())
    raw[1024 + 304 + 10] ^= 0xFF
    tdat.write_bytes(bytes(raw))
    return path


def write_many_channels(path: Path, channels: int = 256, start: int = T0) -> Path:
    """Electrode-style names: 16 groups of 16 contacts (LA1…LA16, RA1…)."""
    prefixes = [f"{side}{x}" for side in "LR" for x in "ABCDEFGH"]
    names = [f"{p}{i}" for p in prefixes for i in range(1, 17)][:channels]
    with mef3io.Writer(str(path), overwrite=True, metadata=metadata()) as w:
        for name in names:
            w.write(name, np.zeros(256), start, 256.0)
    return path


@pytest.fixture
def window_page(window):
    window._closing = True
    window.show()
    return window, window.anonymizer


def open_in_explorer(qtbot, window, paths):
    window.explorer.open_paths([str(p) for p in paths], interactive=False)
    wait_loaded(qtbot, window.explorer, len(paths))


def go_to_channels(page, keys=None):
    page.go(STEP_RECORDINGS)
    page.recordings.set_ticked(keys if keys is not None else [e.key for e in page.recordings.rows])
    page.next()
    assert page.step == STEP_CHANNELS


# --------------------------------------------------------------------------
# Structure


def test_the_wizard_replaces_the_placeholder_and_never_imports_mef3io(window):
    from mef3io_studio.anonymizer import AnonymizerPage

    index = window.module_keys.index("anonymizer")
    assert isinstance(window.page_widgets[index], AnonymizerPage)
    offenders = []
    for path in (SRC / "anonymizer").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n.split(".")[0] == "mef3io" for n in names):
                offenders.append(str(path.relative_to(SRC)))
    assert not offenders, offenders


def test_nothing_is_written_before_the_last_step(window_page, qtbot, tmp_path):
    window, page = window_page
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    out = tmp_path / "out"
    out.mkdir()
    before = sorted(p.name for p in tmp_path.rglob("*"))
    go_to_channels(page)
    page.next()  # dates
    page.dates.keep_utc.setChecked(True)
    # The default new date (2000-01-01) is this synthetic recording's own date.
    assert page.step == STEP_DATES and not page.dates.ok
    page.dates.new_date.setDate(QDate(2010, 3, 1))
    assert page.dates.ok
    page.next()  # identity
    assert page.step == STEP_IDENTITY
    page.back()
    page.back()
    page.back()
    assert page.step == STEP_RECORDINGS
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


# --------------------------------------------------------------------------
# Step 1: recordings


def test_recordings_that_cannot_be_anonymized_cannot_be_ticked(window_page, qtbot, tmp_path):
    from explorer_sessions import PASSWORD_BASIC, write_encrypted

    window, page = window_page
    good = write_plain(tmp_path / "good.mefd", notes=False)
    locked = write_encrypted(tmp_path / "locked.mefd")
    window.explorer.open_paths([str(good)], interactive=False)
    window.explorer.open_paths([str(locked)], interactive=False, password=PASSWORD_BASIC)
    wait_loaded(qtbot, window.explorer, 2)
    page.go(STEP_RECORDINGS)
    page.recordings.tick_all()
    names = [e.name for e in page.recordings.chosen()]
    assert names == ["good"]
    reasons = {
        page.recordings.model.text(r, 0): page.recordings.model.text(r, 3)
        for r in range(page.recordings.model.rowCount())
    }
    assert reasons["good"] == "Yes"
    assert "full-access password" in reasons["locked"]
    assert page.next_button.isEnabled()
    page.recordings.untick_all()
    assert not page.next_button.isEnabled()
    assert "Tick at least one" in page.why_not.text()


# --------------------------------------------------------------------------
# Step 2: the 256-channel picker


@pytest.fixture
def picker_256(window_page, qtbot, tmp_path):
    window, page = window_page
    src = write_many_channels(tmp_path / "big.mefd")
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    return page


def test_picker_keeps_every_channel_by_default_and_groups_them(picker_256):
    page = picker_256
    picker = page.picker
    assert picker.selection.total == 256
    assert picker.count_text() == "Keeping 256 of 256 channels"
    assert picker.model.rowCount() == 16  # 16 electrodes, folded
    first = picker.model.index(0, 0)
    assert picker.model.data(first) == "LA (16)"
    assert picker.model.data(first, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    assert not picker.tree.isExpanded(first)  # long lists start folded
    # Natural order inside a group: LA2 before LA10.
    names = [picker.model.data(picker.model.index(r, 0, first)) for r in range(16)]
    assert names[:3] == ["LA1", "LA2", "LA3"] and names[9] == "LA10"


def test_picker_ranges_with_shift_click(picker_256):
    picker = picker_256.picker
    picker.tree.expandAll()
    picker.click("LA3", keep=False)
    picker.click("LA9", keep=False, shift=True)
    assert picker.selection.excluded == tuple(f"LA{i}" for i in range(3, 10))
    group = picker.model.index(0, 0)
    state = picker.model.data(group, Qt.ItemDataRole.CheckStateRole)
    assert state == Qt.CheckState.PartiallyChecked  # tri-state group
    assert picker.count_text() == "Keeping 249 of 256 channels"
    # A plain click toggles one channel; through the model, as the view does.
    index = picker.model.index(0, 0, group)  # LA1
    picker.model.setData(index, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
    assert not picker.selection.is_kept("LA1")


def test_picker_group_boxes_and_space_on_highlighted_rows(picker_256):
    picker = picker_256.picker
    group = picker.model.index(1, 0)  # LB
    picker.model.setData(group, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
    assert all(not picker.selection.is_kept(f"LB{i}") for i in range(1, 17))
    picker.model.setData(group, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
    assert all(picker.selection.is_kept(f"LB{i}") for i in range(1, 17))
    # Highlight two rows (Ctrl+click), then Space.
    picker.tree.expandAll()
    sel = picker.tree.selectionModel()
    from PySide6.QtCore import QItemSelectionModel

    for row in (0, 5):
        sel.select(
            picker.model.index(row, 0, picker.model.index(2, 0)),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
        )
    picker.toggle_highlighted()
    assert picker.selection.excluded == ("LC1", "LC6")
    picker.toggle_highlighted()
    assert picker.selection.excluded == ()


def test_picker_filter_and_bulk_actions_apply_to_what_is_shown(picker_256):
    picker = picker_256.picker
    picker.filter.setText("RA")
    assert picker.exclude_shown.text() == "Exclude 16 shown"
    assert picker.exclude_shown.isVisible() or not picker.isVisible()
    # Only the RA group is shown.
    shown = [
        picker.model.data(picker.model.index(r, 0))
        for r in range(picker.model.rowCount())
        if not picker.tree.isRowHidden(r, picker.model.index(r, 0).parent())
    ]
    assert shown == ["RA (16)"]
    picker.exclude_shown.click()
    assert picker.count_text() == "Keeping 240 of 256 channels"
    assert set(picker.selection.excluded) == {f"RA{i}" for i in range(1, 17)}
    picker.filter.setText("RA1")  # RA1, RA10…RA16
    assert picker.keep_shown.text() == "Keep 8 shown"
    picker.keep_shown.click()
    assert picker.count_text() == "Keeping 248 of 256 channels"
    picker.filter.clear()
    picker.invert.click()
    assert picker.count_text() == "Keeping 8 of 256 channels"
    picker.exclude_all.click()
    assert picker.count_text() == "Keeping 0 of 256 channels"
    assert not picker_256.next_button.isEnabled()  # Continue disabled at 0
    assert picker.empty_label.text()
    picker.keep_all.click()
    assert picker_256.next_button.isEnabled()


def test_picker_undo_and_redo(picker_256):
    picker = picker_256.picker
    picker.click("LA1", keep=False)
    picker.exclude_all.click()
    picker.undo()
    assert picker.selection.excluded == ("LA1",)
    picker.undo()
    assert picker.selection.excluded == ()
    assert not picker.undo_button.isEnabled()
    picker.redo()
    assert picker.selection.excluded == ("LA1",)
    picker.redo()
    assert picker.count_text() == "Keeping 0 of 256 channels"
    assert not picker.redo_button.isEnabled()


def test_picker_stays_responsive_with_256_channels(picker_256):
    """Every bulk action, a filter and an undo on 256 channels: each well
    under a frame budget on the GUI thread."""
    picker = picker_256.picker
    actions = [
        lambda: picker.exclude_all.click(),
        lambda: picker.keep_all.click(),
        lambda: picker.invert.click(),
        lambda: picker.filter.setText("L"),
        lambda: picker.exclude_shown.click(),
        lambda: picker.filter.clear(),
        lambda: picker.undo(),
        lambda: picker.tree.expandAll(),
        lambda: picker.click("RH16", keep=False),
        lambda: picker.click("LA1", keep=False, shift=True),
    ]
    worst = 0.0
    app = QApplication.instance()
    for action in actions:
        t0 = time.perf_counter()
        action()
        app.processEvents()  # the repaint counts too
        worst = max(worst, time.perf_counter() - t0)
    assert worst < 0.25, f"slowest picker action took {worst:.3f} s"


def test_batch_selection_is_by_name_and_flags_missing_names(window_page, qtbot, tmp_path):
    window, page = window_page
    a = write_plain(tmp_path / "a.mefd", channels=("x1", "x2", "ecg"), notes=False)
    b = write_plain(tmp_path / "b.mefd", channels=("x1", "x2"), notes=False)
    open_in_explorer(qtbot, window, [a, b])
    go_to_channels(page)
    picker = page.picker
    assert picker.selection.total == 3
    assert picker.missing_label.isVisibleTo(picker)
    assert "ecg" in picker.missing_label.text()
    picker.click("ecg", keep=False)
    keys = {c.entry.name: c.key for c in page.choices}
    assert page.selection.excluded_in(keys["a"]) == ("ecg",)
    assert page.selection.excluded_in(keys["b"]) == ()
    # "In recordings" column shows the flag.
    group = picker.model.index(0, 0)
    texts = {
        picker.model.data(picker.model.index(r, 0, group)): picker.model.data(
            picker.model.index(r, 3, group)
        )
        for r in range(picker.model.rowCount(group))
    }
    other = picker.model.index(picker.model.rowCount() - 1, 0)
    texts.update(
        {
            picker.model.data(picker.model.index(r, 0, other)): picker.model.data(
                picker.model.index(r, 3, other)
            )
            for r in range(picker.model.rowCount(other))
        }
    )
    assert texts["ecg"] == "⚠ 1 of 2" and texts["x1"] == "2 of 2"
    # Leaving out every channel one recording has is refused.
    picker.click("x1", keep=False)
    picker.click("x2", keep=False)
    assert not page.next_button.isEnabled()


# --------------------------------------------------------------------------
# Step 3: dates


def test_dates_step_plans_and_refuses_in_plain_words(window_page, qtbot, tmp_path):
    window, page = window_page
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    page.next()
    dates = page.dates
    dates.new_date.setDate(QDate(2003, 6, 1))
    dates.zone.setCurrentText("")
    dates.keep_local.setChecked(True)
    assert not dates.ok
    assert "time zone" in dates.refusals.text()
    dates.zone.setCurrentText("Europe/Prague")
    assert dates.ok
    dates.new_date.setDate(QDate(2000, 1, 1))  # the original's own date: too close
    dates.keep_utc.setChecked(True)
    assert not dates.ok and "too close" in dates.refusals.text()
    dates.new_date.setDate(QDate(2003, 6, 1))
    assert dates.ok
    assert dates.preview.item(0, 2).text().startswith("2003-06-01 00:00:00")


# --------------------------------------------------------------------------
# The run


def _through_to_review(
    page,
    qtbot,
    folder,
    names=None,
    keep_text=False,
    passwords=None,
    output="folders",
    bundle_name="batch.tar",
):
    page.next()  # channels -> dates
    page.dates.keep_utc.setChecked(True)
    page.dates.new_date.setDate(QDate(2010, 3, 1))
    assert page.dates.ok, page.dates.refusals.text()
    page.next()  # -> identity
    if names:
        for row, name in enumerate(names):
            page.identity.table.item(row, 1).setText(name)
    if keep_text:
        page.identity.text_keep.setChecked(True)
    page.next()  # names are checked by mef3io in the background
    qtbot.waitUntil(lambda: page.names_handle.is_finished, timeout=60_000)
    qtbot.waitUntil(lambda: page.step == STEP_PASSWORDS, timeout=5_000)
    if passwords:
        page.passwords.both.setChecked(True)
        page.passwords.basic.setText(passwords[0])
        page.passwords.basic_again.setText(passwords[0])
        page.passwords.full.setText(passwords[1])
        page.passwords.full_again.setText(passwords[1])
    page.next()  # -> output
    assert page.step == STEP_OUTPUT
    if output == "archives":
        page.output.archives.setChecked(True)
    elif output == "bundle":
        page.output.bundle.setChecked(True)
        page.output.bundle_name.setText(bundle_name)
    page.next()  # -> review
    assert page.step == STEP_REVIEW
    page.review.set_folder(str(folder))
    assert page.review.ok, page.review.blocked.text()


def start_and_wait(qtbot, page, timeout=120_000):
    """Make the copies: the up-front checks, then the whole batch."""
    checks = page.start()
    assert checks is not None
    qtbot.waitUntil(lambda: checks.is_finished, timeout=60_000)
    assert not page.preflight_problems, page.preflight_problems
    qtbot.waitUntil(lambda: page.run.finished, timeout=timeout)


def test_a_name_refusal_is_shown_on_the_identity_step(window_page, qtbot, tmp_path):
    window, page = window_page
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    page.next()
    page.dates.keep_utc.setChecked(True)
    page.dates.new_date.setDate(QDate(2010, 3, 1))
    page.next()
    page.identity.table.item(0, 1).setText(f"{LAST}-copy")
    page.next()
    qtbot.waitUntil(lambda: page.names_handle.is_finished, timeout=60_000)
    assert page.step == STEP_IDENTITY  # held back, nothing written
    text = page.identity.problems.text()
    assert "Choose another name for the copy" in text and "identifies the patient" in text
    assert not list(tmp_path.glob(f"{LAST}*"))
    # A pseudonym with the patient's ID is refused the same way.
    page.identity.table.item(0, 1).setText("S-001")
    page.identity.table.item(0, 2).setText(PATIENT_ID)
    page.next()
    qtbot.waitUntil(lambda: page.names_handle.is_finished, timeout=60_000)
    assert page.step == STEP_IDENTITY
    assert "Choose another pseudonym" in page.identity.problems.text()
    # Local problems need no helper at all.
    page.identity.table.item(0, 1).setText("a/b")
    assert "cannot contain" in page.identity.problems.text()
    assert not page.next_button.isEnabled()


def test_end_to_end_copy_result_and_privacy_check(window_page, qtbot, tmp_path, monkeypatch):
    window, page = window_page
    src = write_plain(tmp_path / "orig.mefd")
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    page.picker.click("a3", keep=False)
    out = tmp_path / "out"
    out.mkdir()
    _through_to_review(page, qtbot, out, names=["S001"])
    text = page.review.texts[page.choices[0].key]
    assert "Left out: a3" in text and "Channels kept: 2 of 3" in text
    assert "2000-01-01 00:00:00" in text and "2010-03-01 00:00:00" in text
    checks = page.next()  # Make the copies: checks first, then the batch
    qtbot.waitUntil(lambda: checks.is_finished, timeout=60_000)
    assert page.step == STEP_RUN
    qtbot.waitUntil(lambda: page.run.finished, timeout=120_000)
    assert page.run_step.result_texts[page.choices[0].key] == "Saved and checked"
    copy = out / "S001.mefd"
    assert copy.is_dir() and not (out / "S001.mefd.partial").exists()
    summary = mef3io.inspect(str(copy))
    assert [c.name for c in summary.channels] == ["a1", "a2"]
    # The result table: original -> new dates, shift in words.
    row = [page.run_step.table.item(0, c).text() for c in range(8)]
    assert row[2].startswith("2000-01-01") and row[3].startswith("2010-03-01")
    assert row[6].startswith("Moved 3,712 days later")
    assert page.run_step.key_box.isVisibleTo(page.run_step)

    # "Open the copy" shows it in Explore, on its Privacy check.
    page.open_copy_of(page.choices[0])
    explorer = window.explorer
    assert window.pages.currentWidget() is explorer
    qtbot.waitUntil(
        lambda: (
            explorer.current_entry() is not None
            and explorer.current_entry().details is not None
            and explorer.current_entry().name == "S001"
        ),
        timeout=60_000,
    )
    assert explorer.tabs.currentWidget() is explorer.privacy
    privacy = explorer.privacy
    assert privacy.headline.text() == "No patient details found in this recording"
    assert privacy.origin_text == "Made by the anonymizer."
    assert privacy.dates_note.text() == "These are the moved dates, not the real ones."
    assert "2010-03-01" in privacy.dates.text()
    assert all(symbol != "✗" for symbol, _ in privacy.rows.values()), privacy.rows


def test_a_batch_goes_on_after_one_copy_fails(window_page, qtbot, tmp_path):
    window, page = window_page
    a = write_plain(tmp_path / "a.mefd", notes=False)
    b = write_plain(tmp_path / "b.mefd", notes=False)
    open_in_explorer(qtbot, window, [a, b])
    go_to_channels(page)
    out = tmp_path / "out"
    out.mkdir()
    _through_to_review(page, qtbot, out, names=["C1", "C2"])
    # The first one's signal is damaged where only copying reads it (the
    # up-front checks read the details only): it fails during the batch.
    damage_signal(a)
    start_and_wait(qtbot, page)
    results = page.run_step.result_texts
    names = {c.name: c.key for c in page.choices}
    assert results[names["C1"]] == "Part of the recording is damaged"
    assert results[names["C2"]] == "Saved and checked"
    assert (out / "C2.mefd").is_dir()
    assert not any(p.name.endswith(".partial") for p in out.iterdir())


def test_stop_leaves_nothing_behind(window_page, qtbot, tmp_path):
    from explorer_sessions import write_large

    window, page = window_page
    src = write_large(tmp_path / "large.mefd", channels=2, parts=2, seconds=600)
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    out = tmp_path / "out"
    out.mkdir()
    _through_to_review(page, qtbot, out, names=["BIG1"])
    checks = page.start()
    qtbot.waitUntil(lambda: checks.is_finished and page.handle is not None, timeout=60_000)
    qtbot.waitUntil(
        lambda: (
            page.handle.record.progress is not None
            and (page.handle.record.progress.fraction or 0) > 0
        ),
        timeout=60_000,
    )
    page.stop()
    qtbot.waitUntil(lambda: page.run.finished, timeout=60_000)
    assert page.run.stopped
    assert list(out.iterdir()) == []
    assert "Stopped" in page.run_step.heading.text()


def test_date_key_is_written_only_on_request(window_page, qtbot, tmp_path, monkeypatch):
    window, page = window_page
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    out = tmp_path / "out"
    out.mkdir()
    _through_to_review(page, qtbot, out, names=["K1"])
    start_and_wait(qtbot, page)
    key = tmp_path / "keys" / "key.csv"
    key.parent.mkdir()
    asked = []
    monkeypatch.setattr(page, "choose_date_key_path", lambda: asked.append(1) or str(key))
    # Declined at the warning: no file, and the path is not even asked for.
    monkeypatch.setattr(page, "confirm_date_key", lambda: False)
    assert page.save_date_key() is False
    assert not key.exists() and not asked
    monkeypatch.setattr(page, "confirm_date_key", lambda: True)
    # Next to the copies: refused, with a plain warning, nothing written.
    beside = out / "key.csv"
    warned = []
    monkeypatch.setattr(
        "mef3io_studio.anonymizer.page.QMessageBox.warning", lambda *a: warned.append(a[1])
    )
    monkeypatch.setattr(page, "choose_date_key_path", lambda: str(beside))
    assert page.save_date_key() is False and not beside.exists()
    assert warned == ["Choose another place for the date key"]
    monkeypatch.setattr(page, "choose_date_key_path", lambda: asked.append(1) or str(key))
    assert page.save_date_key() is True
    text = key.read_text(encoding="utf-8")
    assert "orig" in text and "K1" in text
    # The original dates came from mef3io's report (source_start_us), which
    # crossed from the helper process with them on purpose.
    (times,) = page._original_times.values()
    assert times[0] == 946_684_800_000_000
    assert "2000-01-01T00:00:00" in text and "2010-03-01T00:00:00" in text
    assert list(key.parent.iterdir()) == [key]  # no temporary file left
    # Nothing of it anywhere else: not in the copies, not in the settings.
    for path in out.rglob("*"):
        if path.is_file():
            assert b"orig" not in path.read_bytes()
    window.save_preferences()
    settings_file = Path(window.settings._qs.fileName())
    settings_text = settings_file.read_text(encoding="utf-8", errors="replace")
    assert str(key) not in settings_text and "orig" not in settings_text


def test_password_protected_copy_opens_with_the_new_password(window_page, qtbot, tmp_path):
    window, page = window_page
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    out = tmp_path / "out"
    out.mkdir()
    _through_to_review(page, qtbot, out, names=["P1"], passwords=("bas-1", "full-2"))
    start_and_wait(qtbot, page)
    copy = str(out / "P1.mefd")
    summary = mef3io.inspect(copy, "full-2")
    assert summary.encryption["encrypted"] and summary.encryption["access"] == "full"
    # The form forgot the passwords once the copies started.
    assert page.passwords.basic.text() == "" and page.passwords.none.isChecked()
    # Only what "Open the copy" needs stays in memory: the full-access password.
    assert all(r.output_password is None and r.password == "" for r in page.run.requests)
    opened = []
    page.open_copy.connect(lambda path, pw: opened.append(pw))
    page.open_copy_of(page.choices[0])
    assert opened == ["full-2"]


def test_passwords_step_rules(window_page):
    _window, page = window_page
    step = page.passwords
    assert step.problem_text() == "" and step.passwords() is None
    step.both.setChecked(True)
    assert "Type both" in step.problem_text()
    step.basic.setText("a")
    step.basic_again.setText("b")
    step.full.setText("c")
    step.full_again.setText("c")
    assert "basic passwords are not the same" in step.problem_text()
    step.basic_again.setText("a")
    step.full.setText("a")
    step.full_again.setText("a")
    assert "must be different" in step.problem_text()
    step.full.setText("c")
    step.full_again.setText("c")
    assert step.problem_text() == "" and step.passwords() == ("a", "c")


def test_identity_and_review_wording_is_plain():
    """The fixed texts of the wizard name the risks plainly."""
    from mef3io_studio.anonymizer import page as p

    assert "NOT" in p.KEEP_TEXT_WARNING and "dates" in p.KEEP_TEXT_WARNING
    assert "undo the anonymization" in p.DATE_KEY_WARNING
    assert "do not hide the signal" in p.PASSWORD_EXPLANATION


# --------------------------------------------------------------------------
# Review round 1


def test_shift_ranges_skip_folded_groups_and_say_how_many_changed(picker_256):
    picker = picker_256.picker
    model = picker.model
    picker.tree.collapseAll()
    picker.tree.expand(model.index(0, 0))  # LA open
    picker.tree.expand(model.index(2, 0))  # LC open; LB folded between them
    picker.click("LA15", keep=False)
    picker.click("LC2", keep=False, shift=True)
    assert picker.selection.excluded == ("LA15", "LA16", "LC1", "LC2")
    assert all(picker.selection.is_kept(f"LB{i}") for i in range(1, 17))
    assert picker.changed_label.text() == "3 channels changed. Undo takes it back."
    picker.exclude_all.click()
    assert picker.changed_label.text().startswith("252 channels changed")


def test_a_group_box_acts_on_shown_channels_only_while_filtering(picker_256):
    picker = picker_256.picker
    picker.filter.setText("LA1")  # LA1, LA10..LA16
    picker.model.setData(
        picker.model.index(0, 0), Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole
    )
    assert set(picker.selection.excluded) == {"LA1", *(f"LA1{i}" for i in range(7))}
    assert picker.selection.is_kept("LA2")
    picker.model.setData(
        picker.model.index(0, 0), Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole
    )
    assert picker.selection.excluded == ()


def test_changing_the_batch_carries_the_choice_over_by_name(window_page, qtbot, tmp_path):
    window, page = window_page
    a = write_plain(tmp_path / "a.mefd", channels=("x1", "x2", "ecg"), notes=False)
    b = write_plain(tmp_path / "b.mefd", channels=("x1", "x2"), notes=False)
    open_in_explorer(qtbot, window, [a, b])
    go_to_channels(page)
    page.picker.click("x2", keep=False)
    page.picker.click("ecg", keep=False)
    page.back()
    keys = {e.name: e.key for e in page.recordings.rows}
    page.recordings.set_ticked([keys["a"]])
    page.next()
    assert page.step == STEP_CHANNELS
    assert set(page.selection.excluded) == {"x2", "ecg"}
    notice = page.picker.notice_label.text()
    assert "recordings changed" in notice and "2 channels" in notice
    assert "Undo starts again" in notice
    assert not page.selection.can_undo


def test_the_recordings_step_stays_quick_with_2000_recordings(window_page, qtbot, monkeypatch):
    """The Explore list changing while the step is hidden costs nothing; one
    coalesced rebuild when it is shown, quickly."""
    from mef3io_studio.explorer.state import RecordingEntry
    from mef3io_studio.model.recording import ChannelInfo, InspectOutcome, Part, RecordingDetails

    window, page = window_page
    entries = []
    for i in range(2000):
        loc = f"/nowhere/r{i:04d}.mefd"
        part = Part(0, T0 + i, T0 + i + 10 * SECOND, 10, 1)
        channel = ChannelInfo("c", 256.0, "uV", 10, T0 + i, T0 + i + 10 * SECOND, (part,))
        details = RecordingDetails(location=loc, channels=(channel,))
        entries.append(RecordingEntry(location=loc, outcome=InspectOutcome(loc, details=details)))
    rebuilds = []
    real = page.recordings.refresh
    monkeypatch.setattr(page.recordings, "refresh", lambda: rebuilds.append(1) or real())
    window.module_list.setCurrentRow(window.module_keys.index("explorer"))
    for chunk in range(0, 2000, 200):  # a scan delivers recordings in bursts
        window.explorer.model.add(entries[chunk : chunk + 200])
    qtbot.wait(300)
    assert rebuilds == []  # hidden: only marked out of date
    window.module_list.setCurrentRow(window.module_keys.index("anonymizer"))
    t0 = time.perf_counter()
    qtbot.waitUntil(lambda: page.recordings.model.rowCount() == 2000, timeout=5_000)
    assert len(rebuilds) == 1
    page.recordings._reasons.clear()
    t0 = time.perf_counter()
    page._recordings_dirty = True
    page._refresh_recordings()
    QApplication.instance().processEvents()
    assert time.perf_counter() - t0 < 0.5
    t0 = time.perf_counter()
    page.recordings.tick_all()  # works out every reason once, then cached
    assert len(page.recordings.ticked) == 2000
    assert time.perf_counter() - t0 < 3.0


def test_passwords_other_programs_may_not_accept_are_warned(window_page):
    _window, page = window_page
    step = page.passwords
    step.both.setChecked(True)
    for field_, value in ((step.basic, "ok-basic"), (step.full, "ok-full")):
        field_.setText(value)
    assert step.interop_text() == ""
    step.full.setText("a-much-longer-password")
    assert "Other programs may not open the copies" in step.interop_text()
    assert step.interop.isVisibleTo(step)
    step.full.setText("pass€")
    assert step.interop_text()


def test_passwords_are_cleared_when_the_page_is_left(window_page, qtbot, tmp_path):
    window, page = window_page
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    window.module_list.setCurrentRow(window.module_keys.index("anonymizer"))
    go_to_channels(page)
    page.next()
    page.dates.keep_utc.setChecked(True)
    page.dates.new_date.setDate(QDate(2010, 3, 1))
    page.next()
    # Skip mef3io's name check (it needs the anonymizer): straight on.
    page.go(STEP_PASSWORDS)
    step = page.passwords
    step.both.setChecked(True)
    for field_ in (step.basic, step.basic_again):
        field_.setText("bas-1")
    for field_ in (step.full, step.full_again):
        field_.setText("full-2")
    page.next()
    page.next()  # output
    assert page.step == STEP_REVIEW
    window.module_list.setCurrentRow(window.module_keys.index("explorer"))
    assert step.basic.text() == "" and step.full.text() == "" and step.none.isChecked()
    window.module_list.setCurrentRow(window.module_keys.index("anonymizer"))
    # Never on without them: back to the passwords step, saying why.
    assert page.step == STEP_PASSWORDS
    assert step.cleared.isVisibleTo(step) and "cleared" in step.cleared.text()


def test_a_stale_name_verdict_is_ignored(window_page, qtbot, tmp_path):
    from mef3io_studio.model.errors import UserMessage

    window, page = window_page
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    page.next()
    page.dates.keep_utc.setChecked(True)
    page.dates.new_date.setDate(QDate(2010, 3, 1))
    page.next()
    checked = page._names_now()
    page.identity.table.item(0, 1).setText("S-OTHER")  # edited while mef3io looked
    key = page.choices[0].key
    page._names_checked({key: None}, checked)
    assert page.step == STEP_IDENTITY  # the "accepted" was about the old name
    page._names_checked({key: UserMessage("Choose another name for the copy", "x")}, checked)
    assert page.choices[0].names_message is None
    page._names_checked({key: None}, page._names_now())
    assert page.step == STEP_PASSWORDS


def test_a_recording_removed_from_explore_is_taken_out_of_the_run(window_page, qtbot, tmp_path):
    window, page = window_page
    a = write_plain(tmp_path / "a.mefd", notes=False)
    b = write_plain(tmp_path / "b.mefd", notes=False)
    open_in_explorer(qtbot, window, [a, b])
    go_to_channels(page)
    explorer = window.explorer
    gone = next(e for e in explorer.entries() if e.name == "b")
    explorer.model.remove([gone])
    page.next()  # dates: re-checked first
    assert page.step == STEP_RECORDINGS
    assert "b" in page.recordings.lost.text() and page.recordings.lost.isVisibleTo(page)
    assert [c.entry.name for c in page.choices] == ["a"]


def test_an_output_folder_inside_another_recording_is_refused(window_page, qtbot, tmp_path):
    window, page = window_page
    a = write_plain(tmp_path / "a.mefd", notes=False)
    b = write_plain(tmp_path / "b.mefd", notes=False)
    open_in_explorer(qtbot, window, [a, b])
    go_to_channels(page)
    page.next()
    page.dates.keep_utc.setChecked(True)
    page.dates.new_date.setDate(QDate(2010, 3, 1))
    page.next()
    page.go(STEP_REVIEW)
    page.review.set_folder(str(b / "a1.timd"))  # inside the OTHER recording of the batch
    assert not page.review.ok
    assert "inside a recording" in page.review.blocked.text()


def test_an_unfinished_copy_that_stays_is_reported_plainly(window_page):
    from mef3io_studio.anonymizer.page import RunState
    from mef3io_studio.model.anonymize import Leftover

    _window, page = window_page
    page.run = RunState()
    page._on_results([Leftover("k", "/somewhere/S1.mefd.partial")])
    page.run.stopped = True
    page.run.finished = True
    page.run_step.finish()
    text = page.run_step.heading.text()
    assert "could NOT be removed" in text and "S1.mefd.partial" in text
    assert "was removed" not in text


def test_reserved_names_and_the_swap_button(window_page):
    _window, page = window_page
    assert page.picker.invert.text() == "Swap kept and left out"
    from mef3io_studio.model.anonymize import name_problem

    assert name_problem("CON") and name_problem("nul.mefd")


def test_the_output_step_and_its_plain_words(window_page, qtbot, tmp_path):
    window, page = window_page
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    page.next()
    page.dates.keep_utc.setChecked(True)
    page.dates.new_date.setDate(QDate(2010, 3, 1))
    page.next()
    page.go(STEP_OUTPUT)
    out = page.output
    assert out.output() == "folders" and not out.bundle_help.isVisibleTo(out)
    out.bundle.setChecked(True)
    assert out.bundle_help.isVisibleTo(out)
    assert "cannot open this file directly" in out.bundle_help.text()
    assert "does NOT join the recordings into one" in out.bundle_help.text()
    assert "Work folder:" in out.work_label.text()
    out.bundle_name.setText("x.mefd.tar")
    assert "must not end in .mefd.tar" in out.problem_text()
    assert not page.next_button.isEnabled()
    out.bundle_name.setText("study batch")
    assert out.bundle_file() == "study batch.tar" and out.problem_text() == ""
    page.next()
    page.review.set_folder(str(tmp_path / "dest"))
    text = page.review.texts[page.choices[0].key]
    assert "Goes into the batch archive:" in text and "study batch.tar" in text
    assert "extract it first" in page.review.mode_text
    out_archives = page.output
    page.go(STEP_OUTPUT)
    out_archives.archives.setChecked(True)
    page.next()
    page.review.set_folder(str(tmp_path / "dest"))
    assert page.review.texts[page.choices[0].key].count(".mefd.tar") == 1
    assert "work folder" in page.review.mode_text


def test_the_page_is_built_only_when_first_used(window):
    """Review round 2 (11): the wizard's widgets cost the other pages
    nothing until the Anonymize page is opened."""
    page = window.anonymizer
    assert not page.built
    window.explorer.model.add([])  # Explore list signals do not build it
    assert not page.built
    window.module_list.setCurrentRow(window.module_keys.index("anonymizer"))
    window.show()
    assert page.built


def test_the_batch_archive_name_must_not_name_anyone(window_page, qtbot, tmp_path):
    window, page = window_page
    src = write_plain(tmp_path / "ward_study.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    page.go(STEP_OUTPUT)
    page.output.bundle.setChecked(True)
    page.output.bundle_name.setText(f"{LAST} batch")
    assert "patient detail" in page.output.problem_text()
    page.output.bundle_name.setText("my ward_study export")
    assert "original recordings" in page.output.problem_text()
    page.output.bundle_name.setText("batch-2026")
    assert page.output.problem_text() == ""
    from mef3io_studio.anonymizer.page import BUNDLE_EXPLANATION

    assert "mef3io" not in BUNDLE_EXPLANATION
    assert "mef3io" not in page.output.archives.text()


def test_resume_refusals_name_positions_not_recordings(qtbot, jobs, tmp_path):
    from test_batch_pipeline import make_batch

    from mef3io_studio.model import journal, pipeline
    from mef3io_studio.workers import tasks

    src = write_plain(tmp_path / f"{LAST}_rec.mefd", notes=False)
    other = write_plain(tmp_path / "other.mefd", notes=False)
    batch, _dest, _work = make_batch(tmp_path, pipeline.FOLDERS, [other, src], exclude=())
    j = journal.Journal.new(2, pipeline.to_payload(batch))
    src.rename(tmp_path / "moved.mefd")  # gone
    handle = jobs.submit("checks", tasks.preflight_batch, batch, list(j.states), "", j)
    got = []
    handle.succeeded.connect(got.append)
    with qtbot.waitSignal(handle.finished, timeout=120_000):
        pass
    j.delete()
    problems = got[0]["problems"]
    assert any(p.startswith("Recording 2 of 2") for p in problems)
    assert not any(LAST in p for p in problems)
