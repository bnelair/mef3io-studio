"""The viewer's data layer (model/viewer_source.py), on synthetic sessions
written by mef3io in a temporary folder. No real data, ever."""

from __future__ import annotations

from pathlib import Path

import mef3io
import numpy as np
import pytest

from mef3io_studio.model.cancel import CancelToken, JobCancelled
from mef3io_studio.model.errors import describe_error
from mef3io_studio.model.source import LocalRecordingSource
from mef3io_studio.model.viewer_source import TILE_SAMPLES, ViewerRefusal, open_recordings

T0 = 946_684_800_000_000
SEC = 1_000_000


def write(path: Path, channels, start, seconds, fs=256.0, seed=1, **kw) -> Path:
    rng = np.random.default_rng(seed)
    n = int(seconds * fs)
    with mef3io.Writer(str(path), overwrite=True, units="uV", **kw) as w:
        for name in channels:
            w.write(name, np.round(rng.normal(0, 50, n), 2), start, fs)
    return path


@pytest.fixture
def sequential(tmp_path):
    """Two recordings of the same channels, 60 s each, 120 s apart."""
    a = write(tmp_path / "day_one.mefd", ["Cz", "Fz"], T0, 60, seed=1)
    b = write(tmp_path / "day_two.mefd", ["Cz", "Fz"], T0 + 180 * SEC, 60, seed=2)
    return a, b


@pytest.fixture
def side_by_side(tmp_path):
    a = write(tmp_path / "left.mefd", ["C1", "C2"], T0, 60, seed=3)
    b = write(tmp_path / "right.mefd", ["C3", "C4"], T0, 60, seed=4)
    return a, b


def test_one_recording_uses_a_reader_alone(sequential):
    data = open_recordings([str(sequential[0])])
    try:
        assert isinstance(data._combined, mef3io.Reader)
        assert [r.letter for r in data.recordings] == ["A"]
        assert data.channels == ("Cz", "Fz")
        assert data.rate("Cz") == 256.0
        assert data.units("Cz") == "uV"
        assert data.timeline_start == T0
    finally:
        data.close()


def test_letters_follow_the_first_sample_not_the_given_order(sequential):
    a, b = sequential
    data = open_recordings([str(b), str(a)])
    try:
        assert isinstance(data._combined, mef3io.MultiReader)
        assert [r.name for r in data.recordings] == ["day_one", "day_two"]
        assert [r.letter for r in data.recordings] == ["A", "B"]
        assert [r.colour for r in data.recordings] == [0, 1]
        assert data.recordings[0].start_us == T0
        assert data.recordings[1].start_us == T0 + 180 * SEC
        assert data.timeline_end >= T0 + 239 * SEC
        assert sorted(data.identity_values()) == ["day_one", "day_two"]
    finally:
        data.close()


def test_ownership_of_sequential_recordings(sequential):
    data = open_recordings([str(p) for p in sequential])
    try:
        spans = data.ownership("Cz")
        assert [s[2] for s in spans] == [0, 1]
        assert spans[0][0] == T0 and abs(spans[0][1] - (T0 + 60 * SEC)) <= SEC // 100
        assert spans[1][0] == T0 + 180 * SEC
        assert data.recordings_at(T0, T0 + 10 * SEC) == [0]
        assert data.recordings_at(T0 + 100 * SEC, T0 + 110 * SEC) == []
        assert data.recordings_at(T0, T0 + 200 * SEC) == [0, 1]
        cov = data.segments_coverage("Cz")
        assert [c[2] for c in cov] == [0, 1]
        (b,) = data.boundaries()
        assert b.before == (0,) and b.after == (1,)
        assert b.time_us == T0 + 180 * SEC
        assert abs(b.gap_us - 120 * SEC) <= SEC // 100
    finally:
        data.close()


def test_touching_recordings_have_a_zero_gap(tmp_path):
    a = write(tmp_path / "a.mefd", ["x"], T0, 30)
    b = write(tmp_path / "b.mefd", ["x"], T0 + 30 * SEC, 30)
    data = open_recordings([str(a), str(b)])
    try:
        (boundary,) = data.boundaries()
        assert boundary.gap_us == 0
    finally:
        data.close()


