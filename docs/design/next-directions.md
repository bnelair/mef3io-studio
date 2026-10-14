# Next directions

These come **after** v0.1. Each needs its own specification before work
starts. This page records what is known so the v0.1 design does not close
any of them off.

## Signal viewer

**Goal:** scroll and zoom through the traces of a recording of any length,
with gaps and annotations visible.

**Built on mef3io-server.** The server's `FileManager` already does the
caching (a byte-budgeted per-channel tile cache), parallel decode and prefetch
a viewer needs. The app drives it directly, without gRPC, for local files,
and through the gRPC client for remote ones, behind one "recording source"
interface. See [architecture](../architecture.md), including what has to
change in mef3io-server first: encrypted sessions, a plain-Python API, and
logging hooks.

**What makes it feasible on multi-TB recordings:** mef3io's index stores every
block's minimum and maximum. A zoomed-out overview can therefore be drawn
from the index alone, without decompressing any data. The index is about 2%
of the data, though, which is tens of GB on a multi-TB recording. The
overview therefore starts coarse (from segment metadata), refines
progressively from the index of the visible span, and caches what it has
read. Zooming in
switches to a min/max envelope computed in C++, and finally to the samples
themselves. Planned in [mef3io#68](https://github.com/bnelair/mef3io/issues/68);
cancellable reads for fast scrolling in [mef3io#69](https://github.com/bnelair/mef3io/issues/69).

**Open questions for the spec:**
- **What mef3io-server must provide** for local use without gRPC: a password parameter, a plain-Python API next to the protobuf one, logging hooks, and PyInstaller-safe worker processes.
- **The rendering stack:** pyqtgraph/OpenGL in the app, or a web view.
- **Montages, filters and per-channel scaling:** how far a viewer should go before it becomes an analysis tool.

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
- **Which copy to trust:** the index's copy of a block's time and count can disagree with the block's own header, which mef3io treats as authoritative (mef3io#57). The coverage read must say which it uses and flag disagreements.
- **Overlaps:** overlapping blocks must not count twice, so coverage never exceeds 100%.

**Layers:**
- **mef3io:** a `coverage(channel, t0, t1, n_bins)` read, index-only, alongside the overview reads ([mef3io#68](https://github.com/bnelair/mef3io/issues/68)).
- **mef3io-server:** exposes it as an RPC, so remote recordings get it too.
- **Studio:** a channels × time heatmap, zoomable, with gaps and low-rate stretches highlighted and a hover readout (rate, % present, block count).

The explorer's v0.1 timeline (segments and gaps) is the coarse version; this
is the block-level one.

## Annotations

**Goal:** see every annotation (session, channel and segment level) on the
timeline, jump between them, then **create and edit** them.

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
driven without gRPC) or by a remote server. v0.1's background-job layer will
sit behind such an interface ([#2](https://github.com/bnelair/mef3io-studio/issues/2)).

**Open for the spec:** authentication, which operations make sense remotely,
and behaviour on slow networks. v0.1 makes no network connections; remote
access will be an explicit, opt-in choice.

## Validate / repair / recover

mef3io already has a validator, targeted repair and recovery after an
interrupted write. The app can be a front end for them, with a dry run by
default and every write behind an explicit confirmation and backup.
