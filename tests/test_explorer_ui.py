"""The Explore page, offscreen: opening, sorting, masking, passwords, the
check for problems, pack/unpack, scanning and drag and drop. Every read runs
in the background through the helper process; the GUI thread never calls
mef3io."""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path

import mef3io
import pytest
from explorer_sessions import (
    FIRST,
    LAST,
    PASSWORD_BASIC,
    PASSWORD_FULL,
    PATIENT_ID,
    SECOND,
    T0,
    set_time_offset,
    write_encrypted,
    write_plain,
)
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent, QGuiApplication

from mef3io_studio.explorer.dialogs import PasswordDialog
from mef3io_studio.explorer.panels import HIDDEN
from mef3io_studio.explorer.table import COL_NAME, COL_START
from mef3io_studio.model.jobs import JobStatus

SRC = Path(__file__).resolve().parents[1] / "src" / "mef3io_studio"


def wait_loaded(qtbot, page, n, timeout=60_000):
    qtbot.waitUntil(
        lambda: len(page.entries()) == n and all(not e.busy for e in page.entries()),
        timeout=timeout,
    )


def names_in_view_order(page):
    proxy = page.proxy
    return [proxy.index(r, COL_NAME).data() for r in range(proxy.rowCount())]


def _write_at(path, start):
    import numpy as np

    with mef3io.Writer(str(path), overwrite=True) as w:
        w.write("c", np.zeros(512), start, 256.0)
    return path


@pytest.fixture
def page(window):
    # Closing at teardown must not ask about tasks a test left running (the
    # fixture stops them); the question itself is tested in test_app.py.
    window._closing = True
    window.show()
    return window.explorer


def test_opens_and_sorts_by_start_time(page, qtbot, tmp_path):
    # UUID-like names whose alphabetical order is not their time order.
    paths = [
        _write_at(tmp_path / "c3d1.mefd", T0 + 2 * 86_400 * SECOND),
        _write_at(tmp_path / "a9f0.mefd", T0 + 3 * 86_400 * SECOND),
        _write_at(tmp_path / "b442.mefd", T0),
    ]
    page.open_paths([str(p) for p in paths], interactive=False)
    wait_loaded(qtbot, page, 3)
    assert names_in_view_order(page) == ["b442", "c3d1", "a9f0"]
    assert page.table.horizontalHeader().sortIndicatorSection() == COL_START
    page.table.sortByColumn(COL_NAME, Qt.SortOrder.AscendingOrder)
    assert names_in_view_order(page) == ["a9f0", "b442", "c3d1"]
    page.table.sortByColumn(COL_START, Qt.SortOrder.DescendingOrder)
    assert names_in_view_order(page) == ["a9f0", "c3d1", "b442"]
    # Opening the same recording again does not add it twice.
    page.open_paths([str(paths[0])], interactive=False)
    assert len(page.entries()) == 3
    # Multi-select, then remove from the list (nothing is deleted).
    page.table.selectAll()
    assert len(page.selected_entries()) == 3
    page.remove_selected()
    assert page.entries() == [] and all(p.exists() for p in paths)


def test_details_tabs_and_masking(page, qtbot, tmp_path):
    path = write_plain(tmp_path / "rec.mefd")
    page.open_paths([str(path)])
    wait_loaded(qtbot, page, 1)
    entry = page.entries()[0]
    assert page.current_entry() is entry
    assert page.detail_stack.currentWidget() is page.tabs
    assert page.overview.values["Channels"] == "3"
    assert page.channels.table.rowCount() == 3
    assert page.timeline.timeline.details is entry.details

    # Masked by default: patient details and the text of notes.
    assert page.patient.values["First name"] == HIDDEN
    assert page.patient.values["Patient ID"] == HIDDEN
    texts = [page.notes.table.item(r, 3).text() for r in range(page.notes.table.rowCount())]
    assert texts and set(texts) == {HIDDEN}
    where = {page.notes.table.item(r, 1).text() for r in range(page.notes.table.rowCount())}
    assert where == {"Whole recording", "a1", "a1, part 1"}
    assert not any(FIRST in v or PATIENT_ID in v for v in page.overview.values.values())

    # Copy summary while hidden: no names, no dates.
    text = page.copy_summary("text")
    assert text == QGuiApplication.clipboard().text()
    for secret in (FIRST, LAST, PATIENT_ID, "2000-01-01", "rec\n"):
        assert secret not in text
    assert json.loads(page.copy_summary("json"))["patient"] == "[hidden]"

    # Show: only this recording, and the summary then asks first.
    page.patient.button.click()
    assert entry.revealed
    assert page.patient.values["First name"] == FIRST
    assert page.notes.table.item(0, 3).text() != HIDDEN
    asked = []
    page.confirm_copy_with_details = lambda n: asked.append(n) or False
    QGuiApplication.clipboard().setText("before")
    assert page.copy_summary("text") is None
    assert asked == [1] and QGuiApplication.clipboard().text() == "before"
    page.confirm_copy_with_details = lambda n: True
    assert FIRST in page.copy_summary("text")
    page.patient.button.click()  # Hide again
    assert page.patient.values["First name"] == HIDDEN

    # History: created by this mef3io.
    assert any(mef3io.__version__ in t for t in page.history.texts)


