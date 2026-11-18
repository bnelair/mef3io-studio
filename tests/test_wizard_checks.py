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
    assert not page.back_button.isVisible()
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