def test_ownership_of_side_by_side_recordings(side_by_side):
    data = open_recordings([str(p) for p in side_by_side])
    try:
        assert data.channels == ("C1", "C2", "C3", "C4")
        assert [s[2] for s in data.ownership("C1")] == [0]
        assert [s[2] for s in data.ownership("C4")] == [1]
        assert data.recordings_at(T0, T0 + SEC) == [0, 1]
        assert data.recordings_at(T0, T0 + SEC, ["C3"]) == [1]
        assert data.boundaries() == []  # same span: the set never changes
        # a channel the other recording does not have reads as NaN there
        tile = data.read_tile(["C1", "C3"], 256.0, 0)
        assert np.isfinite(tile[0, 0]) and np.isfinite(tile[1, 0])
    finally:
        data.close()


def test_overlap_is_refused_in_letters_only(tmp_path):
    a = write(tmp_path / "Quentin_Zebedee.mefd", ["x"], T0, 60)
    b = write(tmp_path / "other_name.mefd", ["x"], T0 + 30 * SEC, 60)
    with pytest.raises(ViewerRefusal) as err:
        open_recordings([str(a), str(b)])
    m = err.value.user_message
    text = m.title + m.text
    assert err.value.code == "overlap" and err.value.letters == ("A", "B")
    assert "Recordings A and B" in text and "one at a time" in text
    for secret in ("Quentin", "other_name", str(tmp_path), "x.timd"):
        assert secret not in text
        assert secret not in str(err.value)
    shown = describe_error(err.value)
    assert shown.text == m.text and "A and B" in shown.text


def test_different_sampling_rates_are_refused(tmp_path):
    a = write(tmp_path / "a.mefd", ["x"], T0, 30, fs=256.0)
    b = write(tmp_path / "b.mefd", ["x"], T0 + 100 * SEC, 30, fs=512.0)
    with pytest.raises(ViewerRefusal) as err:
        open_recordings([str(a), str(b)])
    assert err.value.code == "rate"
    assert "different sampling rates" in err.value.user_message.text
    assert str(tmp_path) not in err.value.user_message.text


def test_different_passwords_are_refused_with_one_at_a_time(tmp_path):
    a = write(tmp_path / "a.mefd", ["x"], T0, 10, password1="pw-one-1", password2="pw-one-2")
    b = write(
        tmp_path / "b.mefd",
        ["x"],
        T0 + 100 * SEC,
        10,
        password1="pw-two-1",
        password2="pw-two-2",
    )
    with pytest.raises(ViewerRefusal) as err:
        open_recordings([str(a), str(b)], ["pw-one-2", "pw-two-2"])
    assert err.value.code == "passwords"
    text = err.value.user_message.text
    assert "different passwords" in text and "one at a time" in text
    assert str(tmp_path) not in text and "pw-" not in text
    # each opens alone
    data = open_recordings([str(a)], ["pw-one-2"])
    data.close()


def test_one_shared_password_opens_several(tmp_path):
    kw = dict(password1="p1-shared", password2="p2-shared")
    a = write(tmp_path / "a.mefd", ["x"], T0, 10, **kw)
    b = write(tmp_path / "b.mefd", ["x"], T0 + 60 * SEC, 10, **kw)
    data = open_recordings([str(a), str(b)], ["p2-shared", "p2-shared"])
    try:
        assert len(data.recordings) == 2
        assert np.isfinite(data.read_tile(["x"], 256.0, 0)[0, 0])
    finally:
        data.close()


def test_annotations_are_tagged_and_sorted_across_recordings_and_levels(sequential):
    a, b = sequential
    with mef3io.Writer(str(a)) as w:
        w.write_annotations([{"time": T0 + 50 * SEC, "text": "late in A"}])
        w.write_annotations([{"time": T0 + 5 * SEC, "text": "early in A"}], "Cz")
    with mef3io.Writer(str(b)) as w:
        w.write_annotations([{"time": T0 + 185 * SEC, "text": "in B"}])
        w.write_annotations(
            [{"time": T0 + 181 * SEC, "type": "EDFA", "text": "span", "duration": SEC}], "Fz"
        )
    data = open_recordings([str(a), str(b)])
    try:
        notes = data.annotations()
        assert [n["time"] for n in notes] == sorted(n["time"] for n in notes)
        assert [(n["recording"], n["level"], n["channel"]) for n in notes] == [
            (0, "channel", "Cz"),
            (0, "session", None),
            (1, "channel", "Fz"),
            (1, "session", None),
        ]
        assert notes[2]["type"] == "EDFA" and notes[2]["duration"] == SEC
        assert data.annotations_locked == ()
    finally:
        data.close()


