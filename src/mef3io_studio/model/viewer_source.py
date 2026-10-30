"""The signal viewer's data: opening, ownership, tiles, envelope, annotations.

This is the ONLY viewer module that imports mef3io (docs/design/viewer.md,
"Data: reading, tiles, prefetch, cache"). It opens one ``mef3io.Reader`` per
recording plus, with two or more, one ``mef3io.MultiReader`` for the combined
reads. The per-recording Readers say which recording a segment or an
annotation came from, which MultiReader does not.

Rules: no paths, names or notes in anything that can reach a log (messages
name recordings by letter only); names are handed out through
:meth:`ViewerData.identity_values` for the caller (a job thread) to register
with ``register_identity``; opens run inside ``library_warnings_caught()``;
reads run without the process-wide quiet lock and add no warning handling
(conditions found at open time come from ``problems()``).

Plain Python, no Qt.
"""

from __future__ import annotations

import itertools
import os
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .cancel import CancelToken, JobCancelled, stopped_by_library
from .errors import UserMessage
from .quiet import library_warnings_caught

#: Samples per tile (per channel). A tile k holds sample indices
#: [k*TILE_SAMPLES, (k+1)*TILE_SAMPLES) of the rate's sample grid.
TILE_SAMPLES = 65536

#: Gaps up to this long between two segments of one recording are drawn as
#: one span in the coverage (coarse) view.
COVERAGE_MERGE_US = 1_000_000

_LEVEL_ORDER = {"session": 0, "channel": 1, "segment": 2}


# --- plain-language refusals -------------------------------------------------


class ViewerRefusal(ValueError):
    """The recordings cannot be viewed together (or at all). ``user_message``
    is the plain-language text; recordings are named by letter only."""

    def __init__(self, code: str, user_message: UserMessage, letters: Sequence[str] = ()):
        super().__init__(f"viewer refusal: {code}")
        self.code = code
        self.user_message = user_message
        self.letters = tuple(letters)


def _letter(index: int) -> str:
    out = ""
    n = index
    while True:
        out = chr(65 + n % 26) + out
        n = n // 26 - 1
        if n < 0:
            return out


def _letters_text(letters: Sequence[str]) -> str:
    """ "Recordings A and B" (always at the start of a sentence)."""
    if len(letters) == 1:
        return f"Recording {letters[0]}"
    return "Recordings " + ", ".join(letters[:-1]) + " and " + letters[-1]


_ONE_AT_A_TIME = "You can open them one at a time instead. Nothing was changed."


def _passwords_refusal() -> ViewerRefusal:
    return ViewerRefusal(
        "passwords",
        UserMessage(
            "These recordings need different passwords",
            "The selected recordings are protected with different passwords, and the viewer "
            f"can only show recordings together when they share one. {_ONE_AT_A_TIME}",
        ),
    )


# --- data ---------------------------------------------------------------------


@dataclass(frozen=True)
class RecordingInfo:
    index: int
    letter: str
    #: Index into the viewer's colour list (equal to ``index``).
    colour: int
    #: The recording's name. Identifying: shown only while the recording's
    #: details are visible, never logged.
    name: str = field(repr=False)
    start_us: int
    end_us: int
    channels: tuple[str, ...]


@dataclass(frozen=True)
class Boundary:
    """A time where the set of recordings holding data changes."""

    time_us: int
    before: tuple[int, ...]
    after: tuple[int, ...]
    #: Length of the gap between the recording(s) that ended and the ones
    #: that started (0 = they touch); None when recordings overlap in time
    #: (side by side) or the set simply gains/loses a member.
    gap_us: int | None


