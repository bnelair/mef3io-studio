"""Make new recordings (#16): saved channel choices (model/presets.py) and
the ONE batch runner carrying copies with changes (model/pipeline.py kind
"change"): a Stop leaves nothing half-made, a crash resumes, the originals
stay byte for byte, and no password, channel name or detail reaches a log.
Made-up signals only."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import mef3io
import numpy as np
import pytest
from explorer_sessions import SECOND, T0, write_large, write_plain

from mef3io_studio.model import anonymize as a
from mef3io_studio.model import changes, journal, pipeline, presets
from mef3io_studio.model.channels import ChannelSelection
from mef3io_studio.workers import tasks

PW = ("basic-pw-4321", "full-pw-8765")
SECRET_CHANNELS = ("Zq1", "Zq2", "Zq3")
PLACE = "Quiet Room 12"


def tree_hash(folder: Path) -> dict[str, str]:
    return {
        p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


# --------------------------------------------------------------------------
# Presets


def test_a_preset_round_trips_through_the_private_store(tmp_path):
    store = presets.PresetStore(str(tmp_path / "data"))
    assert store.load() == []
    store.add(presets.make_preset("Left depth", ["LA1", "LA2", "LA1"]))
    store.add(presets.make_preset("No scalp", ["Cz", "Fz"], exclude=True))
    assert store.names() == ["Left depth", "No scalp"]
    got = store.get("Left depth")
    assert got.channels == ("LA1", "LA2") and not got.exclude  # once each, in order
    assert store.get("No scalp").exclude
    with pytest.raises(presets.PresetError, match="already exists"):
        store.add(presets.make_preset("left DEPTH", ["X"]))
    store.add(presets.make_preset("left DEPTH", ["X1"]), replace=True)
    assert store.get("left DEPTH").channels == ("X1",) and len(store.load()) == 2
    store.rename("No scalp", "Without scalp")
    assert store.remove("Without scalp") and not store.remove("Without scalp")
    assert store.names() == ["left DEPTH"]


def test_the_store_is_sealed_private_and_in_the_app_data_folder(tmp_path):
    store = presets.PresetStore()  # the app's own place
    assert Path(store.path).parent == Path(journal.data_dir())
    store.add(presets.make_preset("Montage of Zebedee", list(SECRET_CHANNELS)), replace=True)
    try:
        raw = Path(store.path).read_text(encoding="utf-8")
        for secret in (*SECRET_CHANNELS, "Zebedee", "Montage"):
            assert secret not in raw  # sealed: no channel name or title in clear
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
        assert [p.channels for p in store.load()] == [SECRET_CHANNELS]
        # A changed file is not trusted, and not replaced by guesswork.
        record = json.loads(raw)
        record["sealed"] = record["sealed"][:-8] + "AAAAAAA="
        Path(store.path).write_text(json.dumps(record), encoding="utf-8")
        assert store.load() == []
    finally:
        os.unlink(store.path)


def test_a_preset_needs_a_name_and_channels():
    for bad in ("", "   ", "x" * 81):
        with pytest.raises(presets.PresetError):
            presets.make_preset(bad, ["a"])
    with pytest.raises(presets.PresetError, match="at least one channel"):
        presets.make_preset("ok", ["", "  "])
    assert presets.name_problem("Taken", ["taken"]) is not None
    assert presets.name_problem("Free", ["taken"]) is None


def test_presets_are_shared_as_a_small_file(tmp_path):
    original = [
        presets.make_preset("Depth", ["LA1", "LA2"]),
        presets.make_preset("Not scalp", ["Cz"], exclude=True),
    ]
    text = presets.export_text(original)
    assert presets.parse_export(text) == original
    store = presets.PresetStore(str(tmp_path))
    store.add(presets.make_preset("Depth", ["Q"]))
    assert store.import_text(text) == ["Depth (2)", "Not scalp"]  # nothing replaced
    assert store.get("Depth").channels == ("Q",)
    assert store.get("Depth (2)").channels == ("LA1", "LA2")
    for junk in ("not json", "[]", '{"presets": 3}', '{"kind": "other", "presets": []}'):
        with pytest.raises(presets.PresetError):
            presets.parse_export(junk)
    with pytest.raises(presets.PresetError):
        presets.parse_export('{"presets": [{"name": "x", "channels": [5]}]}')


def test_a_keep_preset_on_a_recording_with_missing_channels():
    preset = presets.make_preset("Depth", ["LA1", "LA2", "RA1"])
    match = presets.match_recording(preset, ["LA1", "LA2", "LA3", "Cz"])
    assert match.kept == ("LA1", "LA2")
    assert match.left_out == ("LA3", "Cz")
    assert match.missing == ("RA1",)  # shown, not an error
    assert not match.empty
    assert match.channels == ("LA1", "LA2") and match.exclude_channels == ("LA3", "Cz")
    # Exact by default; case-insensitive on request, in the recording's spelling.
    assert presets.match_recording(preset, ["la1"]).empty
    folded = presets.match_recording(preset, ["la1", "Cz"], ignore_case=True)
    assert folded.kept == ("la1",) and folded.missing == ("LA2", "RA1")


def test_an_exclude_preset_leaves_out_what_the_recording_has():
    preset = presets.make_preset("No scalp", ["Cz", "Fz", "Pz"], exclude=True)
    match = presets.match_recording(preset, ["LA1", "Cz", "Pz"])
    assert match.kept == ("LA1",) and match.left_out == ("Cz", "Pz")
    assert match.missing == ("Fz",)
    nothing_left = presets.match_recording(preset, ["Cz", "Fz"])
    assert nothing_left.empty


def test_a_recording_left_with_no_channel_is_blocked():
    preset = presets.make_preset("Depth", ["LA1"])
    assert presets.match_recording(preset, ["Cz", "Fz"]).empty
    selection = ChannelSelection({"r1": ["LA1", "Cz"], "r2": ["Cz", "Fz"]})
    missing = presets.apply_to_selection(preset, selection)
    assert missing == ()  # LA1 is in one of them
    assert selection.recordings_left_empty() == ("r2",)
    assert not selection.can_continue  # the picker's own block
    assert selection.kept_in("r1") == ("LA1",) and selection.excluded_in("r1") == ("Cz",)


def test_applying_a_preset_to_the_picker_reports_channels_nobody_has():
    selection = ChannelSelection({"r1": ["LA1", "LA2", "Cz"]})
    preset = presets.make_preset("Depth", ["LA1", "RA9"])
    assert presets.apply_to_selection(preset, selection) == ("RA9",)
    assert selection.kept == frozenset({"LA1"})
    selection.undo()  # one step, like any other change
    assert selection.kept == frozenset({"LA1", "LA2", "Cz"})
    gone = presets.make_preset("No scalp", ["Cz"], exclude=True)
    presets.apply_to_selection(gone, selection)
    assert selection.kept == frozenset({"LA1", "LA2"})
    saved = presets.preset_from_selection("Again", selection)
    assert saved.channels == ("LA1", "LA2") and not saved.exclude
    assert presets.preset_from_selection("Out", selection, exclude=True).channels == ("Cz",)


# --------------------------------------------------------------------------
# One batch runner for both kinds


@pytest.fixture(autouse=True)
def _no_journals():
    shutil.rmtree(journal.journal_dir(), ignore_errors=True)
    journal._save_temporaries([])
    journal._save_kept([])
    yield
    shutil.rmtree(journal.journal_dir(), ignore_errors=True)
    journal._save_temporaries([])
    journal._save_kept([])


def write_zq(path: Path) -> Path:
    with mef3io.Writer(str(path), overwrite=True, units="uV") as w:
        for name in SECRET_CHANNELS:
            w.write(name, np.sin(np.arange(2560) / 10.0), T0, 256.0)
    return path


def change_batch(tmp_path, sources, exclude=("a3",), output_password=None, metadata=None):
    dest = tmp_path / "out"
    dest.mkdir(exist_ok=True)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    requests = []
    per_recording = bool(exclude) and isinstance(exclude[0], (tuple, list))
    for i, src in enumerate(sources):
        requests.append(
            changes.ChangeRequest(
                str(src),
                str(dest / f"New{i + 1}.mefd"),
                exclude_channels=tuple(exclude[i] if per_recording else exclude),
                output_password=output_password,
                metadata=metadata,
                key=f"k{i}",
            )
        )
    batch = pipeline.plan_batch(
        pipeline.FOLDERS,
        requests,
        str(dest),
        str(work),
        {r.key: 1_000_000 for r in requests},
        {r.key: (Path(r.source).name, T0, T0 + 10 * SECOND) for r in requests},
    )
    return batch, dest, work


def run(qtbot, jobs, batch, journal_=None, timeout=180_000):
    handle = jobs.submit("batch", tasks.run_batch, batch, journal_)
    published = []
    handle.published.connect(published.extend)
    with qtbot.waitSignal(handle.finished, timeout=timeout):
        pass
    return handle, published


def results(published):
    return {r.key: r for r in published if isinstance(r, a.CopyResult)}


def test_items_know_their_kind_and_the_journal_keeps_it(tmp_path):
    batch, _dest, _work = change_batch(
        tmp_path,
        [write_plain(tmp_path / "r.mefd")],
        exclude=("a3",),
        output_password=PW,
        metadata={"recording_location": PLACE},
    )
    (item,) = batch.items
    assert item.kind == pipeline.KIND_CHANGE
    payload = pipeline.to_payload(batch)
    text = json.dumps(payload)
    assert payload["items"][0]["kind"] == "change"
    assert payload["items"][0]["request"]["protected"] is True
    for password in PW:
        assert password not in text  # passwords never reach the journal
    again = pipeline.from_payload(json.loads(text), lambda _s: "", PW)
    request = again.items[0].request
    assert isinstance(request, changes.ChangeRequest) and again.items[0].kind == "change"
    assert request.exclude_channels == ("a3",)
    assert request.metadata == {"recording_location": PLACE}
    assert request.output_password == PW and request.key == "k0"
    # The anonymizer's items still come back as anonymizer items.
    anon = a.CopyRequest("k", str(tmp_path / "r.mefd"), str(tmp_path / "x.mefd"), 5)
    plan = pipeline.plan_batch(pipeline.FOLDERS, [anon], str(tmp_path), "", {}, {})
    assert plan.items[0].kind == pipeline.KIND_ANONYMIZE
    back = pipeline.from_payload(pipeline.to_payload(plan), lambda _s: "", None)
    assert isinstance(back.items[0].request, a.CopyRequest)


def test_a_batch_of_three_new_recordings(qtbot, jobs, tmp_path):
    sources = [write_plain(tmp_path / f"rec{i}.mefd") for i in range(3)]
    before = [tree_hash(s) for s in sources]
    batch, dest, work = change_batch(
        tmp_path, sources, output_password=PW, metadata={"recording_location": PLACE}
    )
    handle, published = run(qtbot, jobs, batch)
    assert handle.record.status.name == "SUCCEEDED"
    done = results(published)
    assert len(done) == 3 and all(r.ok for r in done.values())
    for i in range(3):
        reader = mef3io.Reader(str(dest / f"New{i + 1}.mefd"), password=PW[1])
        assert sorted(reader.channels) == ["a1", "a2"]
        assert reader.metadata.subject.recording_location == PLACE
        with pytest.raises(Exception):  # noqa: B017 - the old way in no longer works
            mef3io.Reader(str(dest / f"New{i + 1}.mefd"), password="nope")
    assert [tree_hash(s) for s in sources] == before  # originals untouched
    assert list(work.iterdir()) == [] and journal.unfinished() == []


def test_stop_halfway_leaves_no_partial_and_no_half_result(qtbot, jobs, tmp_path):
    small0 = write_plain(tmp_path / "rec0.mefd")
    big = write_large(tmp_path / "rec1.mefd", channels=2, parts=2, seconds=900)
    small2 = write_plain(tmp_path / "rec2.mefd")
    sources = [small0, big, small2]
    before = [tree_hash(s) for s in sources]
    batch, dest, work = change_batch(tmp_path, sources, exclude=(("a3",), ("n1",), ("a3",)))
    handle = jobs.submit("batch", tasks.run_batch, batch, None)
    qtbot.waitUntil(
        lambda: (
            handle.record.progress is not None
            and handle.record.progress.phase.startswith("Recording 2 of 3")
            and (handle.record.progress.fraction or 0) > 1 / 3 + 0.01
        ),
        timeout=60_000,
    )
    handle.cancel()
    with qtbot.waitSignal(handle.finished, timeout=60_000):
        pass
    assert sorted(p.name for p in dest.iterdir()) == ["New1.mefd"]  # only the finished one
    assert not list(dest.rglob("*.partial")) and list(work.iterdir()) == []
    assert sorted(mef3io.Reader(str(dest / "New1.mefd")).channels) == ["a1", "a2"]
    assert [tree_hash(s) for s in sources] == before
    assert journal.unfinished() == []  # a deliberate stop needs no resume


def test_a_crash_is_continued_from_the_journal(qtbot, jobs, tmp_path):
    """A real process death in the middle of the second recording; the
    journal carries the kind, the rest is made on resume, the first is not
    made again."""
    small = write_plain(tmp_path / "rec0.mefd")
    big = write_large(tmp_path / "rec1.mefd", channels=2, parts=2, seconds=1800)
    last = write_plain(tmp_path / "rec2.mefd")
    sources = [small, big, last]
    before = [tree_hash(s) for s in sources]
    batch, dest, _work = change_batch(tmp_path, sources, exclude=(("a3",), ("n1",), ("a3",)))
    blob = tmp_path / "batch.pickle"
    blob.write_bytes(pickle.dumps(batch))
    env = dict(os.environ)
    src = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join([src, str(Path(__file__).parent), env["PYTHONPATH"]])
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "batch_child.py"), str(blob)], env=env
    )
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            pending = journal.unfinished()
            if pending and pending[0].done == 1 and (dest / "New2.mefd.partial").exists():
                break
            assert child.poll() is None, "the batch ended before it could be interrupted"
            time.sleep(0.05)
        child.kill()
        child.wait(30)
    finally:
        if child.poll() is None:
            child.kill()
    (pending,) = journal.unfinished()
    assert (pending.done, pending.total) == (1, 3)
    assert (dest / "New2.mefd.partial").exists()  # the half-made copy is still there
    resumed = pipeline.from_payload(pending.payload, lambda _s: "", None)
    assert [i.kind for i in resumed.items] == ["change"] * 3
    check = jobs.submit("check", tasks.preflight_batch, resumed, list(pending.states), "", pending)
    verdict = []
    check.succeeded.connect(verdict.append)
    with qtbot.waitSignal(check.finished, timeout=120_000):
        pass
    assert verdict[0]["problems"] == [] and verdict[0]["existing"] == []
    _handle, published = run(qtbot, jobs, resumed, pending)
    done = results(published)
    assert all(r.ok for r in done.values()) and len(done) == 3
    assert sorted(p.name for p in dest.iterdir()) == ["New1.mefd", "New2.mefd", "New3.mefd"]
    assert not list(dest.rglob("*.partial"))
    assert sorted(mef3io.Reader(str(dest / "New2.mefd")).channels) == ["n0"]
    assert [tree_hash(s) for s in sources] == before
    assert journal.unfinished() == []


def test_the_check_before_starting_reports_a_refusal_by_recording(qtbot, jobs, tmp_path):
    sources = [write_plain(tmp_path / f"rec{i}.mefd") for i in range(2)]
    batch, dest, _work = change_batch(tmp_path, sources, exclude=())
    (dest / "New2.mefd").mkdir()  # the second name is taken
    check = jobs.submit("check", tasks.preflight_batch, batch)
    verdict = []
    check.succeeded.connect(verdict.append)
    with qtbot.waitSignal(check.finished, timeout=120_000):
        pass
    names = verdict[0]["names"]
    assert names["k0"] is None and names["k1"] is not None
    assert not any(str(tmp_path) in p for p in verdict[0]["problems"])


def test_no_password_channel_name_or_detail_reaches_the_log(qtbot, jobs, tmp_path):
    from mef3io_studio.common import privacy
    from mef3io_studio.common.logging_setup import setup_logging, teardown_logging

    sources = [write_zq(tmp_path / f"rec{i}.mefd") for i in range(2)]
    log_file = setup_logging(tmp_path / "logs", level=logging.DEBUG)
    try:
        batch, _dest, _work = change_batch(
            tmp_path,
            sources,
            exclude=("Zq3",),
            output_password=PW,
            metadata={"recording_location": PLACE},
        )
        handle, published = run(qtbot, jobs, batch)
        assert all(r.ok for r in results(published).values())
        logging.getLogger("test").info("done")
    finally:
        teardown_logging()
        privacy.identities.clear()
    text = log_file.read_text(encoding="utf-8")
    assert "batch of 2 finished" in text
    for secret in (*PW, *SECRET_CHANNELS, PLACE, str(tmp_path), "New1", "rec0"):
        assert secret not in text, secret


# --------------------------------------------------------------------------
# Review round 1: hostile or damaged files, and a store that cannot be read


def test_hostile_shared_files_are_refused_not_crashes():
    for text in ("[" * 200_000, '{"presets":' * 50_000, "\x00", ""):
        with pytest.raises(presets.PresetError):
            presets.parse_export(text)
    bad_flag = '{"presets": [{"name": "x", "channels": ["a"], "exclude": "false"}]}'
    with pytest.raises(presets.PresetError):
        presets.parse_export(bad_flag)  # bool("false") would be True
    ok = '{"presets": [{"name": "x", "channels": ["a"], "exclude": false}]}'
    assert presets.parse_export(ok)[0].exclude is False


def test_names_must_be_printable_and_imported_names_fit(tmp_path):
    assert presets.name_problem("bad\x07name") is not None
    assert presets.name_problem("line\nbreak") is not None
    with pytest.raises(presets.PresetError):
        presets.parse_export('{"presets": [{"name": "a\\u0000b", "channels": ["a"]}]}')
    store = presets.PresetStore(str(tmp_path))
    long_name = "n" * presets.MAX_NAME_CHARS
    store.add(presets.make_preset(long_name, ["a"]))
    text = presets.export_text([presets.make_preset(long_name, ["b"])])
    with pytest.raises(presets.PresetError):  # "(2)" would not fit
        store.import_text(text)
    assert store.names() == [long_name]


def test_an_unreadable_store_is_set_aside_never_overwritten(tmp_path):
    store = presets.PresetStore(str(tmp_path))
    store.add(presets.make_preset("Mine", ["Zq1"]))
    original = Path(store.path).read_text(encoding="utf-8")
    for damaged in (
        '{"sealed": 5}',  # not a string
        '{"sealed": "AAAA"}',  # not ours / changed
        "not json at all",
        '{"nothing": 1}',
    ):
        Path(store.path).write_text(damaged, encoding="utf-8")
        assert store.load() == [] and store.unreadable
        store.add(presets.make_preset("New", ["Zq2"]))
        assert store.moved_aside.startswith("presets.json.unreadable-")
        assert (tmp_path / store.moved_aside).read_text(encoding="utf-8") == damaged
        assert store.names() == ["New"] and not store.unreadable
        store.remove("New")
    # A healthy store is never moved; a missing one is not "unreadable".
    Path(store.path).write_text(original, encoding="utf-8")
    store.add(presets.make_preset("More", ["Zq3"]))
    assert store.moved_aside == "" and len(store.names()) == 2
    os.unlink(store.path)
    assert not store.unreadable and store.load() == []
    assert len(list(tmp_path.glob("presets.json.unreadable-*"))) == 4  # nothing deleted


def test_a_failed_save_leaves_no_temporary_file(tmp_path, monkeypatch):
    store = presets.PresetStore(str(tmp_path))
    store.add(presets.make_preset("Keep", ["a"]))

    def boom(*_a):
        raise OSError("disk")

    monkeypatch.setattr(presets.os, "replace", boom)
    with pytest.raises(OSError):
        store.add(presets.make_preset("Other", ["b"]))
    monkeypatch.undo()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["presets.json"]
    assert store.names() == ["Keep"]


def test_a_stop_just_after_a_change_item_finished_does_not_say_not_made():
    assert "may be complete" in tasks.MAYBE_COMPLETE.title.lower()
    assert "not made" not in tasks.MAYBE_COMPLETE.title.lower()