def test_segment_level_records_are_read_too(tmp_path):
    import shutil

    a = write(tmp_path / "a.mefd", ["x"], T0, 30)
    with mef3io.Writer(str(a)) as w:
        w.write_annotations([{"time": T0 + SEC, "text": "channel"}], "x")
    channel = a / "x.timd"
    part = channel / "x-000000.segd"
    for suffix in ("rdat", "ridx"):
        shutil.copy(channel / f"x.{suffix}", part / f"x-000000.{suffix}")
    data = open_recordings([str(a)])
    try:
        levels = sorted(n["level"] for n in data.annotations())
        assert levels == ["channel", "segment"]
    finally:
        data.close()


def test_locked_annotations_give_nothing_and_say_so(tmp_path):
    a = write(tmp_path / "a.mefd", ["x"], T0, 10, password1="basic-1", password2="full-2")
    with mef3io.Writer(str(a), password1="basic-1", password2="full-2") as w:
        w.write_annotations([{"time": T0 + SEC, "text": "secret note"}])
    full = open_recordings([str(a)], ["full-2"])
    basic = open_recordings([str(a)], ["basic-1"])
    try:
        assert [n["text"] for n in full.annotations()] == ["secret note"]
        assert basic.annotations() == [] and basic.annotations_locked == (0,)
        assert np.isfinite(basic.read_tile(["x"], 256.0, 0)[0, 0])  # signals still show
    finally:
        full.close()
        basic.close()


def test_reopen_shows_what_was_appended(sequential):
    a, _ = sequential
    data = open_recordings([str(a)])
    try:
        assert data.annotations() == []
        with mef3io.Writer(str(a)) as w:
            w.write_annotations([{"time": T0 + SEC, "text": "added"}])
        assert data.annotations() == []  # cached until reopened
        data.reopen()
        assert [n["text"] for n in data.annotations()] == ["added"]
        assert data.channels == ("Cz", "Fz")
    finally:
        data.close()


def test_the_local_source_opens_the_viewer(sequential):
    data = LocalRecordingSource().open_viewer([str(p) for p in sequential], None)
    try:
        assert len(data.recordings) == 2
    finally:
        data.close()


# --- tiles ---------------------------------------------------------------------


@pytest.mark.parametrize("fs", [256.0, 1000.0 / 3.0, 8000.0])
def test_tile_seams_are_exact(tmp_path, fs):
    seconds = 3.4 * TILE_SAMPLES / fs
    path = write(tmp_path / "seam.mefd", ["a", "b"], T0 + 123_457, seconds, fs=fs)
    data = open_recordings([str(path)])
    try:
        tiles = [data.read_tile(["a", "b"], fs, k) for k in range(3)]
        assert all(t.dtype == np.float32 and t.shape == (2, TILE_SAMPLES) for t in tiles)
        joined = np.concatenate(tiles, axis=1)
        t0 = data.timeline_start
        one = data._combined.read(
            ["a", "b"], t0, t0 + round(3 * TILE_SAMPLES * 1e6 / fs), dtype="float32"
        )
        assert one.shape == joined.shape
        np.testing.assert_array_equal(joined, one)
        assert np.isfinite(joined).all()
        # a page across a seam equals one read() of the page
        page0 = data.tile_start_time(fs, 1) - 1_000_000
        page1 = page0 + 2_000_000
        n = round((page1 - page0) * fs / 1e6)
        first = round((page0 - t0) * fs / 1e6)
        direct = data._combined.read(["a"], page0, page1, dtype="float32")[0]
        np.testing.assert_array_equal(joined[0, first : first + n], direct[:n])
    finally:
        data.close()


def test_tiles_are_nan_where_there_is_no_data(sequential):
    data = open_recordings([str(p) for p in sequential])
    try:
        k = data.tile_index_range(256.0, T0 + 100 * SEC, T0 + 110 * SEC)[0]
        tile = data.read_tile(["Cz"], 256.0, k)
        start = data.tile_start_time(256.0, k)
        inside_gap = int((T0 + 100 * SEC - start) * 256 / 1e6)
        assert np.isnan(tile[0, inside_gap])
        assert np.isfinite(data.read_tile(["Cz"], 256.0, 0)[0, 100])
        with pytest.raises(ValueError):
            data.read_tile(["Cz"], 512.0, 0)
    finally:
        data.close()


