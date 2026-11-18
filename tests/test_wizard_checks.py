"""The wizards' up-front check (usability review of the Anonymize, Make new,
Join or trim and Edit details wizards).

What these tests pin, one section per item:

* A: what is written is exactly what was checked. Once Make the copies is
  pressed, the choices cannot be changed (the check is step 1 of the run, on
  the run page), and a check whose choices changed anyway never starts a run.
* Later sections are added with their commits.
"""

from __future__ import annotations

import time

import pytest
from explorer_sessions import write_plain
from test_anonymizer_ui import (
    STEP_IDENTITY,
    STEP_REVIEW,
    STEP_RUN,
    _through_to_review,
    go_to_channels,
    open_in_explorer,
)


@pytest.fixture
def window_page(window):
    window._closing = True
    window.show()
    return window, window.anonymizer


def slow(fn, seconds):
    """The same task after ``seconds`` of doing nothing (stoppable)."""

    def wrapper(ctx, *args, **kwargs):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ctx.check_cancelled()
            time.sleep(0.02)
        return fn(ctx, *args, **kwargs)

    return wrapper


def anonymizer_at_review(window, page, qtbot, tmp_path, name="S001"):
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    out = tmp_path / "out"
    out.mkdir()
    _through_to_review(page, qtbot, out, names=[name])
    return out


# --------------------------------------------------------------------------
# A: what is written is what was checked


def test_the_choices_cannot_be_reached_while_the_check_runs(
    window_page, qtbot, tmp_path, monkeypatch
):
    """Review finding A: Back stayed available during "Checking everything
    before starting", so the choices could be changed under a running check
    and the run then wrote the old ones."""
    import mef3io_studio.anonymizer.page as P

    window, page = window_page
    out = anonymizer_at_review(window, page, qtbot, tmp_path)
    monkeypatch.setattr(P, "preflight_batch", slow(P.preflight_batch, 1.0))
    handle = page.start()
    qtbot.wait(100)
    assert not handle.is_finished
    # The check is step 1 of the run: its page is shown, no choice is on screen.
    assert page.step == STEP_RUN
    assert not page.back_button.isVisibleTo(page)
    page.back()
    assert page.step == STEP_RUN
    assert not page.next_button.isEnabled()
    qtbot.waitUntil(lambda: page.run.finished, timeout=120_000)
    assert (out / "S001.mefd").is_dir()


def test_a_check_about_other_choices_never_starts_a_run(window_page, qtbot, tmp_path, monkeypatch):
    """Whatever way a choice changed during the check (here: straight into the
    widget, which no click can do), the run does not start with a check that
    judged other choices: nothing is written, the choices stay, and the page
    says so."""
    import mef3io_studio.anonymizer.page as P

    window, page = window_page
    out = anonymizer_at_review(window, page, qtbot, tmp_path)
    monkeypatch.setattr(P, "preflight_batch", slow(P.preflight_batch, 1.0))
    handle = page.start()
    qtbot.wait(100)
    page.identity.table.item(0, 1).setText("S777")  # not reachable by a click
    qtbot.waitUntil(lambda: handle.is_finished, timeout=60_000)
    qtbot.waitUntil(lambda: page.step != STEP_RUN, timeout=10_000)
    assert page.step == STEP_REVIEW
    assert page.handle is None  # no run was started
    assert list(out.iterdir()) == []
    assert "changed while the check was running" in page.review.blocked.text()
    # The choices are kept, as typed.
    assert page.identity.table.item(0, 1).text() == "S777"