def test_password_prompt_and_levels(page, qtbot, tmp_path):
    path = write_encrypted(tmp_path / "locked.mefd")
    page.open_paths([str(path)])
    wait_loaded(qtbot, page, 1)
    entry = page.entries()[0]
    assert entry.needs_password
    qtbot.waitUntil(lambda: any(isinstance(d, PasswordDialog) for d in page._dialogs))
    dialog = next(d for d in page._dialogs if isinstance(d, PasswordDialog))
    assert dialog.field.echoMode() == dialog.field.EchoMode.Password
    dialog.field.setText("wrong")
    dialog.accept()
    qtbot.waitUntil(
        lambda: entry.outcome is not None and entry.outcome.wrong_password, timeout=60_000
    )
    qtbot.waitUntil(lambda: not entry.busy, timeout=60_000)
    assert page.status_view.heading.text() == "That password did not open this recording"
    for d in list(page._dialogs):
        d.reject()

    page._unlock_with(entry, PASSWORD_BASIC)
    qtbot.waitUntil(lambda: entry.details is not None and not entry.busy, timeout=60_000)
    assert page.patient.values["First name"].startswith("Locked")
    assert "(unverified)" in page.overview.values["Start"]

    page._unlock_with(entry, PASSWORD_FULL)
    qtbot.waitUntil(
        lambda: entry.details is not None and entry.details.times_verified and not entry.busy,
        timeout=60_000,
    )
    assert page.patient.values["First name"] == HIDDEN
    page.patient.button.click()
    assert page.patient.values["Last name"] == LAST


def test_time_offset_note_is_shown(page, qtbot, tmp_path):
    path = set_time_offset(write_plain(tmp_path / "masked.mefd", notes=False), 86_400 * SECOND)
    page.open_paths([str(path)], interactive=False)
    wait_loaded(qtbot, page, 1)
    page.select_entry(page.entries()[0])
    texts = [w.text() for w in page.overview.findChildren(type(page.detail_heading))]
    assert any("hides its real date in a way that can be undone" in t for t in texts)
    assert "(unverified)" in page.overview.values["Start"]
    assert not any("anonymized recording." in t and "is an" in t for t in texts)


def test_check_for_problems(page, qtbot, tmp_path):
    path = write_plain(tmp_path / "ok.mefd")
    page.open_paths([str(path)], interactive=False)
    wait_loaded(qtbot, page, 1)
    entry = page.entries()[0]
    page.select_entry(entry)
    handle = page.check(entry)
    assert entry.busy == "Checking…"
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    qtbot.waitUntil(lambda: not entry.busy)
    assert entry.check.ok
    assert page.check_panel.headline.text() == "No problems found"
    assert page.model.display(entry, 8) == "No problems"