def test_tile_index_math(tmp_path):
    path = write(tmp_path / "m.mefd", ["a"], T0, 10)
    data = open_recordings([str(path)])
    try:
        assert data.tile_start_time(256.0, 0) == T0
        assert data.tile_start_time(256.0, 1) == T0 + round(TILE_SAMPLES * 1e6 / 256.0)
        assert data.tile_index_range(256.0, T0, T0 + SEC) == (0, 0)
        edge = data.tile_start_time(256.0, 2)
        assert data.tile_index_range(256.0, edge - 1_000_000, edge + 1_000_000) == (1, 2)
        assert data.tile_index_range(256.0, edge - 1_000_000, edge) == (1, 1)
        assert data.tile_index_range(256.0, T0 - 10 * SEC, T0 + SEC)[0] == 0
    finally:
        data.close()


def test_a_cancelled_tile_read_stops(sequential):
    data = open_recordings([str(sequential[0])])
    try:
        token = CancelToken()
        token.cancel()
        with pytest.raises(JobCancelled):
            data.read_tile(["Cz"], 256.0, 0, token)
    finally:
        data.close()


def _rchar() -> int | None:
    try:
        for line in Path("/proc/self/io").read_text().splitlines():
            if line.startswith("rchar:"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


needs_proc = pytest.mark.skipif(_rchar() is None, reason="no /proc/self/io")

#: Measured by the bounded-read test (printed with -s).
MEASURED: dict[str, float] = {}


@needs_proc
def test_reading_a_tile_reads_about_a_tile_not_the_recording(tmp_path):
    fs = 1000.0
    path = write(tmp_path / "long.mefd", ["a", "b"], T0, 3600, fs=fs)
    data_bytes = sum(p.stat().st_size for p in path.rglob("*.tdat"))
    data = open_recordings([str(path)])
    try:
        k = 20  # the middle of the hour
        before = _rchar()
        tile = data.read_tile(["a", "b"], fs, k)
        read = _rchar() - before
        assert np.isfinite(tile).all()
        tile_bytes = data_bytes * TILE_SAMPLES / (3600 * fs)
        MEASURED["tile_read_bytes"] = read
        MEASURED["tile_data_bytes"] = tile_bytes
        MEASURED["recording_bytes"] = data_bytes
        print(f"MEASURED tile read {read} B, tile data {tile_bytes:.0f} B, file {data_bytes} B")
        assert read <= 3 * tile_bytes
        assert read < data_bytes / 10
    finally:
        data.close()


@needs_proc
def test_the_envelope_reads_the_index_only(tmp_path):
    path = write(tmp_path / "env.mefd", ["a"], T0, 1800, fs=1000.0)
    data_bytes = sum(p.stat().st_size for p in path.rglob("*.tdat"))
    data = open_recordings([str(path)])
    try:
        before = _rchar()
        env = data.envelope("a", T0, T0 + 1800 * SEC)
        read = _rchar() - before
        assert len(env["min"]) > 100
        assert (env["max"] >= env["min"]).all()
        assert read < data_bytes * 0.1
    finally:
        data.close()


# --- review round 1 ----------------------------------------------------------------


def test_mid_read_cancellation_reaches_the_library(tmp_path):
    import threading
    import time

    from mef3io_studio.model.cancel import stopped_by_library

    fs = 1000.0
    path = write(tmp_path / "big.mefd", [f"c{i}" for i in range(8)], T0, 3600, fs=fs)
    data = open_recordings([str(path)])
    try:
        names = list(data.channels)
        t1 = T0 + 3600 * SEC
        start = time.perf_counter()
        data._combined.read(names, T0, t1, dtype="float32")
        full = time.perf_counter() - start

        token = CancelToken()
        threading.Timer(full * 0.1, token.cancel).start()
        start = time.perf_counter()
        with pytest.raises(JobCancelled), stopped_by_library():
            data._combined.read(names, T0, t1, dtype="float32", cancel=token.for_library())
        assert time.perf_counter() - start < full * 0.7
    finally:
        data.close()


def test_seams_are_exact_with_a_recording_off_the_grid(tmp_path):
    fs = 256.0
    a = write(tmp_path / "a.mefd", ["x"], T0, 200, fs=fs, seed=5)
    b = write(tmp_path / "b.mefd", ["x"], T0 + 200 * SEC + 1953, 200, fs=fs, seed=6)
    data = open_recordings([str(a), str(b)])
    try:
        joined = np.concatenate([data.read_tile(["x"], fs, k) for k in range(2)], axis=1)
        t0 = data.timeline_start
        one = data._combined.read(
            ["x"], t0, t0 + round(2 * TILE_SAMPLES * 1e6 / fs), dtype="float32"
        )
        np.testing.assert_array_equal(joined, one)
    finally:
        data.close()


def test_the_same_recording_twice_is_its_own_refusal(sequential):
    a, _ = sequential
    with pytest.raises(ViewerRefusal) as err:
        open_recordings([str(a), str(a)])
    assert err.value.code == "twice"
    assert err.value.user_message.title == "This recording was selected twice."


def test_other_value_errors_are_not_turned_into_a_refusal(sequential, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("something else")

    monkeypatch.setattr(mef3io, "MultiReader", boom)
    with pytest.raises(ValueError, match="something else") as err:
        open_recordings([str(p) for p in sequential])
    assert not isinstance(err.value, ViewerRefusal)


def test_a_recording_without_data_is_refused_by_letter(tmp_path):
    a = write(tmp_path / "a.mefd", ["x"], T0, 10)
    empty = tmp_path / "Hidden_Name.mefd"
    with mef3io.Writer(str(empty), overwrite=True):
        pass
    try:
        open_recordings([str(a), str(empty)])
    except ViewerRefusal as refusal:
        assert refusal.code == "empty" and refusal.letters == ("B",)
        assert "Hidden_Name" not in refusal.user_message.text
        assert "Recording B" in refusal.user_message.text
    else:  # mef3io refuses to open it at all: nothing for the viewer to do
        pytest.skip("an empty session cannot be built here")


def test_reopen_bumps_the_epoch_and_keeps_the_id(sequential):
    data = open_recordings([str(sequential[0])])
    try:
        ident, epoch = data.data_id, data.epoch
        data.reopen()
        assert (data.data_id, data.epoch) == (ident, epoch + 1)
        assert not data.closed
    finally:
        data.close()
    other = open_recordings([str(sequential[0])])
    try:
        assert other.data_id != ident
    finally:
        other.close()


def test_reprs_hold_no_names_or_note_text(tmp_path):
    a = write(tmp_path / "Quentin_Zebedee.mefd", ["x"], T0, 10)
    with mef3io.Writer(str(a)) as w:
        w.write_annotations([{"time": T0 + SEC, "text": "Quentin moved"}])
    data = open_recordings([str(a)])
    try:
        shown = repr(data.recordings) + repr(data.annotations())
        assert str(T0 + SEC) not in shown
        assert "Quentin" not in shown and "Zebedee" not in shown
    finally:
        data.close()


def test_touching_within_half_a_sample_follows_the_librarys_verdict(tmp_path):
    fs = 256.0
    a = write(tmp_path / "a.mefd", ["x"], T0, 30, fs=fs, seed=8)
    # starts 1 ms before A's last sample ends: inside half a sample (1953 us)
    b = write(tmp_path / "b.mefd", ["x"], T0 + 30 * SEC - 1000, 30, fs=fs, seed=9)
    try:
        mef3io.MultiReader([str(a), str(b)]).close()
        accepted = True
    except ValueError:
        accepted = False
    if accepted:
        open_recordings([str(a), str(b)]).close()
    else:
        with pytest.raises(ViewerRefusal) as err:
            open_recordings([str(a), str(b)])
        assert err.value.code == "overlap" and str(tmp_path) not in err.value.user_message.text


def test_the_thread_cap_survives_a_reopen(sequential):
    data = open_recordings([str(sequential[0])])
    try:
        data.set_read_threads(2)
        data.reopen()
        assert data._combined.n_threads == 2
        data.set_read_threads(None)
        assert data._combined.n_threads == 0
    finally:
        data.close()
