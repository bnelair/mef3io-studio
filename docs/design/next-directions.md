# Next directions

These come **after** v0.1. Each needs its own specification before work
starts. This page records what is known so the v0.1 design does not close
any of them off.

## Signal viewer

**Goal:** scroll and zoom through the traces of a recording of any length,
with gaps and annotations visible.

**What makes it feasible on multi-TB recordings:** mef3io's index stores every
block's minimum and maximum. A zoomed-out overview can therefore be drawn from
the small index files alone, without decompressing any data. Zooming in
switches to a min/max envelope computed in C++, and finally to the samples
themselves. Planned in [mef3io#68](https://github.com/bnelair/mef3io/issues/68);
cancellable reads for fast scrolling in [mef3io#69](https://github.com/bnelair/mef3io/issues/69).

**Open questions for the spec:**
- **Local or server-backed reading,** or both (see Remote recordings below).
- **The rendering stack:** pyqtgraph/OpenGL in the app, or a web view.
- **Montages, filters and per-channel scaling:** how far a viewer should go before it becomes an analysis tool.

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
over gRPC. The app could open recordings that live on a server, which is useful
when the data must not leave a secure environment.

**Open question:** whether the explorer and viewer talk to a common
"recording source" interface backed either by a local mef3io reader or by the
server. If yes, v0.1's background-job layer should already sit behind such an
interface, so this is a design point to keep in mind now.

## Validate / repair / recover

mef3io already has a validator, targeted repair and recovery after an
interrupted write. The app can be a front end for them, with a dry run by
default and every write behind an explicit confirmation and backup.