def test_pack_and_unpack_into_new_recordings(page, qtbot, tmp_path):
    src = write_plain(tmp_path / "rec.mefd", notes=False)
    out = tmp_path / "packed"
    out.mkdir()
    page.open_paths([str(src)], interactive=False)
    wait_loaded(qtbot, page, 1)
    handle = page.pack(page.entries()[0], str(out))
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    assert handle.record.status is JobStatus.SUCCEEDED, handle.error
    wait_loaded(qtbot, page, 2)
    assert (out / "rec.mefd.tar").is_file()
    assert not [p for p in out.iterdir() if p.name.startswith(".mef3io-studio-")]
    packed = next(e for e in page.entries() if e.outcome.packed)
    # Packing again into the same folder is refused, and nothing is replaced.
    before = (out / "rec.mefd.tar").stat().st_mtime_ns
    handle = page.pack(page.entries()[0], str(out))
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    assert handle.record.status is JobStatus.FAILED
    assert handle.error.title == "That name is already taken"
    assert (out / "rec.mefd.tar").stat().st_mtime_ns == before
    for d in list(page._dialogs):
        d.reject()
    # Unpack the single file somewhere else.
    back = tmp_path / "unpacked"
    back.mkdir()
    handle = page.pack(packed, str(back))
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    assert handle.record.status is JobStatus.SUCCEEDED, handle.error
    assert (back / "rec.mefd").is_dir()


def test_scan_finds_and_opens(page, qtbot, tmp_path):
    root = tmp_path / "scan"
    write_plain(root / "x" / "one.mefd", notes=False)
    write_encrypted(root / "y" / "z" / "two.mefd")
    handle = page.scan_folder(str(root))
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    wait_loaded(qtbot, page, 2)
    assert sorted(e.name for e in page.entries()) == ["one", "two"]
    # A scan never prompts for passwords; the row says what is needed.
    assert not any(isinstance(d, PasswordDialog) for d in page._dialogs)
    two = next(e for e in page.entries() if e.name == "two")
    assert page.model.display(two, COL_START) == "Password needed"
    # Scanning again adds nothing new.
    handle = page.scan_folder(str(root))
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    assert len(page.entries()) == 2


def test_scan_stops_cleanly(page, qtbot, tmp_path):
    root = tmp_path / "many"
    for i in range(400):
        (root / f"d{i:03d}" / "a" / "b").mkdir(parents=True)
    write_plain(root / "d399" / "late.mefd", notes=False)
    handle = page.scan_folder(str(root))
    handle.cancel()
    with qtbot.waitSignal(handle.finished, timeout=30_000):
        pass
    assert handle.record.status is JobStatus.CANCELLED
    assert page.jobs.running_count() == 0
    qtbot.wait(100)
    assert all(not e.busy for e in page.entries())


def test_stopping_an_open_leaves_no_half_rows(page, qtbot, multisegment_session):
    handle = page.open_paths([str(multisegment_session)], interactive=False)
    assert page.entries()[0].busy == "Reading…"
    handle.cancel()
    with qtbot.waitSignal(handle.finished, timeout=30_000):
        pass
    qtbot.waitUntil(lambda: page.entries() == [] or page.entries()[0].outcome is not None)


def test_drag_and_drop_opens(page, qtbot, tmp_path):
    path = write_plain(tmp_path / "dropped.mefd", notes=False)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    page.dropEvent(event)
    wait_loaded(qtbot, page, 1)
    assert page.entries()[0].name == "dropped"


def test_time_zone_choice_changes_every_time_shown(window, qtbot, tmp_path):
    window._closing = True
    window.show()
    page = window.explorer
    page.open_paths([str(write_plain(tmp_path / "tz.mefd", notes=False))], interactive=False)
    wait_loaded(qtbot, page, 1)
    entry = page.entries()[0]
    assert page.model.display(entry, COL_START) == "2000-01-01 00:00:00 UTC"
    window.set_time_zone("America/Chicago")
    assert window.settings.get("time/site_zone") == "America/Chicago"
    assert page.model.display(entry, COL_START) == "1999-12-31 18:00:00 -06:00"
    header = page.model.headerData(COL_START, Qt.Orientation.Horizontal)
    assert header == "Start (America/Chicago)"
    window.set_time_zone("UTC")
    assert window.settings.get("time/site_zone") == ""


