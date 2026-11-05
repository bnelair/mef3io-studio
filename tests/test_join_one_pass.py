"""Join or trim in ONE mef3io call (``merge_sessions`` from the originals):
the notes-and-events choice, the whole pipeline end to end, the refusal words
for the codes the merge added, Stop in the middle, and the originals left
untouched. Synthetic recordings only (made-up signals and an invented
patient)."""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

import mef3io
import numpy as np
import pytest
from explorer_sessions import FIRST, LAST, SITE, metadata
from test_join_anonymous import FS, HOUR, SHIFT, T0, fingerprint, note_texts, write

from mef3io_studio.model import join as j
from mef3io_studio.model.cancel import CancelToken, JobCancelled
from mef3io_studio.model.inspect import inspect_recording
from mef3io_studio.model.refusals import JOIN_WORDS

S = 1_000_000


def three(tmp_path: Path):
    a = write(tmp_path / "A.mefd", T0)
    b = write(tmp_path / "B.mefd", T0 + 3 * HOUR)
    c = write(tmp_path / "C.mefd", T0 + 6 * HOUR)
    return a, b, c


def window_request(tmp_path, destination, window=(T0 + HOUR, T0 + 7 * HOUR), **kwargs):
    a, b, c = three(tmp_path)
    keep = j.Keep(j.KEEP_BETWEEN, start_us=window[0], end_us=window[1])
    return (
        j.new_request(
            [j.JoinSource(str(p), str(p), keep) for p in (a, b, c)],
            str(destination),
            **kwargs,
        ),
        (a, b, c),
    )


def tar_names(path: Path) -> list[str]:
    with tarfile.open(path) as t:
        return t.getnames()


def has_note_files(path: Path) -> bool:
    if path.is_file():
        names = tar_names(path)
    else:
        names = [n for _r, _d, f in os.walk(path) for n in f]
    return any(n.endswith((".rdat", ".ridx")) for n in names)


def test_anonymous_leave_out_notes_one_file_end_to_end(tmp_path):
    """3 recordings, a channel left out, a window, anonymous, no notes, one
    file: the file opens, holds no notes at any level, only the kept
    channels, exactly the window's data, moved by the shift."""
    out = tmp_path / "out"
    out.mkdir()
    dst = out / "SUBJ-100.mefd.tar"
    request, (a, b, c) = window_request(
        tmp_path,
        dst,
        exclude_channels=("a3",),
        anonymous=j.Anonymous(shift_us=SHIFT, pseudonym="P-100"),
        one_file=True,
        records=j.RECORDS_DROP,
    )
    before = fingerprint(a, b, c)
    assert has_note_files(a)  # the originals do have notes
    assert j.check_join(request) == []
    result = j.make_joined(request)
    assert result.ok, result.message
    assert result.notes_left_out and result.anonymous and result.one_file
    assert result.records_dropped >= 1
    assert os.listdir(out) == ["SUBJ-100.mefd.tar"]
    assert fingerprint(a, b, c) == before
    # Opens in place; kept channels only; no notes at any level.
    reader = mef3io.Reader(str(dst))
    assert reader.channels == ["a1", "a2"]
    assert not has_note_files(dst)
    assert note_texts(dst) == []
    # Span = window ∩ data, shifted: A's last hour .. C's first hour.
    found = mef3io.time_range(str(dst))
    assert found.start_uutc == T0 + HOUR + SHIFT
    assert found.end_uutc == T0 + 7 * HOUR + SHIFT
    data = np.asarray(reader.read("a1"), dtype=float)
    data = data[~np.isnan(data)]
    first = (T0 + HOUR - T0) / S
    assert data[0] == first and data[-1] == (T0 + 7 * HOUR - T0) / S - 1 / FS
    # Nothing of the patient anywhere.
    blob = Path(dst).read_bytes()
    for secret in (FIRST, LAST, SITE):
        assert secret.encode() not in blob


