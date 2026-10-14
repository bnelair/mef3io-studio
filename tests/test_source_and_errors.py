import errno

import mef3io
import numpy as np
import pytest

from mef3io_studio.model.cancel import CancelToken, JobCancelled
from mef3io_studio.model.errors import describe_error
from mef3io_studio.model.source import LocalRecordingSource, RecordingSource


def test_local_source_summarizes_without_reading_signal(synthetic_session):
    summary = LocalRecordingSource().summarize(synthetic_session)
    assert summary.channel_count == 3
    assert [c.name for c in summary.channels] == ["a1", "a2", "a3"]
    assert all(c.sampling_frequency == 256.0 for c in summary.channels)


def test_local_source_honours_cancel(synthetic_session):
    token = CancelToken()
    token.cancel()
    with pytest.raises(JobCancelled):
        LocalRecordingSource().summarize(synthetic_session, cancel=token)


def test_source_is_an_interface():
    with pytest.raises(TypeError):
        RecordingSource()  # abstract


def test_wrong_password_gives_plain_message(tmp_path):
    path = tmp_path / "locked.mefd"
    with mef3io.Writer(str(path), password1="one1", password2="two2") as w:
        w.write("c", np.zeros(256), 946_684_800_000_000, 256.0)
    with pytest.raises(mef3io.PasswordError) as info:
        LocalRecordingSource().summarize(path, password="wrong")
    message = describe_error(info.value)
    assert message.title == "Password needed"
    assert "password" in message.text.lower()
    assert "PasswordError" in message.detail


def test_missing_recording_gives_plain_message(tmp_path):
    with pytest.raises(mef3io.IoError) as info:
        LocalRecordingSource().summarize(tmp_path / "missing.mefd")
    assert describe_error(info.value).title == "The recording could not be opened"


@pytest.mark.parametrize(
    "exc,title",
    [
        (PermissionError("x"), "Access denied"),
        (FileNotFoundError("x"), "Not found"),
        (OSError(errno.ENOSPC, "full"), "The disk is full"),
        (MemoryError(), "Not enough memory"),
        (JobCancelled(), "Cancelled"),
        (ValueError("boom"), "Something went wrong"),
    ],
)
def test_describe_error_variants(exc, title):
    message = describe_error(exc)
    assert message.title == title
    # plain language: no exception class names in the user-facing text
    assert type(exc).__name__ not in message.text
