"""No patient details, paths or absolute dates ever reach the log file."""

import logging
import re
import warnings
from pathlib import Path

import mef3io
import numpy as np
import pytest

from mef3io_studio.common import privacy
from mef3io_studio.common.logging_setup import (
    CRASH_FILE_NAME,
    LOG_FILE_NAME,
    recover_crash_report,
    setup_logging,
    teardown_logging,
)
from mef3io_studio.common.privacy import IdentityRegistry, scrub
from mef3io_studio.model.source import LocalRecordingSource
from mef3io_studio.workers.tasks import summarize_recording

# Synthetic identity and a synthetic recording date (never real data).
NAME_1, NAME_2, SUBJECT_ID = "Zebulon", "Quartermaine", "MRN-55501234"
ACCENTED = "Müller"
PASSWORD = "Hunter2secret"
START_UUTC = 1_552_555_613_000_000  # 2019-03-14 09:26:53 UTC
DATE_FORMS = [
    "2019-03-14",
    "2019-03-14T09:26:53Z",
    "2019_03_14",
    "14/03/2019",
    "03/14/2019",
    "14.03.2019",
    "14.3.19",
    "14 03 2019",
    "March 14, 2019",
    "14 Mar 2019",
    "20190314",
    "2019-W11-4",
    "Thu Mar 14 09:26:53 2019",
    "Thu, 14 Mar 2019 09:26:53 +0000",
    "datetime.datetime(2019, 3, 14, 9, 26, 53)",
    "datetime.date(2019, 3, 14)",
    "09:26:53",
    "Mar-14-2019",
    "March 2019",
    "14. März 2019",
    "14 mars 2019",
    "14 de marzo de 2019",
    "14 marzo 2019",
    "14. března 2019",
    "14 marca 2019",
    "Do 14 Mär 2019 09:26:53 CET",
    "2019年3月14日",
    "2019年3月14日 09時26分",
    "on 14/03 at 09:26",
    "3/14/2019 9:26 AM",
    "2019-03-14T09:26:53.589+01:00",
    "946684800",
    "1552555613",
    str(START_UUTC // 1000),
    str(START_UUTC),
    str(-START_UUTC),
]
# What a careless line could carry: paths with names and dates in them.
PATH_FORMS = [
    r"C:\Users\Zebulon Quartermaine\Recordings\visit.mefd",
    r"\\server\share\Quartermaine\rec.mefd.tar",
    "/data/PT0042_2019_03_14.mefd",
    "PT0042_2019_03_14.mefd",
    "sub-0042_ses-20190314",
    "'/data/clinic/Jane Roe/raw'",
    "PosixPath('/srv/eeg/Doe John/session')",
    "/Volumes/Patient Jane Doe/rec.mefd",
    "/data/Jane Marie Doe/rec.mefd",
    r"C:\Users\Jane Doe\Desktop\Smith John Q\x",
    r"open \\fileserver\EEG\Jane Doe\rec.mefd failed",
    "file:///home/someone/Jane%20Doe/rec.mefd",
    "%2Fdata%2FJane%20Doe%2Frec.mefd",
    "Пациенты/Иванов_Иван/запись",
    "患者/山田太郎/記録",
    "QFile::open: cannot open Пациенты/Петров",
    "dir 12345678/0001 missing",
    "~jdoe/x",
    "JaneDoe-000001.segd",
    "JaneDoe.timd",
    "JaneDoe-000001.tdat",
    "JaneDoe-000001.tmet",
    "JaneDoe-000001.tidx",
    "JaneDoe.rdat",
    "JaneDoe.ridx",
    "JaneDoe-rec.MEFD",
    "jane.doe@example.org",
]
# Words that must be gone from the scrubbed text of PATH_FORMS.
PATH_WORDS = (
    "Quartermaine",
    "Roe",
    "Doe",
    "0042",
    "2019",
    "Users",
    "share",
    "Jane",
    "Marie",
    "Smith",
    "Иванов",
    "Петров",
    "Пациенты",
    "山田",
    "患者",
    "12345678",
    "jdoe",
    "fileserver",
)
# Identity spelled differently from how it was registered.
IDENTITY_VARIANTS = [
    "ZEBULON",
    "zebulon_quartermaine",
    "Muller",
    "MÜLLER",
    "Mu\u0308ller",  # decomposed (NFD)
    "MRN55501234",
    "MRN 55501234",
]


@pytest.mark.parametrize("form", DATE_FORMS)
def test_scrub_removes_every_date_form(form):
    out = scrub(f"recording starts {form} here", IdentityRegistry())
    assert form not in out
    for digits in ("2019", "1552555613"):
        assert digits not in out


@pytest.mark.parametrize("form", PATH_FORMS)
def test_scrub_removes_paths(form):
    out = scrub(f"cannot open {form} now", IdentityRegistry())
    assert "<path>" in out or "<email>" in out
    for word in PATH_WORDS:
        assert word.lower() not in out.lower(), out


def test_scrub_removes_home_folder():
    home = str(Path.home())
    assert home not in scrub(f"log at {home} end", IdentityRegistry())


@pytest.mark.parametrize(
    "text",
    [
        "mef3io 1.1.4",
        "Python 3.12.7",
        "Python 3.12.12",
        "Qt 6.12.0",
        "app 0.0.1, mef3io 1.1.4, Qt 6.12.0",
        "job 42",
        "256.0 Hz",
        "3/10 done",
        "channel Fp1 of 64",
        "QFile::open: cannot open",
    ],
)
def test_scrub_keeps_ordinary_text(text):
    assert scrub(text, IdentityRegistry()) == text


@pytest.mark.parametrize("variant", IDENTITY_VARIANTS)
def test_identity_variants_are_redacted(variant):
    registry = IdentityRegistry()
    registry.register(NAME_1, NAME_2, SUBJECT_ID, ACCENTED)
    out = scrub(f"subject {variant} seen", registry)
    assert re.fullmatch(r"subject (?:\[redacted\][\W_]*)+ seen", out), out


def test_short_strings_are_not_registered():
    registry = IdentityRegistry()
    registry.register("", None, "x", "-")
    assert scrub("x marks", registry) == "x marks"


def test_reader_is_opened_without_path_bearing_warnings(monkeypatch, synthetic_session):
    seen = {}
    real = mef3io.Reader

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(mef3io, "Reader", spy)
    LocalRecordingSource().summarize(synthetic_session)
    assert seen.get("warn_declarations") is False


def test_real_hooks_scrub_everything(tmp_path):
    """The reviewer's end-to-end probe: uncaught exceptions in the main thread
    and in a worker thread, a warning and a Qt message, through the hooks the
    app really installs."""
    import sys
    import threading

    from PySide6.QtCore import qInstallMessageHandler, qWarning

    from mef3io_studio.common.logging_setup import install_qt_message_handler, uninstall_hooks

    log_file = setup_logging(tmp_path / "logs", hooks=True)
    install_qt_message_handler()
    try:
        try:
            mef3io.Reader("/Volumes/Patient Jane Doe/Иванов Иван/rec.mefd")
        except Exception:
            sys.excepthook(*sys.exc_info())
        thread = threading.Thread(
            target=lambda: open("/srv/eeg/Smith John Q/14. März 2019/x.mefd")  # noqa: SIM115
        )
        thread.start()
        thread.join()
        warnings.warn("loaded 山田太郎/記録 recorded Mar-14-2019", stacklevel=1)
        qWarning("QFile::open: cannot open Пациенты/Петров")
        logging.getLogger("mef3io_studio").info(
            "started; Python 3.12.12, epoch 946684800, on 14/03 at 09:26, JaneDoe-000001.segd"
        )
    finally:
        qInstallMessageHandler(None)
        uninstall_hooks()
        teardown_logging()
    text = log_file.read_text(encoding="utf-8")
    assert "uncaught IoError" in text and "uncaught FileNotFoundError" in text
    assert "QFile::open: cannot open <path>" in text
    assert "Python 3.12.12" in text
    for word in (
        "Jane",
        "Doe",
        "Иванов",
        "Smith",
        "März",
        "2019",
        "山田",
        "記録",
        "Пациенты",
        "Петров",
        "946684800",
        "09:26",
        "JaneDoe",
    ):
        assert word not in text, word


def test_crash_report_lives_outside_the_log_folder():
    from mef3io_studio.common.logging_setup import crash_dir, log_dir

    assert log_dir().resolve() not in crash_dir().resolve().parents
    assert crash_dir().resolve() != log_dir().resolve()


def test_crash_report_is_scrubbed_into_the_log(tmp_path):
    log_file = setup_logging(tmp_path)
    try:
        (tmp_path / CRASH_FILE_NAME).write_text(
            f'Fatal Python error\n  File "/home/{NAME_2}/x.py", line 1, at {DATE_FORMS[1]}\n'
        )
        recover_crash_report(tmp_path)
    finally:
        teardown_logging()
    text = log_file.read_text()
    assert "stopped unexpectedly" in text
    assert NAME_2 not in text and DATE_FORMS[1] not in text
    assert not (tmp_path / CRASH_FILE_NAME).exists()


def test_log_file_never_contains_identity_paths_or_dates(qtbot, jobs, tmp_path):
    log_dir = tmp_path / "logs"
    log_file = setup_logging(log_dir, level=logging.DEBUG)
    assert log_file == log_dir / LOG_FILE_NAME
    privacy.identities.clear()
    try:
        # A session whose metadata and folder name carry the identity.
        path = tmp_path / f"{NAME_2}_{NAME_1}_2019_03_14.mefd"
        meta = mef3io.Metadata(subject=mef3io.Subject(name_1=NAME_1, name_2=NAME_2, id=SUBJECT_ID))
        with mef3io.Writer(str(path), metadata=meta, password1="L1pass", password2=PASSWORD) as w:
            w.write("c1", np.sin(np.arange(1024) / 5.0), START_UUTC, 256.0)
        # The app learns these and registers them before anything else.
        privacy.register_identity(NAME_1, NAME_2, SUBJECT_ID, PASSWORD, ACCENTED)

        log = logging.getLogger("mef3io_studio.test")
        # 1. a successful job through the job layer (helper process)
        ok = jobs.submit(
            "Opening", summarize_recording, LocalRecordingSource(), str(path), PASSWORD
        )
        with qtbot.waitSignal(ok.finished, timeout=30_000):
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
        for form in DATE_FORMS + PATH_FORMS + IDENTITY_VARIANTS:
            log.debug(f"value={form} who={NAME_2.upper()}")
        log.info("path object %r", path)
        try:
            raise ValueError(f"{NAME_1} {START_UUTC} {path}")
        except ValueError:
            log.exception("handled %s", DATE_FORMS[0])
        # mef3io's own declaration warning starts with the full path
        warnings.warn(
            mef3io.SessionDeclarationWarning(f"{path}: metadata section 2 leaves ..."),
            stacklevel=1,
        )
        warnings.warn(f"odd metadata for {SUBJECT_ID} at {DATE_FORMS[8]}", stacklevel=1)
    finally:
        teardown_logging()
        privacy.identities.clear()

    text = log_file.read_text(encoding="utf-8")
    assert "[redacted]" in text and "[time]" in text and "<path>" in text
    lowered = text.lower()
    for secret in (NAME_1, NAME_2, SUBJECT_ID, PASSWORD, "muller", "55501234", path.name):
        assert secret.lower() not in lowered, secret
    for form in DATE_FORMS + PATH_FORMS:
        assert form not in text, form
    assert str(tmp_path) not in text
    assert "2019" not in text


def test_hidden_characters_do_not_split_an_identity():
    from mef3io_studio.common.privacy import IdentityRegistry

    registry = IdentityRegistry()
    registry.register("Smith", "Müller")
    for text in ("Sm­ith", "Sm​ith", "Müller", "MU‍LLER"):
        assert registry.redact(f"x {text} y") == "x [redacted] y", text


def test_registering_many_identities_stays_cheap():
    """Each value is compiled once, on its own: the cost per registration
    must not grow with how many are already known (it holds the GIL)."""
    import time

    from mef3io_studio.common.privacy import IdentityRegistry

    registry = IdentityRegistry()
    for i in range(1000):
        registry.register(f"Person Number {i} Example")
    start = time.perf_counter()
    registry.register("One More Person Name")
    assert time.perf_counter() - start < 0.05
    assert registry.redact("One More Person Name") == "[redacted]"