def test_keeping_the_notes_keeps_them(tmp_path):
    request, _ = window_request(
        tmp_path,
        tmp_path / "SUBJ-101.mefd",
        anonymous=j.Anonymous(shift_us=SHIFT),
    )
    assert request.records == j.RECORDS_KEEP
    result = j.make_joined(request)
    assert result.ok, result.message
    assert not result.notes_left_out and result.records_dropped == 0
    assert has_note_files(tmp_path / "SUBJ-101.mefd")
    # The default for an anonymous copy: the text is removed, the notes stay.
    texts = note_texts(tmp_path / "SUBJ-101.mefd")
    assert texts and all(not t for t in texts)


def test_not_anonymous_keeps_details_and_text_and_may_leave_notes_out(tmp_path):
    request, _ = window_request(tmp_path, tmp_path / "joined.mefd")
    assert j.make_joined(request).ok
    assert any(LAST in t or SITE in t for t in note_texts(tmp_path / "joined.mefd"))
    details = inspect_recording(str(tmp_path / "joined.mefd")).details
    assert details.patient.first_name == FIRST
    left_out, _ = window_request(tmp_path / "x", tmp_path / "x" / "bare.mefd", records="drop")
    assert left_out.changes  # leaving the notes out is a change on its own
    assert j.check_join(left_out) == []
    result = j.make_joined(left_out)
    assert result.ok and result.notes_left_out and not result.anonymous
    assert not has_note_files(tmp_path / "x" / "bare.mefd")
    assert inspect_recording(str(tmp_path / "x" / "bare.mefd")).details.patient.first_name == FIRST


@pytest.mark.parametrize("anonymous", [None, j.Anonymous(shift_us=SHIFT)])
def test_one_recording_leaves_the_notes_out_too(tmp_path, anonymous):
    a = write(tmp_path / "A.mefd", T0)
    dst = tmp_path / "part.mefd"
    request = j.new_request(
        [j.JoinSource(str(a), str(a), j.Keep(j.KEEP_FIRST, HOUR))],
        str(dst),
        anonymous=anonymous,
        records=j.RECORDS_DROP,
    )
    assert j.check_join(request) == []
    result = j.make_joined(request)
    assert result.ok, result.message
    assert result.notes_left_out and not has_note_files(dst)
    found = mef3io.time_range(str(dst))
    assert found.end_uutc - found.start_uutc == HOUR


def test_one_call_from_the_originals_with_every_option(tmp_path, monkeypatch):
    request, (a, b, c) = window_request(
        tmp_path,
        tmp_path / "SUBJ-102.mefd",
        exclude_channels=("a3",),
        output_password=("p1-new", "p2-new"),
        anonymous=j.Anonymous(shift_us=SHIFT, pseudonym="P-102", text="keep"),
        records=j.RECORDS_DROP,
    )
    calls = []
    real = mef3io.merge_sessions

    def spy(sources, dst, **kwargs):
        calls.append((list(sources), dst, kwargs))
        return real(sources, dst, **kwargs)

    monkeypatch.setattr(mef3io, "merge_sessions", spy)
    assert j.make_joined(request).ok
    assert len(calls) == 1
    sources, dst, kwargs = calls[0]
    assert sources == [str(a), str(b), str(c)] and dst == str(tmp_path / "SUBJ-102.mefd")
    assert kwargs["exclude_channels"] == ["a3"]
    assert kwargs["records"] == "drop" and kwargs["n_threads"] == 0 and kwargs["exact"] is True
    assert kwargs["anonymize"]["shift_us"] == SHIFT
    assert kwargs["anonymize"]["pseudonym"] == "P-102" and kwargs["anonymize"]["text"] == "keep"
    assert kwargs["time_ranges"] == [
        (T0 + HOUR, T0 + 2 * HOUR + 1),  # to the end: one microsecond past it
        None,  # B lies wholly inside the window
        (T0 + 6 * HOUR, T0 + 7 * HOUR),
    ]
    assert kwargs["output_password"] == ("p1-new", "p2-new")