def _natural_key(name: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def _session_name(path: str) -> str:
    name = Path(path).name
    for suffix in (".mefd.tar", ".mefd"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def tile_start_index(k: int) -> int:
    return k * TILE_SAMPLES


class Annotation(dict):
    """One record (the library's dict plus ``recording``). Its repr leaves
    out the text and everything else that could identify someone."""

    def __repr__(self) -> str:
        return (
            f"Item(from={self.get('recording')}, level={self.get('level')!r}, "
            f"type={self.get('type')!r})"
        )


_data_ids = itertools.count(1)


class ViewerData:
    """What the viewer reads from: built by :func:`open_recordings`."""

    def __init__(self, paths, passwords, readers, combined, starts, ends, rec_channels):
        self._paths = list(paths)  # in recording order (letters)
        self._passwords = list(passwords)
        self._readers = list(readers)
        self._combined = combined
        self._multi = combined is not None and len(readers) > 1
        self._lock = threading.RLock()
        self._notes_lock = threading.Lock()
        #: Unique per ViewerData; with ``epoch`` (bumped by every reopen) it
        #: goes into every cache key, so nothing read before a reopen is
        #: ever served after it.
        self.data_id: int = next(_data_ids)
        self.epoch: int = 0
        self._closed = False
        #: The decode-thread cap for one read (None = mef3io's automatic).
        #: State of this object, so a reopen applies it again.
        self._read_threads: int | None = None
        names = [_session_name(p) for p in self._paths]
        self.recordings: tuple[RecordingInfo, ...] = tuple(
            RecordingInfo(i, _letter(i), i, names[i], starts[i], ends[i], tuple(rec_channels[i]))
            for i in range(len(readers))
        )
        self.timeline_start: int = min(starts)
        self.timeline_end: int = max(ends)
        self.channels: tuple[str, ...] = tuple(sorted(combined.channels, key=_natural_key))
        self._info: dict[str, dict] = {}
        self._ownership: dict[str, list[tuple[int, int, int]]] = {}
        self._annotations: list[Annotation] | None = None
        self._locked: tuple[int, ...] = ()

    def location_of(self, index: int) -> str:
        """Where recording ``index`` (letter order) was opened from. A path:
        identifying, for matching with the caller's own list only."""
        return self._paths[index]

    # -- identity (for register_identity, called by the job's thread) ----------

    def identity_values(self) -> list[str]:
        """Recording names: identifying, to be registered with the log filter."""
        return [r.name for r in self.recordings if r.name]

    # -- channels ----------------------------------------------------------------

    def _channel_info(self, channel: str) -> dict:
        info = self._info.get(channel)
        if info is None:
            info = self._info[channel] = dict(self._combined.info(channel))
        return info

    def rate(self, channel: str) -> float:
        return float(self._channel_info(channel)["sampling_frequency"])

    def units(self, channel: str) -> str:
        return str(self._channel_info(channel).get("units_description") or "")

    # -- who owns what ---------------------------------------------------------------

    def ownership(self, channel: str) -> list[tuple[int, int, int]]:
        """``(t0, t1, recording index)`` for each stored segment of a channel,
        sorted by time (from the recordings' own segment maps: metadata only).
        """
        spans = self._ownership.get(channel)
        if spans is None:
            spans = []
            for i, reader in enumerate(self._readers):
                if channel not in self.recordings[i].channels:
                    continue
                for seg in reader.segments(channel):
                    spans.append((int(seg["start_time"]), int(seg["end_time"]), i))
            spans.sort()
            self._ownership[channel] = spans
        return list(spans)

    def recordings_at(self, t0: int, t1: int, channels: Sequence[str] | None = None) -> list[int]:
        """Recordings holding data of ``channels`` (default: any) in [t0, t1)."""
        found: set[int] = set()
        for ch in channels if channels is not None else self.channels:
            for a, b, i in self.ownership(ch):
                if a < t1 and b > t0:
                    found.add(i)
        return sorted(found)

    def segments_coverage(
        self, channel: str | None = None, merge_gap_us: int = COVERAGE_MERGE_US
    ) -> list[tuple[int, int, int]]:
        """Coarse ``(t0, t1, recording)`` spans for the overview strip: the
        segment spans, with gaps of up to ``merge_gap_us`` inside one
        recording closed. ``channel=None`` = any channel of the recording.
        Metadata only."""
        spans: list[tuple[int, int, int]] = []
        for ch in (channel,) if channel is not None else self.channels:
            spans.extend(self.ownership(ch))
        merged: list[tuple[int, int, int]] = []
        last: dict[int, int] = {}  # recording -> position in `merged`
        for a, b, i in sorted(spans):
            pos = last.get(i)
            if pos is not None and a <= merged[pos][1] + merge_gap_us:
                merged[pos] = (merged[pos][0], max(merged[pos][1], b), i)
            else:
                merged.append((a, b, i))
                last[i] = len(merged) - 1
        merged.sort()
        return merged

    def boundaries(self) -> list[Boundary]:
        """Times where the set of recordings changes (each recording spans
        its first to its last sample), with the gap between recordings."""
        spans = [(r.start_us, r.end_us, r.index) for r in self.recordings]
        points = sorted({p for a, b, _ in spans for p in (a, b)})

        def before(p):
            return tuple(sorted(i for a, b, i in spans if a < p <= b))

        def after(p):
            return tuple(sorted(i for a, b, i in spans if a <= p < b))

        out: list[Boundary] = []
        last_end: int | None = None
        last_set: tuple[int, ...] = ()
        for p in points:
            was, now = before(p), after(p)
            if was == now:
                continue
            if not now:
                last_end, last_set = p, was
                continue
            if not was:
                if last_end is not None:
                    out.append(Boundary(p, last_set, now, p - last_end))
                continue
            gap = 0 if not set(was) & set(now) else None
            out.append(Boundary(p, was, now, gap))
        return out

    # -- envelope ------------------------------------------------------------------------

    def envelope(self, channel: str, t0: int, t1: int) -> dict:
        """Min/max per block from the block index alone (never the samples)."""
        return self._combined.block_envelope(channel, int(t0), int(t1))

    # -- annotations -----------------------------------------------------------------------

    @property
    def annotations_locked(self) -> tuple[int, ...]:
        """Recordings whose annotations could not be read (password without
        full access). Valid after :meth:`annotations`."""
        return self._locked

    def annotations(self) -> list[Annotation]:
        """Every record at session, channel and segment level of every
        recording, each tagged with ``recording`` (index); sorted by time.
        Recordings whose records are locked contribute nothing and are
        listed in :attr:`annotations_locked`."""
        with self._notes_lock:
            return self._annotations_locked()

    def _annotations_locked(self) -> list[Annotation]:
        import mef3io

        if self._annotations is not None:
            return list(self._annotations)
        out: list[Annotation] = []
        locked: list[int] = []
        for i, reader in enumerate(self._readers):
            try:
                records = reader.records(level="all")
            except mef3io.PasswordError:
                locked.append(i)
                continue
            for rec in records:
                item = Annotation(rec)
                item["recording"] = i
                out.append(item)
        out.sort(
            key=lambda r: (
                r["time"],
                r["recording"],
                _LEVEL_ORDER.get(r.get("level"), 3),
                r.get("channel") or "",
            )
        )
        self._annotations = out
        self._locked = tuple(locked)
        return list(out)

    # -- tiles -------------------------------------------------------------------------------

    def _index_of(self, rate: float, t_us: float) -> float:
        return (t_us - self.timeline_start) * rate / 1e6

    def tile_start_time(self, rate: float, k: int) -> int:
        """The time of the first sample of tile ``k`` (µs, rounded)."""
        return self.timeline_start + int(round(k * TILE_SAMPLES * 1e6 / rate))

    def tile_index_range(self, rate: float, t0: int, t1: int) -> tuple[int, int]:
        """First and last tile (inclusive) holding samples of [t0, t1)."""
        first = max(0, int(np.floor(self._index_of(rate, t0) / TILE_SAMPLES + 1e-9)))
        last_index = max(self._index_of(rate, t1) - 1e-9, self._index_of(rate, t0))
        last = max(first, int(np.floor(last_index / TILE_SAMPLES)))
        return first, last

    def tile_count(self, rate: float) -> int:
        """Number of tiles from the timeline start to its end at ``rate``."""
        return int(self._index_of(rate, self.timeline_end) // TILE_SAMPLES) + 1

    def read_tile(
        self,
        channels: Sequence[str],
        rate: float,
        k: int,
        cancel: CancelToken | None = None,
    ) -> np.ndarray:
        """Tile ``k`` of ``channels`` (all at ``rate``): float32
        ``[len(channels), TILE_SAMPLES]``, NaN where no data.

        The read is widened by one sample on each side and anchored at its
        own t0; the sample of slot j has index ``k*n - 1 + j``, and the two
        outer slots (which belong to the neighbouring tiles) are dropped.
        """
        names = list(channels)
        if self._closed:
            raise JobCancelled()
        for ch in names:
            if abs(self.rate(ch) - rate) > 1e-9 * max(1.0, rate):
                raise ValueError("the channels of a tile must share one sampling rate")
        n = TILE_SAMPLES
        t0 = self.timeline_start + int(round((k * n - 1) * 1e6 / rate))
        t1 = self.timeline_start + int(round(((k + 1) * n + 1) * 1e6 / rate))
        kwargs = {"dtype": "float32"}
        if cancel is not None:
            cancel.raise_if_cancelled()
            kwargs["cancel"] = cancel.for_library()
        with stopped_by_library():
            raw = self._combined.read(names, t0, t1, **kwargs)
        raw = np.asarray(raw, dtype=np.float32).reshape(len(names), -1)
        out = np.full((len(names), n), np.nan, dtype=np.float32)
        body = raw[:, 1 : 1 + n]
        out[:, : body.shape[1]] = body
        return out

    # -- counts / lifecycle ---------------------------------------------------------------------

    def problems(self) -> list[int]:
        """Recordings with unreadable parts (indexes), from ``problems()``."""
        return [i for i, r in enumerate(self._readers) if r.problems]

    @property
    def closed(self) -> bool:
        return self._closed

    def set_read_threads(self, n: int | None) -> None:
        """How many threads one read may use (``None`` or 0 = automatic).
        The scheduler sets a small number when it runs several reads at once,
        so the decode pools do not oversubscribe the cores. Kept across
        :meth:`reopen`. Raises if the library refuses the value."""
        self._read_threads = None if not n else int(n)
        self._apply_read_threads()

    def _apply_read_threads(self) -> None:
        wanted = self._read_threads or 0
        if getattr(self._combined, "n_threads", wanted) != wanted:
            self._combined.n_threads = wanted

    def close(self) -> None:
        """Close the readers. Reads still running fail; stop them first
        (``TileScheduler.drain``)."""
        with self._lock:
            self._closed = True
            for obj in [self._combined, *self._readers]:
                try:
                    obj.close()
                except Exception:
                    pass

    def reopen(self) -> None:
        """Close and open again (after an annotation save), keeping the
        letters; forgets what was cached here and bumps ``epoch``.

        CONTRACT: the caller drains the scheduler first
        (``TileScheduler.drain``): reads running during a reopen fail."""
        fresh = open_recordings(self._paths, self._passwords)
        with self._lock:
            self.close()
            data_id, epoch, cap = self.data_id, self.epoch, self._read_threads
            self.__dict__.update(fresh.__dict__)
            self.data_id, self.epoch, self._read_threads = data_id, epoch + 1, cap
            self._closed = False
            self._apply_read_threads()
        fresh._combined = fresh._readers = None  # the state moved to self


# --- opening -------------------------------------------------------------------------------------


def _password_list(paths: Sequence, passwords) -> list[str]:
    if passwords is None:
        return [""] * len(paths)
    if isinstance(passwords, Mapping):
        return [str(passwords.get(os.fspath(p), passwords.get(p, "")) or "") for p in paths]
    out = [str(p or "") for p in passwords]
    if len(out) != len(paths):
        out = (out + [""] * len(paths))[: len(paths)]
    return out


def _involved(readers, rec_channels, letters, kind: str) -> list[str]:
    """Letters of the recordings that clash, from metadata, as far as that is
    possible. It never decides WHETHER there is a clash (MultiReader's own
    verdict does); with no answer it names them all."""
    found: set[int] = set()
    try:
        for a in range(len(readers)):
            for b in range(a + 1, len(readers)):
                for ch in set(rec_channels[a]) & set(rec_channels[b]):
                    ra = readers[a].info(ch)["sampling_frequency"]
                    rb = readers[b].info(ch)["sampling_frequency"]
                    if kind == "rate":
                        if ra != rb:
                            found |= {a, b}
                        continue
                    sa = [(x["start_time"], x["end_time"]) for x in readers[a].segments(ch)]
                    sb = [(x["start_time"], x["end_time"]) for x in readers[b].segments(ch)]
                    if any(x0 < y1 and y0 < x1 for x0, x1 in sa for y0, y1 in sb):
                        found |= {a, b}
    except Exception:
        found = set()
    return [letters[i] for i in sorted(found or range(len(readers)))]


def _classify(exc: ValueError, readers, rec_channels, letters) -> ViewerRefusal | None:
    """MultiReader's own ValueError as a plain refusal, by the kind of its
    message (never its text, which names channels and paths); None for any
    other error."""
    text = str(exc)
    if "different sampling rates" in text:
        who = _involved(readers, rec_channels, letters, "rate")
        return ViewerRefusal(
            "rate",
            UserMessage(
                "These recordings cannot be shown together",
                f"{_letters_text(who)} hold the same channel at different "
                f"sampling rates, so they cannot share one timeline. {_ONE_AT_A_TIME}",
            ),
            who,
        )
    if "must not overlap" in text:
        who = _involved(readers, rec_channels, letters, "overlap")
        return ViewerRefusal(
            "overlap",
            UserMessage(
                "These recordings cannot be shown together",
                f"{_letters_text(who)} hold data of the same channel at the same "
                f"time, so one timeline cannot show both. {_ONE_AT_A_TIME}",
            ),
            who,
        )
    return None


def open_recordings(
    paths: Sequence,
    passwords=None,
    cancel: CancelToken | None = None,
) -> ViewerData:
    """Open recordings for the viewer: a Reader each, plus a MultiReader for
    two or more. ``passwords`` is a list aligned with ``paths`` (or a mapping
    from path). Raises :class:`ViewerRefusal` (plain-language) when they
    cannot be viewed together. Runs inside ``library_warnings_caught()``."""
    import mef3io

    given = [os.fspath(p) for p in paths]
    if not given:
        raise ValueError("no recordings to open")
    if len({os.path.normcase(os.path.realpath(p)) for p in given}) < len(given):
        raise ViewerRefusal(
            "twice",
            UserMessage(
                "This recording was selected twice.",
                "The same recording appears more than once in the selection, and one "
                "timeline cannot show it twice. Nothing was changed. Select each recording "
                "once.",
            ),
        )
    pwds = _password_list(given, passwords)
    if len({p for p in pwds if p}) > 1:
        raise _passwords_refusal()
    shared = next((p for p in pwds if p), "")

    opened: list = []
    try:
        with stopped_by_library(), library_warnings_caught():
            for path, pwd in zip(given, pwds, strict=True):
                if cancel is not None:
                    cancel.raise_if_cancelled()
                opened.append(mef3io.Reader(path, pwd))
            starts, ends, rec_channels = [], [], []
            for r in opened:
                chans = list(r.channels)
                infos = [r.info(c) for c in chans]
                rec_channels.append(chans)
                starts.append(min((int(i["start_time"]) for i in infos), default=None))
                ends.append(max((int(i["end_time"]) for i in infos), default=None))
            # recordings without data sort last, so the letters of the others hold
            order = sorted(range(len(opened)), key=lambda i: (starts[i] is None, starts[i] or 0, i))
            readers = [opened[i] for i in order]
            paths_o = [given[i] for i in order]
            pwds_o = [pwds[i] for i in order]
            starts = [starts[i] for i in order]
            ends = [ends[i] for i in order]
            rec_channels = [rec_channels[i] for i in order]
            letters = [_letter(i) for i in range(len(readers))]
            empty = [letters[i] for i, st in enumerate(starts) if st is None]
            if empty:
                verb = "holds" if len(empty) == 1 else "hold"
                raise ViewerRefusal(
                    "empty",
                    UserMessage(
                        "Nothing to show",
                        f"{_letters_text(empty)} {verb} no signal data, so there is nothing to "
                        "show on a timeline. Nothing was changed. Open the other recordings "
                        "without it.",
                    ),
                    empty,
                )
            if len(readers) == 1:
                combined = readers[0]
            else:
                if cancel is not None:
                    cancel.raise_if_cancelled()
                try:
                    combined = mef3io.MultiReader(paths_o, shared)
                except ValueError as exc:
                    refusal = _classify(exc, readers, rec_channels, letters)
                    if refusal is None:
                        raise
                    raise refusal from exc
        data = ViewerData(paths_o, pwds_o, readers, combined, starts, ends, rec_channels)
        opened = []  # owned by `data` now
    finally:
        for r in opened:
            try:
                r.close()
            except Exception:
                pass
    return data
