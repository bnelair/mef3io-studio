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
