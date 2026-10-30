# Next directions

These come **after** v0.1. Each needs its own specification before work
starts. This page records what is known so the v0.1 design does not close
any of them off.

## Save a part, cut into pieces, merge recordings

**Goal:** save a time range of a recording, cut a recording into pieces
(for example one per day), or merge several recordings into one, one after
another in time or side by side (different channels of the same period).
Every result is a **new, checked copy**; the original is never changed.
Planned as the first feature after the anonymizer test version, with the
user-side agenda in [#31](https://github.com/bnelair/mef3io-studio/issues/31)
and the library side in [mef3io#103](https://github.com/bnelair/mef3io/issues/103).

**Known constraints:** the channel picker is the anonymizer's; overlaps in a
merge are refused, never guessed at; a channel's sampling rate must be the
same in every recording that is merged.

**Built so far: Join or trim.** Saving a part of one recording, and joining
parts of several recordings one after another in time, are built (the
"Join or trim" page, on mef3io 1.5.1; see the
[user guide](../user-guide.md#join-or-trim-recordings) and `model/join.py`).
Still open: cutting one recording into several pieces in one go, and
joining side by side (different channels of the same period, which the
joiner refuses as overlapping parts).

## Signal viewer

**Specified:** see [viewer](viewer.md). It reads local recordings through
mef3io directly (Reader / MultiReader, windowed reads, block min/max from the
index) with its own tile cache and prefetch, rather than through
mef3io-server's `FileManager` as first planned: mef3io 2.0 now provides the
parallel decode, cancellation and index-only envelope that made the server
attractive, and going direct avoids a second process and the server-side
changes listed before. Remote recordings stay a later "recording source".

## Data coverage view

**Goal:** see at a glance **when data exists, at what rate, and where it is
missing**, for every channel. It's the first thing to check when inspecting a
recording: dropouts, gaps, channels recorded at different times, a block
rate that changes.

**Why it's cheap:** each `.tidx` index entry stores its block's start time and
sample count. Comparing samples present per time bin with the sampling rate
gives the coverage (0–100%) and the effective data rate. No samples are
decoded.

**Caveats for the spec:**
- **Scale:** the index is still about 2% of the data. Coverage over a whole multi-TB recording is computed coarse-first and progressively, and cached.
- **Which copy to trust:** the index's copy of a block's time and count can disagree with the block's own header, which mef3io treats as authoritative (mef3io#57). An index-only read can't see the headers, so the view says plainly: **the index copy is used; disagreements with block headers are not detectable in this read.** Detecting them would mean reading every block header, an optional, slower mode if the spec wants one.
- **Overlaps:** overlapping blocks must not count twice, so coverage never exceeds 100%.

**Layers:**
- **mef3io:** a `coverage(channel, t0, t1, n_bins)` read, index-only, alongside the overview reads ([mef3io#68](https://github.com/bnelair/mef3io/issues/68)).
- **mef3io-server:** exposes it as an RPC, so remote recordings get it too.
- **Studio:** a channels × time heatmap, zoomable, with gaps and low-rate stretches highlighted and a hover readout (rate, % present, block count).

The explorer's v0.1 timeline (segments and gaps) is the coarse version; this
is the block-level one.

## Annotations

**Goal:** see every annotation (session, channel and segment level) on the
timeline, jump between them, then **create and edit** them. Viewing and
editing in the viewer are specified in [viewer](viewer.md#annotations).

**Known constraints:**
- MEF defines several record types: Note, Seiz, Epoc, EDFA, and others ([mef3io#62](https://github.com/bnelair/mef3io/issues/62)).
- Writing them is a mef3io feature, not an app feature.
- Edits must follow the same rules as everything else: never modify an original without an explicit choice, and keep a record of what changed (mef3io's provenance region).

## Export

**Goal:** export a recording, or a selection of channels and a time range, to other formats.

**Candidates:**
- EDF/EDF+, the clinical interchange format;
- NWB, the neuroscience archive standard;
- CSV/NumPy for quick analysis.

**Open questions:** each format's limits must be stated plainly. EDF has fixed
record durations, and its 16-bit samples can lose precision against MEF's
32-bit samples. Export should probably live in mef3io or a companion library,
so scripts get it too, with the app as a front end.

## Remote recordings

[mef3io-server](https://github.com/bnelair/mef3io-server) streams MEF 3.0 data
over gRPC. The viewer talks to the server's `FileManager` directly for local
files and to the gRPC client for remote ones, behind one "recording source"
interface, so opening a recording that lives on a server needs no separate
viewer code. That's useful when data must not leave a secure environment.

**Decided:** the explorer and viewer talk to a common "recording source"
interface, backed by a local source (mef3io, or mef3io-server's `FileManager`
driven without gRPC) or by a remote server. The app already reads through
such an interface (`model.source.RecordingSource`, with one local
implementation; [#2](https://github.com/bnelair/mef3io-studio/issues/2)).

**Open for the spec:** authentication, which operations make sense remotely,
and behaviour on slow networks. v0.1 makes no network connections; remote
access will be an explicit, opt-in choice.

## Validate / repair / recover

mef3io already has a validator, targeted repair and recovery after an
interrupted write. The app can be a front end for them, with a dry run by
default and every write behind an explicit confirmation and backup.
