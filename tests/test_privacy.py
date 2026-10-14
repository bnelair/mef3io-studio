"""No patient details and no absolute dates ever reach the log file."""

import logging

import mef3io
import numpy as np
import pytest

from mef3io_studio.common import privacy
from mef3io_studio.common.logging_setup import LOG_FILE_NAME, setup_logging, teardown_logging
from mef3io_studio.common.privacy import IdentityRegistry, scrub
from mef3io_studio.model.source import LocalRecordingSource
from mef3io_studio.workers.tasks import summarize_recording

# Synthetic identity and a synthetic recording date (never real data).
NAME_1, NAME_2, SUBJECT_ID = "Zebulon", "Quartermaine", "MRN-55501234"
PASSWORD = "Hunter2secret"
START_UUTC = 1_552_555_613_000_000  # 2019-03-14 09:26:53 UTC
DATE_FORMS = [
    "2019-03-14",
    "2019-03-14T09:26:53Z",
    "14/03/2019",
    "03/14/2019",
    "14.03.2019",
    "March 14, 2019",
    "14 Mar 2019",
    "20190314",
    str(START_UUTC),
    str(-START_UUTC),
    str(START_UUTC // 1000),
]


@pytest.mark.parametrize("form", DATE_FORMS)
def test_scrub_removes_every_date_form(form):
    out = scrub(f"recording starts {form} here", IdentityRegistry())
    assert form not in out
    assert "[time]" in out


@pytest.mark.parametrize(
    "text", ["mef3io 1.1.4", "Python 3.12.10", "Qt 6.12.0", "job 42", "256.0 Hz"]
)
def test_scrub_keeps_ordinary_numbers(text):
    assert scrub(text, IdentityRegistry()) == text


def test_identity_redaction_is_case_insensitive():
    registry = IdentityRegistry()
    registry.register("Zebulon", "", None, "x")  # empty/one-char ignored
    assert scrub("ZEBULON and zebulon", registry) == "[redacted] and [redacted]"
    assert scrub("x marks", registry) == "x marks"


def test_log_file_never_contains_identity_or_dates(qtbot, jobs, tmp_path):
    log_dir = tmp_path / "logs"
    log_file = setup_logging(log_dir, level=logging.DEBUG)
    assert log_file == log_dir / LOG_FILE_NAME
    privacy.identities.clear()
    try:
        # A session whose metadata and folder name carry the identity.
        path = tmp_path / f"{NAME_2}_{NAME_1}.mefd"
        meta = mef3io.Metadata(subject=mef3io.Subject(name_1=NAME_1, name_2=NAME_2, id=SUBJECT_ID))
        with mef3io.Writer(str(path), metadata=meta, password1="L1pass", password2=PASSWORD) as w:
            w.write("c1", np.sin(np.arange(1024) / 5.0), START_UUTC, 256.0)
        # The app learns these and registers them before anything else.
        privacy.register_identity(NAME_1, NAME_2, SUBJECT_ID, PASSWORD, path.name)

        log = logging.getLogger("mef3io_studio.test")
        # 1. a successful job through the job layer
        ok = jobs.submit(
            "Opening", summarize_recording, LocalRecordingSource(), str(path), PASSWORD
        )
        with qtbot.waitSignal(ok.finished, timeout=10_000):
            pass
        assert ok.record.status.name == "SUCCEEDED"

        # 2. a failing job whose exception text carries the path and a date
        def leaky(ctx):
            raise RuntimeError(f"cannot read {path} recorded {DATE_FORMS[1]} for {NAME_1}")

        bad = jobs.submit("Failing", leaky)
        with qtbot.waitSignal(bad.finished, timeout=10_000):
            pass
        # 3. careless log calls: formatted args, f-strings, exceptions, warnings
        log.info("subject %s %s (%s) password %s", NAME_1, NAME_2, SUBJECT_ID, PASSWORD)
        for form in DATE_FORMS:
            log.debug(f"start={form} who={NAME_2.upper()}")
        try:
            raise ValueError(f"{NAME_1} {START_UUTC} {path}")
        except ValueError:
            log.exception("handled %s", DATE_FORMS[0])
        import warnings

        warnings.warn(f"odd metadata for {SUBJECT_ID} at {DATE_FORMS[5]}", stacklevel=1)
    finally:
        teardown_logging()
        privacy.identities.clear()

    text = log_file.read_text(encoding="utf-8")
    assert "[redacted]" in text and "[time]" in text  # the paths above did log
    lowered = text.lower()
    for secret in (NAME_1, NAME_2, SUBJECT_ID, PASSWORD, path.name):
        assert secret.lower() not in lowered, secret
    for form in DATE_FORMS:
        assert form not in text, form