def test_stop_in_the_middle_of_the_merge_leaves_nothing(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    request, (a, b, c) = window_request(
        tmp_path,
        out / "SUBJ-103.mefd",
        anonymous=j.Anonymous(shift_us=SHIFT),
        records=j.RECORDS_DROP,
    )
    before = fingerprint(a, b, c)
    token = CancelToken()
    seen = []

    def send(message):
        seen.append(message.kind)
        token.cancel()

    with pytest.raises(JobCancelled):
        j.make_joined(request, send=send, cancel=token)
    assert seen and set(seen) == {"join"}
    assert os.listdir(out) == []
    assert not [n for n in os.listdir(tmp_path) if n.startswith(".mef3io-studio")]
    assert fingerprint(a, b, c) == before


# --------------------------------------------------------------------------
# Refusals, in plain words


def test_a_window_in_a_gap_is_refused_as_holding_no_recording(tmp_path):
    # One recording with a gap of two hours inside it: a window in the gap
    # is inside its extent, so only mef3io can say there is no signal there.
    gap = tmp_path / "G.mefd"
    values = np.arange(int(HOUR / S * FS), dtype=float)
    with mef3io.Writer(str(gap), overwrite=True, metadata=metadata()) as w:
        w.write("a1", values, T0, FS)
        w.write("a1", values, T0 + 3 * HOUR, FS)
    other = write(tmp_path / "B.mefd", T0 + 5 * HOUR)
    keep = j.Keep(j.KEEP_BETWEEN, start_us=T0 + 90 * 60 * S, end_us=T0 + 150 * 60 * S)
    request = j.new_request(
        [
            j.JoinSource(str(gap), str(gap), keep),
            j.JoinSource(str(other), str(other), j.Keep(j.KEEP_FIRST, HOUR)),
        ],
        str(tmp_path / "x.mefd"),
    )
    findings = j.check_join(request)
    assert findings and findings[0].message.title == "The chosen part holds no recording"
    # Shown under the recording it is about (mef3io's single check names it).
    assert findings[0].key == str(gap)
    assert not (tmp_path / "x.mefd").exists()
    result = j.make_joined(request)
    assert not result.ok and result.message.title == "The chosen part holds no recording"


def test_a_notes_choice_that_is_not_valid_is_refused_plainly(tmp_path):
    request, _ = window_request(tmp_path, tmp_path / "x.mefd", records="neither")
    findings = j.check_join(request)
    assert findings
    assert findings[0].message.title == JOIN_WORDS["records-invalid"]({})[0]
    assert "neither" not in findings[0].message.text
    assert not j.make_joined(request).ok and not (tmp_path / "x.mefd").exists()


def _refusal_titles(report, request):
    paths, stems = j._paths_and_stems(request)
    return {r.code: j.refusal_message(r, paths, stems).title for r in report.refusals}


def test_the_codes_only_a_bad_call_can_raise_have_their_own_words(tmp_path):
    """The app never makes these calls, but if it ever did the person gets
    plain words (and the code), never a raw library message."""
    request, (a, b, c) = window_request(tmp_path, tmp_path / "x.mefd")
    paths = [str(a), str(b), str(c)]
    dst = str(tmp_path / "x.mefd")
    cases = {
        "merge-time-ranges-count": dict(time_ranges=[None]),
        "merge-anonymize-metadata": dict(anonymize=True, metadata={"subject_id": "x"}),
        "merge-threads-invalid": dict(n_threads=-1),
        "records-invalid": dict(records="neither"),
    }
    for code, kwargs in cases.items():
        report = mef3io.merge.check(paths, dst, **kwargs)
        titles = _refusal_titles(report, request)
        assert code in titles, (code, titles)
        title, text = JOIN_WORDS[code]({})
        assert titles[code] == title and "were not changed" in text
        assert not (tmp_path / "x.mefd").exists()


def test_leaving_the_notes_out_is_said_on_the_result(tmp_path):
    from mef3io_studio.joiner.page import saved_words

    request, _ = window_request(tmp_path, tmp_path / "x.mefd", records=j.RECORDS_DROP)
    result = j.make_joined(request)
    assert result.ok
    assert "All notes and events were left out." in saved_words(result)
    kept, _ = window_request(tmp_path / "k", tmp_path / "k" / "y.mefd")
    assert "notes and events" not in saved_words(j.make_joined(kept)).lower()