def test_gui_thread_never_calls_mef3io(page, qtbot, tmp_path, monkeypatch):
    """Every mef3io entry point the explorer uses, wrapped: a call from the
    GUI thread fails the test. (They run in the helper process, so in this
    process they should not be called at all.)"""
    calls = []

    def guard(name, real):
        def wrapped(*args, **kwargs):
            calls.append((name, threading.current_thread() is threading.main_thread()))
            return real(*args, **kwargs)

        return wrapped

    for name in ("Reader", "Validator", "archive_session", "extract_session"):
        monkeypatch.setattr(mef3io, name, guard(name, getattr(mef3io, name)))
    path = write_plain(tmp_path / "rec.mefd")
    calls.clear()
    page.open_paths([str(path)], interactive=False)
    wait_loaded(qtbot, page, 1)
    handle = page.check(page.entries()[0])
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    out = tmp_path / "o"
    out.mkdir()
    handle = page.pack(page.entries()[0], str(out))
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    assert not [c for c in calls if c[1]], calls


def test_widgets_never_import_mef3io():
    """The explorer's and the shell's widgets reach mef3io only through
    background tasks (workers/) and the plain model."""
    offenders = []
    for folder in ("explorer", "common"):
        for path in (SRC / folder).rglob("*.py"):
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


def test_what_the_explorer_learns_never_reaches_the_log(page, qtbot, tmp_path):
    from mef3io_studio.common.privacy import scrub

    path = write_encrypted(tmp_path / "Zebedee_case.mefd")
    page.open_paths([str(path)], interactive=False)
    wait_loaded(qtbot, page, 1)
    page._unlock_with(page.entries()[0], PASSWORD_FULL)
    qtbot.waitUntil(lambda: page.entries()[0].details is not None, timeout=60_000)
    line = f"oops {FIRST} {LAST} / {LAST}, {FIRST} {PATIENT_ID} Zebedee_case {PASSWORD_FULL}"
    scrubbed = scrub(line)
    for secret in (FIRST, LAST, PATIENT_ID, "Zebedee_case", PASSWORD_FULL):
        assert secret not in scrubbed, scrubbed


def test_stopping_a_pack_leaves_nothing_behind(page, qtbot, tmp_path):
    from explorer_sessions import write_large

    src = write_large(tmp_path / "big.mefd", channels=2, parts=2, seconds=600)
    page.open_paths([str(src)], interactive=False)
    wait_loaded(qtbot, page, 1)
    out = tmp_path / "out"
    out.mkdir()
    handle = page.pack(page.entries()[0], str(out))
    qtbot.waitUntil(lambda: any(out.iterdir()), timeout=30_000)
    handle.cancel()
    with qtbot.waitSignal(handle.finished, timeout=30_000):
        pass
    assert handle.record.status is JobStatus.CANCELLED
    assert list(out.iterdir()) == []


def test_pack_of_a_password_protected_recording(page, qtbot, tmp_path):
    src = write_encrypted(tmp_path / "locked.mefd")
    page.open_paths([str(src)], interactive=False)
    wait_loaded(qtbot, page, 1)
    entry = page.entries()[0]
    page._unlock_with(entry, PASSWORD_FULL)
    qtbot.waitUntil(lambda: entry.details is not None and not entry.busy, timeout=60_000)
    out = tmp_path / "out"
    out.mkdir()
    handle = page.pack(entry, str(out))
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    assert handle.record.status is JobStatus.SUCCEEDED, handle.error
    assert (out / "locked.mefd.tar").is_file()


def test_a_helper_crash_costs_only_that_recording(page, qtbot, tmp_path):
    import helper_targets

    paths = [write_plain(tmp_path / f"{n}.mefd", notes=False) for n in ("a", "crash", "c")]
    page.source = helper_targets.CrashOnSecond()
    page.open_paths([str(p) for p in paths], interactive=False)
    wait_loaded(qtbot, page, 3)
    by_name = {e.name: e for e in page.entries()}
    assert by_name["a"].details is not None and by_name["c"].details is not None
    assert by_name["crash"].outcome.error.title == "The app stopped while reading this recording"


def test_an_older_task_cannot_remove_a_reopened_recording(page, qtbot, tmp_path):
    path = write_plain(tmp_path / "x.mefd", notes=False)
    first = page.open_paths([str(path)], interactive=False)
    page.table.selectAll()
    page.remove_selected()
    second = page.open_paths([str(path)], interactive=False)
    first.cancel()
    with qtbot.waitSignal(second.finished, timeout=60_000):
        pass
    qtbot.wait(200)
    assert [e.name for e in page.entries()] == ["x"]
    assert page.entries()[0].details is not None
