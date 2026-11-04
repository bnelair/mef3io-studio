"""Join or trim, anonymous and as one file: the whole pipeline on small
recordings the tests write themselves (made-up signals and an invented
patient; never real data).

Every sample value encodes its own time, so a read of the result says
exactly which part was kept."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import mef3io
import numpy as np
import pytest
from explorer_sessions import FIRST, LAST, PATIENT_ID, SITE, metadata

from mef3io_studio.model import join as j
from mef3io_studio.model import pipeline
from mef3io_studio.model.cancel import CancelToken, JobCancelled
from mef3io_studio.model.inspect import inspect_recording

T0 = 946_684_800_000_000
S = 1_000_000
HOUR = 3600 * S
DAY = 86_400 * S
FS = 2.0
SHIFT = -3650 * DAY


def write(
    path: Path, start: int, seconds: int = 7200, channels=("a1", "a2", "a3"), notes=True
) -> Path:
    n = int(seconds * FS)
    values = (start - T0) / S + np.arange(n) / FS
    with mef3io.Writer(str(path), overwrite=True, metadata=metadata()) as w:
        for name in channels:
            w.write(name, values, start, FS)
        if notes:
            w.write_annotations([{"time": start + 10 * S, "text": f"{LAST} moved at {SITE}"}])
    return path


def fingerprint(*paths: Path) -> dict:
    out = {}
    for path in paths:
        for root, _dirs, files in os.walk(path):
            for name in sorted(files):
                full = os.path.join(root, name)
                with open(full, "rb") as f:
                    out[full] = hashlib.sha256(f.read()).hexdigest()
    return out


def two_sources(tmp_path: Path, notes=True):
    a = write(tmp_path / "A.mefd", T0, notes=notes)
    b = write(tmp_path / "B.mefd", T0 + 3 * HOUR, notes=notes)
    return a, b


def request_for(a, b, destination, anonymous=None, one_file=False, exclude=("a3",), keep_a=None):
    keep_a = keep_a or j.Keep(j.KEEP_FIRST, HOUR)
    return j.new_request(
        [
            j.JoinSource(str(a), str(a), keep_a),
            j.JoinSource(str(b), str(b), j.Keep(j.KEEP_WHOLE)),
        ],
        str(destination),
        exclude_channels=tuple(exclude),
        anonymous=anonymous,
        one_file=one_file,
    )


def note_texts(path) -> list[str]:
    reader = mef3io.Reader(str(path))
    return [str(r.get("text") or "") for r in reader.records(level="all")]


def leftovers(folder: Path) -> list[str]:
    return [n for n in os.listdir(folder) if n.startswith(j.PARTS_PREFIX) or n.endswith(".partial")]


def test_anonymous_one_file_trimmed_and_joined(tmp_path):
    a, b = two_sources(tmp_path)
    before = fingerprint(a, b)
    out = tmp_path / "out"
    out.mkdir()
    dst = out / "SUBJ-001.mefd.tar"
    request = request_for(a, b, dst, j.Anonymous(shift_us=SHIFT, pseudonym="P-1"), one_file=True)
    assert j.check_join(request) == []
    sent: list[j.JoinProgress] = []
    result = j.make_joined(request, send=sent.append)
    assert result.ok, result.message
    assert result.anonymous and result.one_file
    # One file, nothing else left in the folder, originals untouched.
    assert os.listdir(out) == ["SUBJ-001.mefd.tar"]
    assert fingerprint(a, b) == before
    # It opens (the tar is read in place), holds only the kept channels, and
    # only the kept time, moved by the shift.
    reader = mef3io.Reader(str(dst))
    assert reader.channels == ["a1", "a2"]
    found = mef3io.time_range(str(dst))
    assert found.start_uutc == T0 + SHIFT
    assert found.end_uutc == T0 + 3 * HOUR + 2 * HOUR + SHIFT
    first = np.asarray(reader.read("a1"), dtype=float)
    assert np.nanmin(first) == 0.0  # the first hour of A starts the recording
    # No patient detail, no original name, no note text.
    details = inspect_recording(str(dst)).details
    assert details is not None
    texts = [
        details.patient.first_name,
        details.patient.last_name,
        details.patient.patient_id,
        details.patient.location,
    ]
    assert not any(texts)
    assert all(not t for t in note_texts(dst))
    blob = b"".join(open(p, "rb").read() for p in [dst])
    for secret in (FIRST, LAST, PATIENT_ID, SITE):
        assert secret.encode() not in blob
    # The History says it was anonymized.
    summary = mef3io.inspect(str(dst), strict=False, include_values=True)
    ops = summary.channels[0].segments[0].provenance["operations"]
    assert "anonymize" in ops
    # Progress runs from the first stage to the last, in order, never back.
    kinds = [m.kind for m in sent]
    assert kinds[0] == "cut" and kinds[-1] == "check" and "join" in kinds and "pack" in kinds
    overall = [m.overall for m in sent]
    assert overall == sorted(overall) and 0.0 <= overall[0] and overall[-1] <= 1.0
    assert all(m.anonymous for m in sent)
    assert "anonymous" in j.progress_phase(sent[0])


def test_anonymous_folder_keeps_dates_and_text_when_asked(tmp_path):
    a, b = two_sources(tmp_path)
    dst = tmp_path / "SUBJ-002.mefd"
    anon = j.Anonymous(shift_us=None, text="keep")
    result = j.make_joined(request_for(a, b, dst, anon))
    assert result.ok, result.message
    assert mef3io.time_range(str(dst)).start_uutc == T0
    texts = note_texts(dst)
    assert any("moved" in t for t in texts)
    # The names mef3io knows are taken out of the text that is kept.
    assert not any(LAST in t or SITE in t for t in texts)
    assert not [n for n in os.listdir(tmp_path) if n.startswith(j.PARTS_PREFIX)]


def test_single_recording_anonymous_as_one_file(tmp_path):
    a = write(tmp_path / "A.mefd", T0)
    out = tmp_path / "out"
    out.mkdir()
    dst = out / "SUBJ-003.mefd.tar"
    request = j.new_request(
        [j.JoinSource(str(a), str(a), j.Keep(j.KEEP_LAST, HOUR))],
        str(dst),
        anonymous=j.Anonymous(shift_us=SHIFT),
        one_file=True,
    )
    assert j.check_join(request) == []
    result = j.make_joined(request)
    assert result.ok, result.message
    assert os.listdir(out) == ["SUBJ-003.mefd.tar"]
    found = mef3io.time_range(str(dst))
    assert (found.start_uutc, found.end_uutc) == (T0 + HOUR + SHIFT, T0 + 2 * HOUR + SHIFT)


def test_not_anonymous_as_one_file_is_unchanged_except_the_file(tmp_path):
    # No notes: with notes, a trimmed part joined to another fails mef3io's
    # check (provenance_UUID) also WITHOUT this feature (mef3io 2.1.1).
    a, b = two_sources(tmp_path, notes=False)
    dst = tmp_path / "joined.mefd.tar"
    request = request_for(a, b, dst, None, one_file=True)
    assert j.check_join(request) == []
    result = j.make_joined(request)
    assert result.ok, result.message
    assert not result.anonymous and result.one_file
    details = inspect_recording(str(dst)).details
    assert details.patient.first_name == FIRST
    assert not leftovers(tmp_path)


def test_the_new_name_may_not_hold_a_patient_detail(tmp_path):
    a, b = two_sources(tmp_path)
    dst = tmp_path / f"{LAST}-copy.mefd.tar"
    request = request_for(a, b, dst, j.Anonymous(shift_us=SHIFT), one_file=True)
    findings = j.check_join(request)
    assert findings and findings[0].key is not None
    assert not dst.exists() and not leftovers(tmp_path)


def test_a_part_that_cannot_be_joined_is_found_before_anything_is_written(tmp_path):
    a = write(tmp_path / "A.mefd", T0)
    n = int(7200 * 4.0)
    b = tmp_path / "B.mefd"
    with mef3io.Writer(str(b), overwrite=True, metadata=metadata()) as w:
        w.write("a1", np.zeros(n), T0 + 3 * HOUR, 4.0)  # another sampling rate
    request = request_for(a, b, tmp_path / "SUBJ-004.mefd", j.Anonymous(shift_us=SHIFT))
    findings = j.check_join(request)
    assert findings
    assert not [p for p in os.listdir(tmp_path) if p not in ("A.mefd", "B.mefd")]


@pytest.mark.parametrize("one_file", [False, True])
@pytest.mark.parametrize("stage", ["cut", "join", "pack", "check"])
def test_stop_at_every_stage_leaves_nothing_behind(tmp_path, stage, one_file):
    if stage in ("pack", "check") and not one_file:
        pytest.skip("only one file is packed")
    a, b = two_sources(tmp_path)
    before = fingerprint(a, b)
    out = tmp_path / "out"
    out.mkdir()
    name = "SUBJ-005.mefd.tar" if one_file else "SUBJ-005.mefd"
    request = request_for(a, b, out / name, j.Anonymous(shift_us=SHIFT), one_file=one_file)
    token = CancelToken()

    def send(message):
        if message.kind == stage:
            token.cancel()

    with pytest.raises(JobCancelled):
        j.make_joined(request, send=send, cancel=token)
    assert os.listdir(out) == []
    assert not leftovers(tmp_path)
    assert fingerprint(a, b) == before


def test_not_enough_room_is_refused_with_how_much_is_needed(tmp_path, monkeypatch):
    a, b = two_sources(tmp_path)
    monkeypatch.setattr(pipeline, "free_bytes", lambda folder: 1000)
    out = tmp_path / "out"
    out.mkdir()
    request = request_for(
        a, b, out / "SUBJ-006.mefd.tar", j.Anonymous(shift_us=SHIFT), one_file=True
    )
    findings = j.check_join(request)
    assert len(findings) == 1 and findings[0].key is None
    assert (
        "free" in findings[0].message.text
        and " B" in findings[0].message.text
        or "KB" in findings[0].message.text
        or "MB" in findings[0].message.text
    )
    result = j.make_joined(request)
    assert not result.ok and result.message.title == "There is not enough free space"
    assert os.listdir(out) == [] and not leftovers(tmp_path)


def test_the_worst_moment_counts_the_file_and_what_it_is_packed_from(tmp_path):
    a, b = two_sources(tmp_path)
    layouts, _ = j.read_layouts(request_for(a, b, tmp_path / "x.mefd"), CancelToken())
    folder = request_for(a, b, tmp_path / "x.mefd", j.Anonymous(shift_us=SHIFT))
    one = request_for(a, b, tmp_path / "x.mefd.tar", j.Anonymous(shift_us=SHIFT), one_file=True)
    plan = j.plan_for(folder, layouts)
    s_folder, s_one = j.sizing(folder, plan, layouts), j.sizing(one, plan, layouts)
    assert s_folder.total == s_one.total > 0
    # Parts + joined result (about 2x) for a folder; the file adds nothing
    # to that peak because the parts are removed before packing.
    assert s_folder.peak >= 2 * s_folder.total * 0.99
    assert s_one.peak >= s_folder.peak


def test_an_existing_file_is_never_replaced(tmp_path):
    a, b = two_sources(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    dst = out / "SUBJ-007.mefd.tar"
    request = request_for(a, b, dst, j.Anonymous(shift_us=SHIFT), one_file=True)
    dst.write_bytes(b"not yours to replace")
    result = j.make_joined(request)
    assert not result.ok
    assert dst.read_bytes() == b"not yours to replace"
    assert os.listdir(out) == ["SUBJ-007.mefd.tar"]


# -- review round 1 ------------------------------------------------------------------


def pack_as_tar(folder: Path) -> Path:
    import tarfile

    tar = folder.with_name(folder.name + ".tar")
    with tarfile.open(tar, "w") as t:
        t.add(folder, arcname=folder.name)
    return tar


def test_anonymous_join_accepts_a_leave_out_that_only_some_recordings_have(tmp_path):
    # A has a1 a2 a3, B has a1 a2 b9: leaving out a3 and b9 is fine for a
    # normal join, so it is fine for an anonymous one (a folder and a file).
    a = write(tmp_path / "A.mefd", T0, channels=("a1", "a2", "a3"))
    b_folder = write(tmp_path / "B.mefd", T0 + 3 * HOUR, channels=("a1", "a2", "b9"))
    b = pack_as_tar(b_folder)
    before = fingerprint(a, b_folder)
    dst = tmp_path / "SUBJ-010.mefd"
    request = j.new_request(
        [
            j.JoinSource(str(a), str(a), j.Keep(j.KEEP_WHOLE)),
            j.JoinSource(str(b), str(b), j.Keep(j.KEEP_WHOLE)),
        ],
        str(dst),
        exclude_channels=("a3", "b9"),
        anonymous=j.Anonymous(shift_us=SHIFT),
    )
    assert j.check_join(request) == []
    result = j.make_joined(request)
    assert result.ok, result.message
    assert set(mef3io.Reader(str(dst)).channels) == {"a1", "a2"}
    assert fingerprint(a, b_folder) == before


def test_a_channel_in_none_of_the_recordings_is_still_refused(tmp_path):
    a = write(tmp_path / "A.mefd", T0, channels=("a1", "a2", "a3"))
    b = write(tmp_path / "B.mefd", T0 + 3 * HOUR, channels=("a1", "a2", "b9"))
    request = request_for(a, b, tmp_path / "SUBJ-011.mefd", j.Anonymous(shift_us=SHIFT),
                          exclude=("nowhere",), keep_a=j.Keep(j.KEEP_WHOLE))
    assert j.check_join(request)
    assert not (tmp_path / "SUBJ-011.mefd").exists()


def everything_in(path: Path) -> tuple[bytes, list[str]]:
    """All bytes and all names (files and folders) under a path or of a file."""
    path = Path(path)
    if path.is_file():
        return path.read_bytes(), [path.name]
    blob, names = b"", []
    for root, dirs, files in os.walk(path):
        names += list(dirs) + list(files)
        for f in files:
            blob += (Path(root) / f).read_bytes()
    return blob, names


@pytest.mark.parametrize("one_file", [False, True])
def test_nothing_of_the_originals_is_in_the_result(tmp_path, one_file):
    a = write(tmp_path / "Quartz-night1.mefd", T0)
    b = write(tmp_path / "Quartz-night2.mefd", T0 + 3 * HOUR)
    out = tmp_path / "out"
    out.mkdir()
    dst = out / ("SUBJ-012.mefd.tar" if one_file else "SUBJ-012.mefd")
    request = request_for(a, b, dst, j.Anonymous(shift_us=SHIFT), one_file=one_file)
    result = j.make_joined(request)
    assert result.ok, result.message
    blob, names = everything_in(dst)
    secrets = [FIRST, LAST, PATIENT_ID, SITE, "Quartz", "night1", "night2", str(tmp_path)]
    for secret in secrets:
        assert secret.encode() not in blob, secret
        assert not any(secret in n for n in names), secret
    # The folder holds only the recording's own files.
    assert os.listdir(out) == [dst.name]


@pytest.mark.parametrize("one_file", [False, True])
def test_a_failure_in_the_join_leaves_nothing_behind(tmp_path, monkeypatch, one_file):
    a, b = two_sources(tmp_path)
    before = fingerprint(a, b)
    out = tmp_path / "out"
    out.mkdir()
    dst = out / ("SUBJ-013.mefd.tar" if one_file else "SUBJ-013.mefd")

    def boom(*args, **kwargs):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(mef3io, "merge_sessions", boom)
    result = j.make_joined(request_for(a, b, dst, j.Anonymous(shift_us=SHIFT), one_file=one_file))
    assert not result.ok
    assert os.listdir(out) == []
    assert not leftovers(tmp_path)
    assert fingerprint(a, b) == before


def test_a_failure_in_the_packing_leaves_nothing_behind(tmp_path, monkeypatch):
    a, b = two_sources(tmp_path)
    before = fingerprint(a, b)
    out = tmp_path / "out"
    out.mkdir()
    dst = out / "SUBJ-014.mefd.tar"

    def boom(*args, **kwargs):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(j.archive, "run_in_helper", boom)
    result = j.make_joined(request_for(a, b, dst, j.Anonymous(shift_us=SHIFT), one_file=True))
    assert not result.ok
    assert os.listdir(out) == []
    assert not leftovers(tmp_path)
    assert fingerprint(a, b) == before


@pytest.mark.parametrize(
    "bad",
    [
        "A",  # an original's name
        "a",  # other case
        " A ",  # spaces around it
        "A.mefd",  # typed with the folder ending
        "A.mefd.tar",  # typed with the one-file ending
        PATIENT_ID,
        FIRST,
        SITE,
    ],
)
def test_the_new_name_may_not_be_an_original_or_a_patient_detail(tmp_path, bad):
    a, b = two_sources(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    request = request_for(a, b, out / f"{bad}.mefd", j.Anonymous(shift_us=SHIFT))
    assert j.check_join(request), bad
    assert os.listdir(out) == []


@pytest.mark.parametrize("pseudonym", [f"P-{PATIENT_ID}", f"{LAST}-1", f"x{FIRST}x", f"{SITE}-p"])
def test_the_pseudonym_may_not_hold_a_patient_detail(tmp_path, pseudonym):
    a, b = two_sources(tmp_path)
    anon = j.Anonymous(shift_us=SHIFT, pseudonym=pseudonym)
    request = request_for(a, b, tmp_path / "SUBJ-015.mefd", anon)
    assert j.check_join(request), pseudonym
    assert not (tmp_path / "SUBJ-015.mefd").exists()


def test_one_file_alone_counts_as_a_change(tmp_path):
    # One recording, kept whole, nothing left out, not anonymous: packing it
    # into one file is a real result, not an exact copy.
    a = write(tmp_path / "A.mefd", T0, notes=False)
    out = tmp_path / "out"
    out.mkdir()

    def req(dst, one_file):
        return j.new_request(
            [j.JoinSource(str(a), str(a), j.Keep(j.KEEP_WHOLE))], str(dst), one_file=one_file
        )

    assert not req(out / "x.mefd", False).changes
    assert j.check_join(req(out / "x.mefd", False))  # still an exact copy: refused
    request = req(out / "x.mefd.tar", True)
    assert request.changes
    assert j.check_join(request) == []
    result = j.make_joined(request)
    assert result.ok, result.message
    assert os.listdir(out) == ["x.mefd.tar"]


def _zero_channel_request(tmp_path, anonymous):
    # B has only b9, and b9 is left out: B would keep no channel at all.
    a = write(tmp_path / "A.mefd", T0, channels=("a1", "a2"), notes=False)
    b = write(tmp_path / "B.mefd", T0 + 3 * HOUR, channels=("b9",), notes=False)
    out = tmp_path / "out"
    out.mkdir()
    request = request_for(a, b, out / "SUBJ-020.mefd", anonymous, exclude=("b9",))
    return request, out


@pytest.mark.parametrize("anonymous", [None, j.Anonymous(shift_us=SHIFT)])
def test_a_recording_left_with_no_channel_is_refused_plainly(tmp_path, anonymous):
    # Anonymous or not, the same plain refusal up front; nothing is written.
    request, out = _zero_channel_request(tmp_path, anonymous)
    findings = j.check_join(request)
    assert findings
    assert "Every channel" in findings[0].message.detail or "Every channel" in str(findings[0].message)
    assert findings[0].key == str(tmp_path / "B.mefd")
    result = j.make_joined(request)
    assert not result.ok
    assert os.listdir(out) == []
    assert not leftovers(tmp_path)