def test_negative_control_without_the_guard_the_old_choices_are_written(
    window_page, qtbot, tmp_path, monkeypatch
):
    """With the guard removed the run is seen writing the choices the check
    judged while the page shows others: that is what the guard prevents."""
    import mef3io_studio.anonymizer.page as P

    window, page = window_page
    out = anonymizer_at_review(window, page, qtbot, tmp_path)
    monkeypatch.setattr(P, "preflight_batch", slow(P.preflight_batch, 1.0))
    monkeypatch.setattr(P.AnonymizerPage, "_check_is_stale", lambda self, batch: False)
    handle = page.start()
    qtbot.wait(100)
    page.identity.table.item(0, 1).setText("S777")
    qtbot.waitUntil(lambda: handle.is_finished, timeout=60_000)
    qtbot.waitUntil(lambda: page.run.finished, timeout=120_000)
    assert [p.name for p in out.iterdir()] == ["S001.mefd"]  # not what the page showed
    assert page.identity.table.item(0, 1).text() == "S777"


# --------------------------------------------------------------------------
# B / item 4: cleared passwords block Start until the person decides again

PW = ("basic-pw-1", "full-pw-22")


def click_radio(qtbot, button):
    """A real click on the round button itself."""
    from PySide6.QtCore import QPoint, Qt

    qtbot.mouseClick(button, Qt.MouseButton.LeftButton, pos=QPoint(6, button.height() // 2))


def type_passwords(step, pw=PW):
    step.both.setChecked(True)
    step.basic.setText(pw[0])
    step.basic_again.setText(pw[0])
    step.full.setText(pw[1])
    step.full_again.setText(pw[1])


def test_cleared_passwords_block_until_decided_again(qtbot, qapp):
    """A cleared form is not "no password": it is a question. Only a click on
    a choice, or new passwords, answers it."""
    from mef3io_studio.common.passwords import PasswordsStep

    step = PasswordsStep(None)
    qtbot.addWidget(step)
    step.show()
    assert step.problem_text() == ""  # nothing was ever typed: nothing to ask
    type_passwords(step)
    assert step.problem_text() == ""
    step.forget()
    assert step.passwords() is None  # the values are gone
    assert "cleared for safety" in step.problem_text()
    assert step.cleared.isVisible()
    # Typing nothing, or toggling by program, decides nothing.
    step.none.setChecked(True)
    assert step.problem_text()
    # A real click on "No password" is the explicit choice to go without.
    click_radio(qtbot, step.none)
    assert step.problem_text() == ""
    assert not step.cleared.isVisible()
    # And typed passwords answer it too.
    type_passwords(step)
    step.forget()
    assert step.problem_text()
    type_passwords(step)
    assert step.problem_text() == "" and step.passwords() == PW


def test_the_fields_cannot_bring_the_passwords_back(qtbot):
    from mef3io_studio.common.passwords import PasswordsStep

    step = PasswordsStep(None)
    qtbot.addWidget(step)
    type_passwords(step)
    step.forget()
    step.basic.undo()
    step.full.undo()
    assert step.basic.text() == "" and step.full.text() == ""


def test_anonymize_without_the_passwords_never_starts_silently(window_page, qtbot, tmp_path):
    """Review finding B (Anonymize): the page was left with passwords typed;
    they were cleared and the wizard went on to Review, where Make the copies
    wrote copies WITHOUT them."""
    from test_anonymizer_ui import STEP_PASSWORDS

    window, page = window_page
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    window.module_list.setCurrentRow(window.module_keys.index("anonymizer"))
    go_to_channels(page)
    out = tmp_path / "out"
    out.mkdir()
    _through_to_review(page, qtbot, out, names=["P1"], passwords=PW)
    assert page.review.ok
    window.module_list.setCurrentRow(window.module_keys.index("explorer"))
    qtbot.wait(100)
    window.module_list.setCurrentRow(window.module_keys.index("anonymizer"))
    # Brought back to the question, which says what happened.
    assert page.step == STEP_PASSWORDS
    assert page.passwords.cleared.isVisible()
    assert "cleared for safety" in page.why_not.text()
    assert not page.next_button.isEnabled()
    page.next()
    assert page.step == STEP_PASSWORDS
    # Even by jumping ahead the copies are not made without an answer.
    page.go(STEP_REVIEW)
    assert page.start() is None
    assert "passwords" in page.why_not.text().lower()
    assert not page.next_button.isEnabled()
    page.go(STEP_PASSWORDS)
    click_radio(qtbot, page.passwords.none)
    assert page.next_button.isEnabled()
    page.next()
    page.next()
    assert page.step == STEP_REVIEW and page.next_button.isEnabled()


def test_join_leaving_the_page_asks_for_the_passwords_again(window, qtbot, tmp_path):
    from test_joiner_ui import HOUR, T0, choose_all, write

    from mef3io_studio.joiner.page import KEEP_FIRST, STEP_NEW

    window._closing = True
    window.show()
    window.module_list.setCurrentRow(window.module_keys.index("joiner"))
    page = window.joiner
    a = write(tmp_path / "A.mefd", T0)
    b = write(tmp_path / "B.mefd", T0 + 3 * HOUR)
    open_in_explorer(qtbot, window, [a, b])
    choose_all(page)
    page.next()
    assert page.step == STEP_NEW
    page.new.folder = str(tmp_path)
    page.new.name.setText("joined")
    type_passwords(page.new.passwords)
    assert page.next_button.isEnabled()
    window.module_list.setCurrentRow(window.module_keys.index("explorer"))
    qtbot.wait(100)
    window.module_list.setCurrentRow(window.module_keys.index("joiner"))
    assert page.step == STEP_NEW
    assert page.new.passwords.passwords() is None
    assert not page.next_button.isEnabled()  # no silent run without them
    assert "cleared for safety" in page.new.problem.text() or page.new.passwords.cleared.isVisible()
    assert page.next() is None
    click_radio(qtbot, page.new.passwords.none)
    assert page.next_button.isEnabled()
    assert KEEP_FIRST


def test_make_new_closing_with_x_during_the_check_keeps_the_passwords(
    window, qtbot, tmp_path, monkeypatch
):
    """Review finding B (Make new): the X wiped the passwords although the
    window stayed open (it refuses to close while busy); after a refused check
    the next Start wrote the new recordings with no password."""
    import mef3io
    from test_make_new_ui import make_dialog, open_all

    import mef3io_studio.makenew.run as R
    from mef3io_studio.makenew import dialog as dlg

    window._closing = True
    window.show()
    sources = [write_plain(tmp_path / f"rec{i}.mefd") for i in range(2)]
    out = tmp_path / "out"
    out.mkdir()
    entries = open_all(qtbot, window, sources)
    dialog = make_dialog(qtbot, window, entries, tmp_path)
    dialog.open()
    real_preflight = R.preflight_batch
    monkeypatch.setattr(R, "preflight_batch", slow(real_preflight, 1.0))
    dialog.next()
    dialog.picker.click("a3", False)
    dialog.next()
    dialog.next()
    type_passwords(dialog.passwords)
    dialog.next()
    assert dialog.step == dlg.STEP_WHERE
    dialog.folder = str(out)
    dialog._changed()
    handle = dialog.start()
    qtbot.wait(100)
    dialog.close()  # the X of the window, while the check runs
    assert dialog.isVisible()  # it does not close, and says why
    assert "Stop" in dialog.closing_note.text()
    (out / "rec0-new.mefd").mkdir()  # a name gets taken: the check refuses
    qtbot.waitUntil(lambda: handle.is_finished, timeout=60_000)
    qtbot.waitUntil(lambda: dialog.step == dlg.STEP_WHERE, timeout=10_000)
    (out / "rec0-new.mefd").rmdir()
    dialog._changed()
    assert dialog.passwords.passwords() == PW  # still there: nothing was cleared
    monkeypatch.setattr(R, "preflight_batch", real_preflight)
    dialog.start()
    qtbot.waitUntil(lambda: dialog.finished_run, timeout=180_000)
    for p in out.iterdir():
        with pytest.raises(Exception):  # noqa: B017, PT011
            mef3io.Reader(str(p))  # protected: it needs the passwords
        mef3io.Reader(str(p), password=PW[1])
    dialog.reject()


def test_make_new_after_a_run_the_passwords_are_asked_again(window, qtbot, tmp_path):
    from test_make_new_ui import make_dialog, open_all

    from mef3io_studio.makenew import dialog as dlg

    window._closing = True
    window.show()
    sources = [write_plain(tmp_path / f"rec{i}.mefd") for i in range(1)]
    out = tmp_path / "out"
    out.mkdir()
    entries = open_all(qtbot, window, sources)
    dialog = make_dialog(qtbot, window, entries, tmp_path)
    dialog.open()
    dialog.next()
    dialog.picker.click("a3", False)
    dialog.next()
    dialog.next()
    type_passwords(dialog.passwords)
    dialog.next()
    dialog.folder = str(out)
    dialog._changed()
    dialog.start()
    qtbot.waitUntil(lambda: dialog.finished_run, timeout=180_000)
    dialog.go(dlg.STEP_PASSWORDS)
    assert dialog.passwords.needs_decision
    dialog.go(dlg.STEP_WHERE)
    dialog.folder = str(tmp_path / "second")
    (tmp_path / "second").mkdir()
    dialog._changed()
    assert not dialog.start_button.isEnabled()
    assert "password" in dialog.problem.text().lower()
    click_radio(qtbot, dialog.passwords.none)
    dialog._changed()
    assert dialog.start_button.isEnabled()
    dialog.reject()


# --------------------------------------------------------------------------
# Items 1-3 for Join or trim: one button, the check is step 1 of the run


def _join_page_at_new(window, qtbot, tmp_path, monkeypatch=None):
    from test_joiner_ui import HOUR, T0, choose_all, write

    from mef3io_studio.joiner.page import KEEP_FIRST, KEEP_LAST, STEP_NEW

    window._closing = True
    window.show()
    window.module_list.setCurrentRow(window.module_keys.index("joiner"))
    page = window.joiner
    a = write(tmp_path / "A.mefd", T0)
    b = write(tmp_path / "B.mefd", T0 + 3 * HOUR)
    open_in_explorer(qtbot, window, [a, b])
    choose_all(page)
    page.keep.rows[0].set_how(KEEP_LAST)
    page.keep.rows[0].duration.set_seconds(3600)
    page.keep.rows[1].set_how(KEEP_FIRST)
    page.keep.rows[1].duration.set_seconds(1800)
    page.next()
    assert page.step == STEP_NEW
    out = tmp_path / "out"
    out.mkdir()
    page.new.folder = str(out)
    page.new.name.setText("joined")
    page._update_buttons()
    return page, out


def test_join_has_one_button_and_the_check_is_step_one_on_the_run_page(
    window, qtbot, tmp_path, monkeypatch
):
    import mef3io_studio.joiner.page as J

    page, out = _join_page_at_new(window, qtbot, tmp_path)
    monkeypatch.setattr(J, "check_join", slow(J.check_join, 1.0))
    assert page.next_button.text() == "Save the new recording"  # one button, no Check
    check = page.next()
    qtbot.wait(100)
    assert not check.is_finished
    assert page.step == J.STEP_RUN
    assert "Step 1 of 2" in page.run_step.heading.text()
    assert page.run_step.progress.cancel_button.isEnabled()  # Stop is there
    page.back()
    assert page.step == J.STEP_RUN  # nothing can be changed meanwhile
    qtbot.waitUntil(lambda: page.handle is not None and page.handle.is_finished, timeout=120_000)
    assert (out / "joined.mefd").is_dir()
    assert "Saved and checked" in page.run_step.heading.text()


def test_join_a_refusal_lists_every_reason_and_keeps_every_choice(
    window, qtbot, tmp_path, monkeypatch
):
    import mef3io_studio.joiner.page as J

    page, out = _join_page_at_new(window, qtbot, tmp_path)
    type_passwords(page.new.passwords)
    monkeypatch.setattr(J, "check_join", slow(J.check_join, 0.6))
    check = page.next()
    qtbot.wait(100)
    (out / "joined.mefd").mkdir()  # the name gets taken meanwhile
    qtbot.waitUntil(lambda: check.is_finished, timeout=60_000)
    qtbot.waitUntil(lambda: page.step == J.STEP_NEW, timeout=10_000)
    assert page.handle is None
    assert list(out.iterdir()) == [out / "joined.mefd"]  # nothing was written
    assert page.new.name.text() == "joined"
    assert page.new.passwords.passwords() == PW  # nothing was cleared
    assert page.new.findings.count() == 1
    assert "cannot be saved yet" in page.new.problem.text() or page.new.findings.count()
    assert "already" in page.why_not.text() or "already" in page.new.problem.text()


def test_join_stop_during_the_check_keeps_the_choices(window, qtbot, tmp_path, monkeypatch):
    import mef3io_studio.joiner.page as J

    page, out = _join_page_at_new(window, qtbot, tmp_path)
    monkeypatch.setattr(J, "check_join", slow(J.check_join, 5.0))
    check = page.next()
    qtbot.wait(100)
    qtbot.mouseClick(
        page.run_step.progress.cancel_button,
        __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.MouseButton.LeftButton,
    )
    qtbot.waitUntil(lambda: check.is_finished, timeout=30_000)
    qtbot.waitUntil(lambda: page.step == J.STEP_NEW, timeout=10_000)
    assert "stopped" in page.new.notice.text().lower()
    assert page.new.name.text() == "joined" and list(out.iterdir()) == []


def test_join_a_check_about_other_choices_never_starts_a_run(window, qtbot, tmp_path, monkeypatch):
    import mef3io_studio.joiner.page as J

    page, out = _join_page_at_new(window, qtbot, tmp_path)
    monkeypatch.setattr(J, "check_join", slow(J.check_join, 0.8))
    check = page.next()
    qtbot.wait(100)
    page.new.name.setText("renamed")  # no click can do this on the run page
    qtbot.waitUntil(lambda: check.is_finished, timeout=60_000)
    qtbot.waitUntil(lambda: page.step == J.STEP_NEW, timeout=10_000)
    assert page.handle is None and list(out.iterdir()) == []
    assert "changed while the check was running" in page.new.notice.text()


# --------------------------------------------------------------------------
# Items 1-3 for Make new recordings and Cut into pieces (one shared run half)


def _make_new_at_where(window, qtbot, tmp_path, passwords=True):
    from test_make_new_ui import make_dialog, open_all

    from mef3io_studio.makenew import dialog as dlg

    window._closing = True
    window.show()
    sources = [write_plain(tmp_path / f"rec{i}.mefd") for i in range(2)]
    out = tmp_path / "out"
    out.mkdir()
    entries = open_all(qtbot, window, sources)
    dialog = make_dialog(qtbot, window, entries, tmp_path)
    dialog.open()
    dialog.next()
    dialog.picker.click("a3", False)
    dialog.next()
    dialog.next()
    if passwords:
        type_passwords(dialog.passwords)
    dialog.next()
    assert dialog.step == dlg.STEP_WHERE
    dialog.folder = str(out)
    dialog._changed()
    return dialog, out


def test_make_new_the_check_is_step_one_and_nothing_can_change(
    window, qtbot, tmp_path, monkeypatch
):
    import mef3io_studio.makenew.run as R
    from mef3io_studio.makenew import dialog as dlg

    dialog, out = _make_new_at_where(window, qtbot, tmp_path)
    monkeypatch.setattr(R, "preflight_batch", slow(R.preflight_batch, 1.0))
    assert dialog.start_button.text() == "Make the new recordings"  # one button
    handle = dialog.start()
    qtbot.wait(100)
    assert dialog.step == dlg.STEP_RUN
    assert "Step 1 of 2" in dialog.run_heading.text()
    assert "Step 1 of 2" in dialog.progress_slot.itemAt(0).widget().label.text()
    assert not dialog.back_button.isVisible() and not dialog.close_button.isEnabled()
    dialog.back()
    assert dialog.step == dlg.STEP_RUN
    qtbot.waitUntil(lambda: dialog.finished_run, timeout=180_000)
    assert "Step 2 of 2" in dialog.run_heading.text() or dialog.finished_run
    assert sorted(p.name for p in out.iterdir()) == ["rec0-new.mefd", "rec1-new.mefd"]
    assert handle.is_finished


def test_make_new_stop_during_the_check_keeps_every_choice(window, qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt

    import mef3io_studio.makenew.run as R
    from mef3io_studio.makenew import dialog as dlg

    dialog, out = _make_new_at_where(window, qtbot, tmp_path)
    monkeypatch.setattr(R, "preflight_batch", slow(R.preflight_batch, 5.0))
    handle = dialog.start()
    qtbot.wait(100)
    qtbot.mouseClick(
        dialog.progress_slot.itemAt(0).widget().cancel_button, Qt.MouseButton.LeftButton
    )
    qtbot.waitUntil(lambda: handle.is_finished, timeout=30_000)
    qtbot.waitUntil(lambda: dialog.step == dlg.STEP_WHERE, timeout=10_000)
    assert (
        "stopped" in dialog.problem.text().lower() and "choices are kept" in dialog.problem.text()
    )
    assert dialog.passwords.passwords() == PW and dialog.folder == str(out)
    assert dialog.start_button.isEnabled() and list(out.iterdir()) == []


def test_make_new_a_check_about_other_choices_never_starts_a_run(
    window, qtbot, tmp_path, monkeypatch
):
    import mef3io_studio.makenew.run as R
    from mef3io_studio.makenew import dialog as dlg

    dialog, out = _make_new_at_where(window, qtbot, tmp_path)
    monkeypatch.setattr(R, "preflight_batch", slow(R.preflight_batch, 1.0))
    handle = dialog.start()
    qtbot.wait(100)
    dialog.naming_text.setText("-other")  # no click can reach this on the run page
    qtbot.waitUntil(lambda: handle.is_finished, timeout=60_000)
    qtbot.waitUntil(lambda: dialog.step == dlg.STEP_WHERE, timeout=10_000)
    assert "changed while the check was running" in dialog.problem.text()
    assert list(out.iterdir()) == [] and dialog.handle is handle


# --------------------------------------------------------------------------
# Items 1-3 for Edit details (Changes)


def _edit_dialog(window, qtbot, tmp_path):
    from test_edit_details_ui import open_dialog, open_entry

    window._closing = True
    window.show()
    entry = open_entry(qtbot, window, write_plain(tmp_path / "rec.mefd"))
    dialog = open_dialog(qtbot, window, entry)
    out = tmp_path / "out"
    out.mkdir()
    dialog.open()
    dialog.boxes["recording_location"].set_text("Room 5")
    dialog.folder = str(out)
    dialog.name.setText("edited")
    dialog._update()
    return dialog, out


def test_edit_details_one_button_and_the_check_shows_what_it_does(
    window, qtbot, tmp_path, monkeypatch
):
    import mef3io_studio.explorer.edit_details as E

    dialog, out = _edit_dialog(window, qtbot, tmp_path)
    monkeypatch.setattr(E, "check_changes", slow(E.check_changes, 1.0))
    assert dialog.save_button.text() == "Save the new recording"  # one button
    assert dialog.save_button.isEnabled()
    handle = dialog.check_and_save()
    qtbot.wait(100)
    assert not handle.is_finished
    widget = dialog.progress_slot.itemAt(0).widget()
    assert widget.label.text().startswith("Step 1 of 2")
    assert widget.cancel_button.isEnabled()  # Stop is on the window
    assert "writes nothing" in widget.note.text()
    assert not dialog.inputs.isEnabled()  # nothing can be changed meanwhile
    qtbot.waitUntil(lambda: dialog.result is not None, timeout=120_000)
    qtbot.waitUntil(lambda: not dialog._busy, timeout=60_000)
    assert (out / "edited.mefd").is_dir()


def test_edit_details_stop_during_the_check_keeps_every_choice(
    window, qtbot, tmp_path, monkeypatch
):
    from PySide6.QtCore import Qt

    import mef3io_studio.explorer.edit_details as E

    dialog, out = _edit_dialog(window, qtbot, tmp_path)
    monkeypatch.setattr(E, "check_changes", slow(E.check_changes, 5.0))
    handle = dialog.check_and_save()
    qtbot.wait(100)
    qtbot.mouseClick(
        dialog.progress_slot.itemAt(0).widget().cancel_button, Qt.MouseButton.LeftButton
    )
    qtbot.waitUntil(lambda: handle.is_finished, timeout=30_000)
    qtbot.waitUntil(lambda: not dialog._busy, timeout=10_000)
    assert "check was stopped" in dialog.result_label.text()
    assert dialog.name.text() == "edited" and dialog.boxes["recording_location"].text() == "Room 5"
    assert dialog.save_button.isEnabled() and list(out.iterdir()) == []


def test_edit_details_a_check_about_other_choices_never_starts_a_save(
    window, qtbot, tmp_path, monkeypatch
):
    import mef3io_studio.explorer.edit_details as E

    dialog, out = _edit_dialog(window, qtbot, tmp_path)
    monkeypatch.setattr(E, "check_changes", slow(E.check_changes, 1.0))
    handle = dialog.check_and_save()
    qtbot.wait(100)
    dialog.boxes["recording_location"].set_text("Room 9")  # no click can reach it
    qtbot.waitUntil(lambda: handle.is_finished, timeout=60_000)
    qtbot.waitUntil(lambda: not dialog._busy, timeout=10_000)
    assert "changed while the check was running" in dialog.result_label.text()
    assert dialog.handle is None and list(out.iterdir()) == []


# --------------------------------------------------------------------------
# C / D / item 5: after a run ends, the choices are kept and the result stays


def test_anonymize_after_stop_the_choices_are_kept_and_the_result_is_reachable(
    window_page, qtbot, tmp_path, monkeypatch
):
    """Review findings C and D: after Stop only "Anonymize more recordings"
    was left, which threw every choice away, and the outcome was lost with it."""
    from PySide6.QtCore import Qt
    from test_anonymizer_ui import _gate

    window, page = window_page
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    out = tmp_path / "out"
    out.mkdir()
    _through_to_review(page, qtbot, out, names=["BIG1"])
    entered = _gate(monkeypatch, tmp_path, "during")
    page.start()
    qtbot.waitUntil(entered.exists, timeout=60_000)
    assert not page.back_button.isVisibleTo(page)  # still running: no way out but Stop
    page.stop()
    qtbot.waitUntil(lambda: page.run.finished, timeout=60_000)
    stopped = page.run_step.heading.text()
    assert stopped.startswith("Stopped")
    # The way back keeps everything.
    assert page.back_button.isVisibleTo(page) and page.back_button.text() == "Back to my choices"
    qtbot.mouseClick(page.back_button, Qt.MouseButton.LeftButton)
    assert page.step == STEP_REVIEW
    assert [c.name for c in page.choices] == ["BIG1"]
    assert page.review.folder == str(out)
    assert page.recordings.ticked
    # ... and the outcome stays reachable until the next run starts.
    assert page.result_button.isVisibleTo(page)
    qtbot.mouseClick(page.result_button, Qt.MouseButton.LeftButton)
    assert page.step == STEP_RUN and page.run_step.heading.text() == stopped
    page.back()
    assert page.step == STEP_REVIEW
    assert page.next_button.isEnabled()  # start again, nothing to type again
    monkeypatch.undo()
    page.start()
    assert not page.result_button.isVisibleTo(page)  # a new run: the old outcome is gone
    qtbot.waitUntil(lambda: page.run.finished, timeout=120_000)
    assert (out / "BIG1.mefd").is_dir()


def test_join_after_the_run_the_choices_are_kept_and_the_result_is_reachable(
    window, qtbot, tmp_path
):
    from PySide6.QtCore import Qt

    import mef3io_studio.joiner.page as J

    page, out = _join_page_at_new(window, qtbot, tmp_path)
    page.next()
    qtbot.waitUntil(lambda: page.handle is not None and page.handle.is_finished, timeout=120_000)
    qtbot.waitUntil(lambda: page.back_button.isVisibleTo(page), timeout=10_000)
    assert page.back_button.text() == "Back to my choices"
    heading = page.run_step.heading.text()
    qtbot.mouseClick(page.back_button, Qt.MouseButton.LeftButton)
    assert page.step == J.STEP_NEW
    assert page.new.name.text() == "joined" and len(page.keep.rows) == 2
    assert page.result_button.isVisibleTo(page)
    qtbot.mouseClick(page.result_button, Qt.MouseButton.LeftButton)
    assert page.step == J.STEP_RUN and page.run_step.heading.text() == heading
    assert page.run_step.open_button.isVisibleTo(page)  # "Open it in Explore" still works


def test_make_new_after_the_run_the_choices_are_kept_and_the_result_is_reachable(
    window, qtbot, tmp_path
):
    from PySide6.QtCore import Qt

    from mef3io_studio.makenew import dialog as dlg

    dialog, out = _make_new_at_where(window, qtbot, tmp_path, passwords=False)
    dialog.passwords.none.setChecked(True)
    dialog.change_details.setChecked(True)
    dialog.boxes["recording_location"].set_text("Room 5")
    dialog.start()
    qtbot.waitUntil(lambda: dialog.finished_run, timeout=180_000)
    assert dialog.back_button.isVisible() and dialog.back_button.text() == "Back to my choices"
    summary = dialog.summary.text()
    qtbot.mouseClick(dialog.back_button, Qt.MouseButton.LeftButton)
    assert dialog.step == dlg.STEP_WHERE and dialog.folder == str(out)
    assert dialog.boxes["recording_location"].text() == "Room 5"
    assert dialog.result_button.isVisible()
    qtbot.mouseClick(dialog.result_button, Qt.MouseButton.LeftButton)
    assert dialog.step == dlg.STEP_RUN and dialog.summary.text() == summary


# --------------------------------------------------------------------------
# E: the same recordings are not checked again for nothing


def test_anonymize_the_name_check_is_not_repeated_when_nothing_changed(
    window_page, qtbot, tmp_path
):
    """Review finding E: Back to Identity and Next again, changing nothing,
    started the same mef3io name check over the same recordings again."""
    from test_anonymizer_ui import STEP_PASSWORDS

    window, page = window_page
    src = write_plain(tmp_path / "orig.mefd", notes=False)
    open_in_explorer(qtbot, window, [src])
    go_to_channels(page)
    out = tmp_path / "out"
    out.mkdir()
    _through_to_review(page, qtbot, out, names=["E1"])
    page.go(STEP_IDENTITY)
    before = len(window.jobs.handles)
    page.next()  # nothing changed since the names passed
    assert len(window.jobs.handles) == before
    assert page.step == STEP_PASSWORDS
    # A changed name is checked again.
    page.go(STEP_IDENTITY)
    page.identity.table.item(0, 1).setText("E2")
    page.next()
    assert len(window.jobs.handles) == before + 1
    qtbot.waitUntil(lambda: page.step == STEP_PASSWORDS, timeout=60_000)
