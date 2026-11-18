# Design: signal viewer

**Status:** specification, built in three parts. Part 1: the window (toolbars,
canvas, overview strip, keys, montages, filter panel and response plot, jump).
Part 2: the recording band, boundary lines, banner, side panel, notes and
events (read-only: chips, bands, types, stepping) and the export range
(Copy times, the list, Send to Join or trim, and "View these recordings" in
the joiner). Part 3 (phase 2): edit mode for ADDITIONS (draft, undo, import
and export, save by append). Still to come: changes and removals of stored
notes as a new copy (phase 3, mef3io#163). Supersedes the "Signal viewer"
section of [next directions](next-directions.md) and the viewer part of its
"Annotations" section.

See the signals of one or more recordings, with their annotations, quickly
and without changing anything. It is the reference viewer for MEF 3.0 data
written by mef3io, and the place where people decide **where to trim and what
to merge** before using "Join or trim".

The look and the controls follow the clinical review viewer that our lab
uses for epilepsy-monitoring review, so people who know one know the other:
- the same physical scales (mm/s, µV/mm, mm row spacing);
- the same keys and mouse gestures;
- the same colour profiles and annotation bands.

It is a native part of the app (Qt), not a web page. The reasons: no
embedded browser (installers stay small, and the bundle keeps its no-network
rule), and all signal processing runs in numpy beside mef3io.

## Principles

- **Read-only by default.** Viewing never writes. Edit mode is an explicit
  switch, and even then nothing reaches a recording until **Save**.
- **Never delete stored data.** A recording only ever gains annotations in
  place (mef3io appends). Removing or changing an annotation that is
  already stored produces a **new, checked copy**; the original stays as it
  was.
- **Always say which recording you are looking at.** With several
  recordings open, every point on the screen belongs to one of them, and
  the viewer shows which one.
- **Never load a whole recording.** Reads are windowed; the data on screen
  plus a bounded look-ahead, under a memory budget.
- **The window never freezes.** Reads, filtering and drawing preparation run
  on worker threads (mef3io releases the GIL on every long call); the UI
  thread only paints.
- **Plain language** (docs/ui-language.md). Filter design details sit behind
  "Technical details".

## Opening

- From the explorer: select one or more recordings → **View signals**. From
  "Join or trim": **View these recordings**.
- **One recording:** `mef3io.Reader`.
- **Several:** `mef3io.MultiReader`, which reads them as one timeline
  without merging anything on disk. MultiReader refuses recordings whose
  data overlap on the same channel or whose rates differ for one channel.
  The viewer then explains this in plain words and offers to open them one
  at a time. Recordings that hold *different channels* of the same period
  are accepted (side-by-side merge preview).
- **Also one `Reader` per recording.** The viewer opens one per recording
  beside the MultiReader. MultiReader's `segments()` and `records()` don't
  say which recording a result came from, so the per-recording Readers
  provide the ownership map (their own `segments(channel)`) and the
  annotations, each tagged with its recording. The combined reads go
  through the MultiReader. With mef3io 2.0's parallel open the extra opens
  cost little, and the opens are one job with one progress bar.
- **Nothing is inspected at opening or reopening.** Edit mode's save reads
  what it needs (protected or not, access level) once, on a task thread, when
  the save is first asked for; the fingerprint it compares with is taken at
  opening from what the Readers already read.
- **Passwords:** the explorer's, per recording. `MultiReader` takes one
  password for all of them. If the recordings need different passwords,
  the viewer says so and offers to open them one at a time; per-recording
  passwords in MultiReader are [mef3io#164](https://github.com/bnelair/mef3io/issues/164). An encrypted recording
  opened without the full-access password shows signals but no
  annotations, and says why.
- **How it fits the app:**
  - `RecordingSource` gains `open_viewer(paths, passwords)`, which returns
    the viewer's data object, so a remote source can implement it later.
  - The opens run as one `JobManager.submit` job, inside
    `library_warnings_caught()`. They run in-process: mef3io 2.0 releases
    the GIL, which was the reason for the `isolate` helper.
  - Page reads are too small and too many to be jobs. They run on the
    viewer's own scheduler, without the process-wide quiet lock (it would
    serialise every read).
  - `Reader.read` emits no block-copy warnings, and open-time problems are
    taken from `problems()`. Any other mef3io warning during a read is
    filtered by category (`mef3io.MefWarning`) and counted in the status
    line, never logged with its text.

## Layout

From top to bottom, as in the clinical viewer:

1. **Toolbar row 1:** previous/next page, day + time-of-day + Go, Jump
   (Ctrl/Cmd+K), Annotations (n), the **Edit mode** switch (off),
   **Export start / Export end**, and help (?).
2. **Toolbar row 2:** Montage, Channels (C), Spacing, Sensitivity + Auto,
   Timebase and, read only beside it, **how much time is on screen**
   ("= 10 s"), Filters (summary text, opens the filter panel), Colour profile,
   Types (T).
   - **The page length label** is `vv.format_length(page_us())`, a plain-words
     formatter in `model/viewer_view.py`: "800 ms", "2.5 s", "10 s",
     "4 min 30 s", "12 min", "2 h 10 min", "1 day 6 h" (a tenth of a second
     under 10 s, whole seconds under 10 min, whole minutes under a day, whole
     hours after; a value that rounds up into the next unit is written in
     that unit). It is updated from the page refresh (so by the timebase,
     Alt+1…5, Ctrl+wheel and a resize), at once from the canvas's resize (before
     the refresh's 120 ms delay), and when the window moves to a display with
     another resolution (`QWindow.screenChanged`: the same millimetres hold
     another number of pixels). It also shows in the overview. Tooltip: what
     it is, and that a wider window shows more time at the same timebase.
3. **Overview band (above the strip, 22 px per channel, up to 4):** the
   whole recording's min/max per pixel column for the chosen shown channels
   (default: the first shown), each lane scaled to its own 99th-percentile
   range; gaps empty; the page frame continues from the strip over the band;
   click/drag shares `TimelineNav` with the strip. The band is the ONLY
   picture of the signal over the whole recording (the strip no longer draws
   a faint envelope of its own: it would show the same thing twice). It shares
   ONE index read with the strip: after the first page has been asked for, one
   multi-channel `block_envelope` call (`ViewController.request_strip_envelope`
   (strip channels, band channels)) on the viewer's own reader (no second open)
   gives both the band's columns and the strip's real stretches, on the strip's
   own thread, waiting until the page and its look-ahead are loaded
   (`_wait_quiet`, at most 20 s) so it never delays them. Every channel read is
   cached for the open recording (4096 columns per channel, reduced to the width
   when drawn): paging never reads it again; a channel picked later is the only
   one asked for (`request_overview`, on a thread of its own, `viewer-pick`: it never
   waits behind the automatic read, which may sit in its quiet-wait for seconds or read
   a multi-day index for minutes); a save that changed the
   signal clears the cache and reads again. Code: `viewer/overview.py`.
3a. **Overview strip (32 px):** the whole timeline.
   - Data drawn per recording in that recording's colour, with gaps
     hatched.
   - The current page drawn as a box.
   - The export range shaded.
   - Day/time ticks.
   - Click or drag to go there.
4. **Recording band (with 2+ recordings):** a thin bar above the traces. It
   is coloured and lettered (A, B, C…) by **which recordings hold data**
   in each part of the page (one letter, or several side by side when
   recordings hold different channels of the same period). Labelled
   boundary lines run through the traces.
5. **Annotation chips row**, then the **trace area** with the channel-name
   gutter on the left and the time axis below.
6. **Side panel (toggle):**
   - **Recordings** tab: letter, colour, name, span, channels.
   - **Annotations** tab: list, search, type filter.
7. **Status line:**
   - the time under the pointer;
   - **"Recording B: <name>"** for the row under the pointer;
   - montage, reference and filter summary. In overview mode it reads
     "Overview: as recorded, no filters"; the chosen montage and filters
     are kept and come back on zooming in.

Colour profiles: White, Green (paper style with minor/major grid), Dark,
High contrast. They follow the app theme by default.

**Your own colours…** (the fifth choice, `custom`):
- Two Qt colour dialogs, in turn: the background, then the trace. Choosing it
  again offers the colours in use. Stopping either dialog changes nothing.
- Every other palette field (text, muted text, time grid, gap shading, cursor,
  selection, dimmed trace and the `dark` flag) is **derived** in
  `model/viewer_colours.py`, a pure function of the two colours:
  - `dark` = white reads better on the background than black does;
  - text = a soft ink if that reaches 7:1, else pure black or white (always at
    least 4.6:1); muted text keeps 4.5:1 where it can, never below 3:1;
  - the accent (cursor, selection) is the usual blue, moved towards black or
    white only as far as 3:1 needs;
  - the dimmed trace is the trace moved towards the background, but never
    below 3:1 (so with a trace that is barely 3:1 itself, the pale stretches
    look like the trace);
  - the grids and gap shading are quiet shades of the ink on the background.
- **Two colours closer than 3:1 are refused**, in plain words beside the list
  ("Too close to read: not applied.", the whole sentence in its tooltip and in
  the status line), and nothing changes; the list shows the profile in use.
- **Recording letters, note types and the overview strip** are drawn by
  `viewer_colours.legible_on(palette, colour)`: on a custom palette a colour
  under 3:1 is moved towards black or white just far enough; the built-in
  profiles are never touched (the light variant on a light background, the dark
  variant on a dark one, by the derived `dark` flag).
- **A colour choice only repaints.** It does not ask for a page, read a tile or
  compute a block (`_profile_chosen` sets the state and calls `_apply_profile`;
  a test counts reads, requests and page refreshes). This holds for the
  built-in profiles too.
- **Saved** as two declared preferences, `viewer/custom_background` and
  `viewer/custom_trace` (`#rrggbb` or empty; colours only, nothing from a
  recording), and only when the user chose them. On load each is validated
  (`viewer_colours.clean_pair`, then the 3:1 rule); a pair that is empty,
  edited into something else, or too close means the **white** profile.

## Knowing which recording is which

- **Assignment:** each recording gets a fixed letter and colour for the
  window's lifetime, in time order of its first sample.
- **Ownership is per channel:** at a given time, a channel's data comes
  from at most one recording (MultiReader refuses overlaps on a channel).
  Different channels may come from different recordings.
- **The map:** built from each recording's own `Reader.segments(channel)`
  (start/end per segment). It is shown in the overview strip, the
  recording band (the set of recordings holding data in each span), the
  side panel, the status line (for the row under the pointer) and the
  channel-name gutter (a small letter tag when 2+ recordings are open).
- **Crossing a boundary:** when the set of recordings on the page changes,
  a banner says **"Now showing recording B: <name>"** (or "recordings A
  and B"). It fades after a few seconds and is also announced to screen
  readers.
- **Mixed pages:** a page holding two recordings shows both letters.
- **Names follow privacy:** while a recording's patient details are hidden
  (the explorer's per-recording mask), the viewer shows its **letter
  only**, in the banner, status line, tooltips, screen-reader text and
  side panel too. Names and notes are never logged. Names are registered
  with `register_identity()` from the open job's thread.
- **Merge previews:** where recordings meet, the gap between them (or the
  fact that they touch) is labelled with its length.

## Navigation

The clinical viewer's keymap. The help overlay is generated from the same
table, so it can't drift.

| Keys | Action |
|---|---|
| → / ← | page forward/back |
| Shift+→/← | half a page |
| Alt+→/← | 1 s |
| Home / End | start / end |
| ↑ / ↓, + / − | sensitivity |
| Ctrl+↑/↓ (Mac: Alt+↑/↓) | row spacing |
| Shift+↑/↓, PageUp/PageDown | channels by screenful |
| Alt+1…5 | timebase 10/15/30/60/120 mm/s |
| [ / ] | previous/next annotation |
| { / } | previous/next recording boundary |
| g | go to time |
| Ctrl/Cmd+K | jump |
| c / t / m | channels / types / annotation list |
| b | next montage |
| f | filter panel |
| s / e | set export start / end at the pointer (or the selection's edges) |
| a, 1…9 | new annotation (edit mode only) |
| Esc | clear selection / close panels |
| ? | help |

Mouse:
- **Wheel** scrolls time.
- **Shift+wheel** scrolls channels.
- **Ctrl/Cmd+wheel** (or pinch) zooms the timebase around the pointer.
- **Drag** pans.
- **Shift+drag** measures: Δt, 1/Δt, and the amplitude of the channel
  under the pointer, from the min/max envelope, so spikes are exact.
- **Double-click** centres the page there.
- **Right-click** opens a context menu: set as reference, hide
  channel/lead, show only this lead, and export start/end here.

Held keys repeat navigation only, never writes.

**Page length:**
- The page length is physical: page = plot width in mm / timebase.
- Beyond the clinical 300 s cap, zooming out continues into an
  **overview mode**, drawn from the block min/max (see Data).
- The time axis says plainly when the view is an overview.

**Jump grammar** (the clinical one):
- `HH:MM[:SS]` = the next occurrence (Shift = the previous one);
- `day 2 08:00`;
- `YYYY-MM-DD HH:MM`;
- `+10m`, `-90s`, `+2h`;
- `A`, `B`… = the start of that recording.

**Times:**
- Shown in the site time zone via `model/timefmt.py`; UTC, labelled, until
  one is chosen.
- To the millisecond in readouts and annotation times; whole seconds on the
  axis.

View settings (scales, montage, filters, hidden channels) are remembered per
window while it is open. Only *preferences* are saved, through
`common/settings.PREFERENCES`: the default timebase, sensitivity, colour
profile, filter presets (cutoffs, steepness, timing, mains), the quick
annotation types and the memory budget. Never per-recording state,
which would put recording identity into preferences.

## Montages

Three, cycled with **b** or chosen in the toolbar:

1. **As recorded:** every channel unchanged.
2. **Referenced to a channel:**
   - Every channel is shown minus one channel the user picks, from the
     toolbar list or by right-clicking a channel name → "Use as reference".
   - Labels read `LA3 − Cz`; the reference row itself is hidden (it would
     be flat).
   - The reference is subtracted sample by sample **before** filtering.
   - Where the reference has no data (NaN), the difference is NaN too.
   - Channels at a different rate from the reference are shown as recorded
     and marked "not re-referenced (different rate)".
3. **Lead bipolar:** the clinical rule.
   - A lead is the name without its trailing contact number (`LA3`, `LA_3`,
     `LA-3` → `LA`; `A1'` and `A'1` → `A'`).
   - Each contact is paired with the next contact number on the same lead.
   - Gaps in numbering and duplicates are not paired.
   - Unpaired and unnumbered channels (ECG, EMG) stay as recorded.
   - Labels are made unique.

Montage computation lives in `model/` (pure numpy, no Qt) and is unit-tested
against hand-made channel lists, including the edge cases above.

## Filters

**Controls** (filter panel, **f**):
- **High-pass and low-pass:**
  - Each has an on/off switch.
  - The cutoff is **any value**: a number field plus a logarithmic slider.
    The slider runs from 0.01 Hz to just under half the sampling rate,
    clamped per channel.
  - Each has presets: HP 0.1 / 0.5 / 1 / 5 Hz; LP 30 / 50 / 70 / 100 /
    300 / 1000 Hz.
- **Mains filter (notch):**
  - Off, 50 or 60 Hz.
  - Width: narrow / normal / wide.
  - "Also remove harmonics" (2×, 3×… below the low-pass, or below Nyquist).
- **Steepness:**
  - Gentle (order 2), Standard (order 4), or Sharp (order 8).
  - Technical details show "Butterworth, order N". In zero-phase mode it
    is applied twice, so they say "order N, applied twice (as steep as
    order 2N)".
- **Timing:**
  - **No time shift** (default, zero-phase): best for reading timing
    between channels.
  - **As the recorder shows it** (single pass, causal): the waves shift
    slightly, as on the acquisition system.
- Settings apply to all channels.
- The toolbar shows the summary, e.g. `0.5–70 Hz, 60 Hz notch`.

**How it is computed** (`model/dsp.py`, numpy only, no scipy):
- **Method:**
  - Each NaN-free run of a channel is filtered in the frequency domain:
    real FFT, multiply by the response, inverse FFT.
  - The response is the analog Butterworth / notch prototype mapped to the
    sampling rate with the bilinear transform, evaluated exactly on the FFT
    grid.
  - Zero-phase mode multiplies by |H|² (the same result as filtering forward
    then backward). Causal mode multiplies by H.
- **Why frequency domain:** the classic difference equation becomes
  numerically fragile at extreme ratios, such as a 0.5 Hz high-pass on
  8 kHz data, which is a cutoff at 1/16000 of the rate. The FFT form has no
  such problem, and it handles any cutoff value without a design table.
  Arithmetic is float64, and FFT lengths are rounded up to 5-smooth sizes.
  No mean removal is needed: the odd-reflection pad (below) already makes
  the edges continuous.
- **Edges and context:**
  - Each read fetches a **margin** on both sides of the page and discards it
    after filtering.
  - The margin is computed from the filter itself, as its **settling
    time**: the time after which the step response stays within 0.1% of
    its final value (relative to the step height). It is computed for the
    response **actually applied**: |H|² in zero-phase mode, whose impulse
    response is symmetric and about twice as long as H's, measured from
    its centre on both sides; H in causal mode. It covers high-pass,
    low-pass and notch (with harmonics) alike, since a narrow notch's
    ring-down shows up in its step response too. It is found once per
    (filter settings, rate) from an inverse FFT and cached.
    - The impulse-response peak would be the wrong measure: a high-pass's
      impulse response is a spike at zero plus a long, tiny tail, so it
      decays "immediately" while the filter takes seconds to settle.
  - The margin and the pad are capped at 60 s. If the cap bites, whatever
    the filter (a 0.01 Hz high-pass, a very narrow notch), the panel says
    the edges of the page may not be fully settled.
  - Runs are padded by **odd (point) reflection** about their end samples,
    `x[-k] = 2·x[0] − x[k]`. It continues the signal's value and slope
    through the edge, so no filter type sees a step or a kink there (an
    even reflection leaves a kink, and zero padding a step).
    - Pad length = min(settling time, run length − 1).
    - A run shorter than the settling time is not filtered (see Gaps).
  - The FFT length is at least run + both pads + one more settling time of
    zeros, so the circular wrap of FFT filtering stays below 0.1% at the
    kept page (exactly the settling criterion), also when the cap bites.
  - Causal mode needs the margin and padding before the page only.
- **Gaps:**
  - NaN gaps are never filled with zeros.
  - Gaps of up to 2 samples are bridged linearly before filtering and put
    back to NaN after.
  - Longer gaps split the channel into runs that are filtered separately.
  - A run shorter than the filter's settling time is drawn unfiltered and
    dimmed. Technical details explain why.
  - **The explanation on screen:** when the page on screen holds such samples
    (`ViewPiece.has_unfiltered` over the visible rows and the page's time
    only), one plain sentence is shown in the filter window and in the status
    bar: "Paler stretches are too short to filter with these settings (shorter
    than N s) and are shown as recorded." N is the settling time of the rate of
    the rows that have such samples (the longest, when several). It is off
    when the filters are off and in the overview, and it keeps the last
    sentence while a row of the new page is still loading, so it does not
    flicker.
- **Large pages:**
  - Above about 2 million samples per channel (an 8 kHz page past ~4
    minutes), filtering is done in overlap-save chunks with the same
    margin.
  - In overview mode, filters and montages other than "as recorded" are
    off: min/max envelopes cannot be filtered, and the minimum of a
    difference is not the difference of the minimums. The toolbar says so.
- **Performance target:**
  - The first filtered page of 64 channels × 30 s at 8 kHz (0.5 Hz
    high-pass, 70 Hz low-pass, 60 Hz notch) appears within about 0.5 s on
    a 4-core laptop, and paging after that is instant, because neighbouring
    view blocks are filtered ahead of time.
  - Measured in the first build: numpy's FFT releases the GIL, so worker
    threads scale. The time is in the FFTs.
  - With a low-pass on, the output is computed at a reduced rate directly
    in the frequency domain (a shorter inverse FFT, only when the response
    above the new Nyquist is below 0.1%). This loses nothing: the low-pass
    has already removed what the reduced rate could not hold.
  - Pinned by a benchmark test, run on request.
  - `tests/test_viewer_bench.py` (`MEF3IO_STUDIO_BENCH=1`, prints, asserts no
    time) measures the window itself: the first complete page, a page forward
    cold and warm, runs of pages one after another and 30 ms apart (forward and
    backward), a far jump, a filter change, a montage change and a screenful of
    channels, and the cache fill. Each run starts from an empty cache.

**Frequency response** (button **Show response** in the filter panel):
- It plots the combined response of the current settings for the sampling
  rate of the channel under the pointer. There is a rate picker when
  channels differ.
- The plot uses a logarithmic frequency axis from 0.01 Hz to Nyquist, and
  two views:
  - **"How much is kept":** % of the amplitude kept, 0–100%. The plain one.
  - **dB:** gain in decibels, with the −3 dB line.
- Vertical markers show the cutoffs and the mains frequency. Hovering reads
  out "At 2.0 Hz: 98% kept (−0.2 dB)".
- A summary in words, e.g.:
  - "Slow drifts below 0.5 Hz are reduced: at 0.1 Hz, 4% is kept."
  - "60 Hz mains: 0.1% kept."
  - "Brain activity between 1 and 60 Hz: at least 95% kept."
- For causal mode a third tab shows the **time shift** (group delay) in ms
  per frequency. In zero-phase mode it says "No time shift".
- The plot uses exactly the response the filter multiplies by (the same
  function), so plot and filter cannot disagree.
- **A cutoff means the same thing in both timing modes:** the frequency
  where 70.7% of the amplitude is kept (−3 dB) in what is actually applied.
  Zero-phase applies |H|², so its design cutoff is moved to make |H(fc)|²
  = 1/√2 rather than the 50% a naive forward-backward filter gives. A test
  checks that a sine at the cutoff comes out at 70.7% in both modes.

## Data: reading, tiles, prefetch, cache

All in `model/viewer_source.py` (the only viewer module that imports
mef3io), driven by a scheduler in `workers/`.

- **Tiles:**
  - Pages are served from tiles on a **sample grid**: sample index
    i = round((t − timeline start)·fs/1e6), per rate. Tile k holds indices
    [k·n, (k+1)·n), n = 65536.
  - `read()` anchors its grid at the t0 it is given and rounds to the
    microsecond, so a tile is read over its span widened by one sample on
    each side. Each sample is then placed by its index (from the read's
    own t0), and the samples the neighbouring tile also holds are
    dropped. The seams are therefore exact by construction, at any rate.
  - Seams are tested at 256 Hz, 1000/3 Hz and 8 kHz: no duplicated or
    dropped sample, and a page read across tiles equals one `read()` of
    the page.
  - Channels are read and cached in **fixed groups of 16** of the recording's
    own channel order (per sampling rate), whatever the montage
    (`viewer_pipeline.fixed_groups`). A view block lists the fixed groups
    holding its rows and their reference or bipolar partners, so a montage
    change finds its tiles in the cache and reads at most the fixed groups it
    has not read yet.
  - Read with `dtype="float32"` (scaled by mef3io, NaN gaps included).
- **Two levels:**
  - **Samples** (`read`): used while samples per pixel ≤ 64.
  - **Envelope** (`block_envelope`): the index's per-block min/max. It reads
    the index only, never the data file, and is used for overview mode and
    the overview strip. Before the index of a span is read, the strip shows
    segment-level coverage from the recordings' segment start/end times.
    The block level (about 1–10 s) is the finest the envelope gets.
- **Scheduler:**
  - A priority queue on a small thread pool (default: half the cores, at
    most 6).
  - Priorities: the visible page first, then the look-ahead (the
    `viewer/lookahead_pages` pages in the direction of travel, default 4,
    nearest first, and 1 behind), then the channel screens above and below.
    **No look-ahead or screen read starts while a visible read is queued or
    running.** The filtered blocks of the look-ahead pages are prepared too,
    on a compute pool of half the cores (2 to 8; `viewer/filter_workers`
    overrides), of which look-ahead takes at most two thirds. A new page
    cancels what was prepared for another direction at once. Idle for a
    second, the look-ahead deepens one page at a time while the cache holds
    less than half its budget, and only while the window is shown (not
    hidden or minimised) and no read has failed since the last page.
  - After a change of filters, montage or width every block is new work: only
    then, no look-ahead block is computed while a visible block waits or runs
    (they share the cores and slowed it down); plain paging is not held back.
    The decimation step (`dsp.max_out_step`) is designed once per (settings,
    rate), not for every page and group. Look-ahead blocks are aligned from
    the page's own start and length, never from the block's (that was 1 us
    off for an odd length). A block kept for the new page asks for its
    missing tiles again, so the stale sweep does not cancel them. A tile
    raised from look-ahead to visible wakes the idle workers.
  - The overview (envelope) reads the indexes of all its channels in one
    library call.
  - A jump or a direction change cancels stale look-ahead through mef3io's
    `CancelToken`.
  - A newer page that needs a tile already being read joins that read. If
    the read then fails, it is read once more for the newer page instead
    (the failure may predate it, for example a disk that came back); a
    second failure is reported. Without this, a page could stay empty after
    a brief read error until the next move.
  - A read error reaches the window later than it happened. It fails only
    the blocks asked for no later than the failure; a block asked for after
    it gets a fresh read of that tile instead.
  - Filtering and min/max-per-pixel decimation run in the same workers, so
    the UI thread receives ready-to-draw arrays.
- **Cache:**
  - An LRU by bytes. Default budget 512 MB, a preference.
  - Holds raw tiles, plus **filtered view blocks**: one page length,
    aligned to half pages on the sample grid, so panning and small moves
    reuse them. A view block is keyed by (block span, montage, reference,
    filter settings), not by tile, because its values depend on the
    margin's neighbouring tiles and on gap runs crossing tile edges.
  - Montage, gap runs and filtering are computed over the
    margin-extended window, never per tile.
  - Changing a filter re-filters from the cached raw tiles without reading
    again.
- **Drawing:**
  - A page that loads never blanks: each row keeps the picture it had, drawn
    at the times it holds (a row whose channel, montage partner or filters
    changed keeps nothing, and neither does a new zoom or width: its min/max
    bins would look blocky and real); the old picture is drawn faint from its
    first frame, so it never looks current; a corner spinner shows only after
    400 ms. A row whose block could not be read shows "This channel could not
    be read." and no trace.
  - QPainter on a backing pixmap, redrawn only when the view or the data
    changes.
  - One polyline per row: min/max per pixel column when there are more
    samples than pixels, otherwise the exact samples joined by straight
    lines.
  - Traces use a pen one device pixel wide (cosmetic, width 1): Qt's fast
    line path. Any wider pen (1.2 px, or 1 px on a 2x screen) is stroked
    as an outline, about 100x slower; with it, 64 channels took 2-4 s per
    move. On a 2x screen each trace is drawn twice, one device pixel
    apart, to keep the line weight. Polylines are built from numpy in one
    step (QPolygonF's stream form), not one point at a time. Measured on
    64 channels at 2 kHz: first page about 0.5 s (was 4 s), about 0.1 s
    per move at 1x and 2x. The backing pixmap is reused while its size key
    (width, height, scale) is unchanged, so fractional scales do not
    re-render on pointer moves. Pinned by
    `tests/test_viewer_trace_speed.py` (points exact, pen, ink on screen).
  - Gaps are shaded bands. Overlay items (cursor, selection, export range,
    boundaries, chips) go on a separate layer, so moving the pointer never
    repaints traces.
- **Bounded reading:**
  - A test pins, through `/proc/self/io`, that viewing a page reads
    approximately that page's data plus the margin and look-ahead, never
    the recording.
  - The overview strip reads the index only.

## Annotations

**Viewing** (default mode):
- **What is read:** `records()` at every level (session, channel, segment)
  of every open recording.
- **Display:**
  - Shown as chips in the type's colour.
  - Records with a duration (EDFA, Seiz) are drawn as bands; point records
    as lines.
  - Channel-level records are bound to their row.
  - The chip's tooltip names the recording it came from.
- **Types:**
  - The known MEF types (Note, EDFA, Seiz, SyLg, Epoc, Curs, CSti, ESti,
    LNTP) get fixed colours. Unknown types are grey.
  - The types panel hides or shows types per window.
- **Many notes:** when more notes fall in the view than can be told apart
  (more than one per 4 pixels), they merge per pixel column into one tick
  with a count and the colour of the most important type, and per 8 pixels
  into one chip; a draft note stays a line of its own. Tooltips are made when
  hovered, and the side list is filled in slices. "In view" is one rule for
  both ways of drawing: the note starts in the page, or began before it and
  lasts into it (an array compare against the notes' ends, never a walk over
  the look-back). Left out of the merge, because a tick would lose what they
  are: a note lasting more than a pixel (the 200 longest), a note that began
  before the page, and the draft's own notes (never counted twice). A side
  list cut short says "The first N of M" from its first row on.
- **Side list:**
  - Sorted by time, with search, type filter and recording letter.
  - Click to go there; [ / ] step through.
- **Privacy:** note text follows the explorer's per-recording privacy
  state.
  - While patient details are hidden, chips show the type only and the list
    shows "(hidden)", with the same **Show** button.
  - Note text is never logged.

**Adding notes quickly** (phase 5; the owner: "nothing fancy, the speed is a key").
The honest steps for one note, from "I see something" to "it is stored":

| | before | now |
|---|---|---|
| turn edit mode on | click the switch | none (`N` does it) |
| point at the time | 1 | 1 |
| ask for the note | `A` | `N` |
| write the text | click the text box (the form opens on the time), type | type (the field has the caret) |
| keep it | click "Add to the draft" | Enter |
| save | click "Save…" | Ctrl+S |
| confirm | read the preview, click "Add them to the recordings" | Enter on "Save" (one line) |
| **actions** | **8** | **5** (point, `N`, type, Enter, Ctrl+S) + Enter on the line; **4 + 1** with Ctrl+S inside the field |

- **`N`** (always available, key table `new_note`) adds a note at the pointer, or over
  the Shift+drag selection (a duration), and turns edit mode on when it is off (a
  recording packed in a `.mefd.tar` is refused in plain words, as before). A small
  field (`viewer/inline_note.py`) opens where the note will be, with the caret in it.
  Enter keeps (an empty field keeps a bare marker), Esc drops, a click elsewhere keeps
  typed words and drops an empty field. A type written first ("Spike: ...") is the type,
  by the convention above. The time is the one under the pointer **on the page shown**:
  every page turn asks the canvas again (`refresh_pointer`), so a note added after paging
  with the keys and a resting mouse lands on the new page; `EditMode._anchor` never returns a
  time off the page (a pointer time or selection from before a page turn is not used: it
  falls back to the middle of the page, where the field opens). When `N` turned edit mode
  on, Esc on its field (or a refused note with an empty draft) turns it off again; a mode
  the person turned on stays.
- **Offered for one key** (Alt+1 ... Alt+6, a click, or Up/Down through the texts): the last
  four texts typed in this window, then the types the recordings already hold (their quick
  labels) and the quick types. The texts are in memory for the window only and are never
  written (a note's text can hold patient details; `PREFERENCES` has no such key). The
  texts are offered only while the details of EVERY recording are shown (`EditMode.hints`);
  with any hidden none is offered, and they come back when the details are shown. The types
  list is made again when what is shown changes. `Hint.__repr__` hides its value.
- **Entering edit mode explicitly** (the switch, `A`, the digit keys, the right-click menu,
  the form) works as before; the form stays for the time/channel/length by hand.
- **Draft notes** keep their marks: dashed on the traces, *(new)* in the list, and a quiet
  "N notes not saved yet" in the status bar while any exist.
- **Save, fast:** Ctrl+S (edit mode; also inside the field). A draft that ONLY ADDS, to
  recordings that need no password typed and with nothing waiting for a choice or refused,
  asks ONE line ("Save 2 new notes into Recording A? A saved note can be removed again,
  but not brought back.", a letter while the name is hidden) with Save / Cancel
  (`EditMode.quick_save_text`). Anything else shows the full preview. Every check of
  `annotation_save` is unchanged: the recording moved, replaced or changed, the passwords.
- **No duplicates, one rule** (`annotation_draft.note_key`: time, record type, length, stored
  text, channel; and, once the recording a note goes to is known, that recording: a plan's key
  is `(recording, note_key)` and stored notes are compared as `(recording, key)` pairs
  (`Draft.stored_pairs`), so two recordings side by side can each hold the same note). The draft and the editor refuse a note equal to a stored one or to another
  draft one with one sentence; an import leaves such rows out and says how many; a save
  leaves a copy of a stored note out of the write and out of the draft; and the read-again
  after a stopped save (`items_now_stored`) matches with the same key (now including the
  length).
- **A save of notes does not start over.** The signal did not change, so `ViewerData.refresh_notes`
  reads only the notes again: one inspect per written recording must still give the
  fingerprint taken at opening, and the notes read back must be exactly the ones appended.
  The proof is no longer a third inspect after the write: `write_plan` takes
  `annotation_save.signal_snapshot` of each recording right after its check against the
  fingerprint of opening (the files below the channel folders, where the signal lies, as name, size
  and mtime_ns, found by depth and never by name; ONE `os.scandir` walk, no file opened) and again right after its write; `WriteResult.
  signals_same` says whether they are equal, and `refresh_notes(appended, signals_same)` skips
  the inspect for those (False = reopen as before; a recording not in the dict is inspected as
  before). The two checks before the write (`prepare` and the one right before the write) are
  both kept: they are the safety check, they run back to back inside the task AFTER the user's
  answer, and a result a moment old is not reused on purpose (the window between check and
  write is the thing the second check keeps short, and it also covers the time earlier
  recordings take to write). The limit of the snapshot: a change that keeps a file's size and
  its mtime (on a drive with a coarse clock) is not seen by it.
  The readers, `epoch` (so the tile cache and the prepared pages), the strip and the
  view stay; the canvas is never set loading; the draft is not blanked or abandoned before
  the write. When either test fails the window reopens as before (page waits, the strip read
  ends first, `reopen`). The baseline for the NEXT save is still the fingerprint taken at
  opening, so a recording changed meanwhile is refused as before. Measured on synthetic
  data, 128 channels, local disk: file opens of the data refresh 509 -> 262 (the one inspect
  is 260 of them; without it 2), the page's tile reads after the save 3 -> 0.
- **Many notes.** What the stored notes give (type, time, end as arrays) is made once per
  read of the notes (`model/viewer_display.py`); a change to the draft only inserts or flags
  a few entries, so adding a note to 60,000 stays near 20 ms instead of 235. A draft note
  is always listed (marked as not stored) and selected, also beyond the 5,000 rows a long
  list shows.
- **Draft notes: remove and change the text** with Delete / Backspace on the selected note
  and the menu on a note (right-click on its chip, its list row or its mark): *Change the
  text* (the inline field, filled) and *Remove this note*. Delete / Backspace remove a
  draft note only while the traces, the note chips or the notes list have the keyboard
  (`ViewerWindow._focus_on_notes`): on a combo box or a button the key does nothing.
  **Removing and changing a STORED note (mef3io 2.4 `remove_annotations`).** *Remove this
  note*, Delete and the notes list's Take out on a stored note ask ONE plain line ("Remove
  this note from Recording A? This cannot be undone.") and then run ONE task
  (`EditMode._remove_stored` -> `_run_stored` -> `annotation_save.remove_stored_notes`): the
  protection facts are read on a task if not yet known (`read_facts`), a protected
  recording asks for the password the app does not hold (`ask_other_password`; both are
  always passed, as for adding; checked with `inspect` first, like a save), then the task
  re-checks the recording against the opened fingerprint (`prepare(for_removal=True)`),
  finds the stored records that ARE the note (time, type, level, channel, text, length: read
  back from `data.annotations()`, so the library gets them as `records()` returns them),
  releases the kept-open signal files (`data.release_files()`), takes a snapshot of the
  signal files' names, sizes and times, makes ONE `mef3io.remove_annotations` call per
  recording (all or nothing; only that level's notes files are rewritten, never the signal
  or the details), compares the snapshot, and reads only the notes again
  (`ViewerData.refresh_notes` with the NET change, negative for a removal; else the old
  reopen). Same save lock as saving notes (`EditMode.saving`: the draft is frozen, the
  window waits for the task). **Stop** is the task's token (`for_library()`): before the
  library swaps the new notes files in, nothing changes and it puts everything back itself;
  a swap that has begun is finished (the library ignores a Stop from then on: a few
  renames) and the app then reports the removal as done, which is true. A note stored
  TWICE (identical records) is said so in the line, and the choices are one copy (the
  library call removes both with `expect=2` and adds one back in the same call, so nothing
  is half-done) or all of them. "One copy" is offered only for the kinds `add` can write
  back whole (`COPYABLE_TYPES`: Note, EDFA, SyLg); any other kind (Seiz, Epoc, Curs ...)
  holds body fields `add` drops, so its line offers only "remove all N" (refused as
  `copy-type` in the task too). **Changing the text** of a stored note stays what the
  draft already does (a changed version beside the struck-through original); at Save, one
  line asks ("Replace 1 stored note in Recording A with the changed version? The old wording
  cannot be brought back.", plus "N of them are stored more than once: only one copy of
  each is replaced" when so) and the stored change is written first, remove + add in ONE
  call, then what is new in the draft is saved as before. What an edit may and may not do:
  * only a Note, or an EDFA that has a length, can be changed (`can_edit_stored`); any
    other kind can only be removed, because the editor can only write those two (a Seiz or
    Epoc would turn into a plain note and lose its fields): the edit entry says so in one
    sentence and Save refuses a forced draft;
  * the record keeps its kind (`replacement_record`): a Note stays a Note unless the user
    gave it a length, an EDFA stays an EDFA unless the user took the length away;
  * the text is the stored text AS IT IS when it was not edited (a trailing blank survives a
    change of time alone: `compose_text` would strip it);
  * it stays in its own recording and level: the editor refuses a time at which that
    recording holds no data of the channel ("remove it and add a new note there"), and Save
    checks the same (`_stays_in`); there is no re-routing (an `add` also needs the level's
    notes file to exist: `add-level-missing` has words).
  Matching is linear: the stored notes are indexed ONCE by their exact content
  (`stored_index`, `mark_key`), and the window counts copies once per read of the notes
  (`EditMode._copy_counts`), so thousands of changes cost a dictionary lookup each (test:
  2,000 changes among 50,000 stored notes). Refusals: every `code` the library gives for
  this call has plain words with a next step in `annotation_save.REMOVAL_WORDS`
  (`describe_removal_error`); a test derives the codes from the installed library (its
  docstring and the code-shaped strings of its compiled part) and fails on any without
  words. An interrupted earlier edit (`edit-interrupted`, `edit-unsettled`,
  `edit-journal-inconsistent`, `mid-operation`) uses the wording of Explore and Check for
  problems, with the rescue command `python -m mef3io recover --apply <folder>` inside
  `technical()` (the app does not rescue). The message says exactly what happened per
  recording: "Nothing was removed", or "Recording B was not changed ... Already changed,
  and this cannot be undone: recording A". A folder that cannot be written to (read-only or
  no permission: the library says "cannot open for write" or "Permission denied") has its
  own sentence (`read-only`). The application's switch that skips the library's check for a
  program that is still editing is never used. A single-file (`.mefd.tar`) recording says
  so and nothing is asked. The mef3io pin is 2.4.0, which has `remove_annotations`, so
  there is no fallback for an older library. A recording whose notes edit was interrupted
  opens with its signal; the notes tab and the button's tooltip say why the notes are not
  there and what to do (`NOTES_EDIT_INTERRUPTED`).
- **A reopen that fails after a notes-only save** switches saving off (`stale`, the count line
  says so) because the notes shown are the old ones and a note added again would be stored
  twice. `begin_data_change()` is inside the guarded path of both reopen routes, so a raise
  there frees the draft.

**Edit mode** (switch in toolbar row 1; off on every open):
- **Draft:** a banner says "Edit mode: changes are a draft until you save".
  All edits go into an in-memory draft, with undo/redo (Ctrl+Z / Ctrl+Shift+Z).
- **Add:**
  - `a`, the context menu, or a digit key 1…9 with a quick type.
  - The quick types are a preference. Defaults: Note, Seizure onset,
    Seizure end, Spike, Artifact, Sleep.
  - The time comes from the pointer or the Shift+drag selection (a
    duration).
  - The type, the text, and either the whole recording or a channel are
    chosen.
  - **How they are stored:**
    - mef3io writes bodies for Note, SyLg and EDFA only. A point annotation
      is a `Note` and one with a duration is an `EDFA`.
    - The quick type is the start of the text, e.g. `Spike: <text>` (the
      app's own convention, readable by any MEF tool). The viewer colours
      by that label when present, otherwise by record type.
    - Seiz, Epoc, Curs, CSti, ESti and LNTP are display-only.
- **Edit and remove:** select a chip or a list row. Changed and removed
  stored records are shown struck through / outlined until saved, so
  nothing silently disappears.
- **Import:**
  - CSV or JSON, with columns or keys `start` (ISO time with zone, or uUTC),
    `end` (optional), `type`, `text`, and `channel` (optional).
  - Imported rows enter the draft, highlighted, after a preview showing
    counts, the time span, and rows that fall outside every recording.
- **Export:**
  - CSV or JSON of the visible annotations (or of all of them), in the same
    columns.
  - The times are written in the site time zone with the offset, plus uUTC.
  - The save dialog says the file holds note text and real dates, so it is
    identifying.
  - Export is only offered for recordings whose details are shown. While
    any are hidden, it asks to Show them first.
- **Save:**
  - **Only additions:** written with `Writer.write_annotations`, which
    **appends**.
    - **Routing:**
      - A channel annotation goes to the recording holding that channel at
        its time.
      - A whole-recording annotation goes to the recording holding data at
        its time. If several hold data then (side-by-side recordings), the
        user picks one in the preview.
      - An annotation that falls in no recording is refused, with the
        reason.
    - **Preview and confirmation:** Save first shows what will be written:
      per recording (name or letter), the number of annotations and their
      time span. It says plainly that **adding annotations to a recording
      cannot be undone in place**. They can only be left out later in a
      new copy.
    - **Why no marker or journal** (unlike renames and metadata edits):
      those protect in-place *rewrites*. An append never rewrites a stored
      record, so an interrupted save cannot damage what was there. mef3io
      refuses a torn annotation file until `recover_session` has fixed it,
      and the viewer then says so in plain words.
    - **Writer:** opened on the existing session with that recording's
      passwords (the full-access one for an encrypted recording).
    - **After Save:** the viewer reads the notes again (see "A save of notes does
      not start over"); only when the signal cannot be shown to be unchanged does it
      reopen the Readers and the MultiReader.
  - **Any change to or removal of a stored annotation (decided 2026-11, replaces the
    "new copy only" plan of mef3io#163):** done IN PLACE with mef3io 2.4's
    `remove_annotations` after one plain line that says it cannot be undone: see
    "Removing and changing a STORED note" above. The notes files of the level involved
    are rewritten crash-safely by the library; the signal and the details are never
    opened for writing, which the save checks with a snapshot of the signal files. A
    verified copy (`transform_session`) stays the way to change a recording WITHOUT
    touching the original.
- **Leaving with unsaved changes:** leaving edit mode or closing the window
  asks Save / Discard / Cancel.

## Export range (trim and merge)

- **Setting the range:**
  - **Export start** and **Export end** (toolbar, `s` / `e`, the context
    menu, or the edges of a Shift+drag selection) set a range.
  - The range is drawn on the traces and in the overview strip, with its
    length and the recordings it covers.
  - The edges can be dragged and nudged with arrows; times are exact to
    the sample.
- **Using it:**
  - **Copy times** puts the start and end on the clipboard, in the site
    zone with the offset plus uUTC.
  - **Send to Join or trim** opens that page prefilled with these
    recordings and this window.
  - The joiner remains where the copy is checked and written; the viewer
    never writes signals.
- **Several ranges:** **Add to list** keeps several ranges (e.g. one per
  day, to cut into pieces). The list can be exported as CSV or JSON for
  scripts, and sent to the joiner once it can cut into several pieces
  (next directions).

## Code layout

- `model/viewer_source.py`: opens Reader/MultiReader, the recording
  ownership map, tiles, envelope and records; the only mef3io import.
- `model/dsp.py`: the filter design, the response function, FFT filtering,
  the margin, gap handling and decimation. Pure numpy.
- `model/montage.py`: leads, bipolar pairs and the reference montage. Pure
  numpy.
- `model/annotation_draft.py`: the draft with undo/redo, CSV/JSON
  import/export, and the save plan (append vs copy).
- `workers/viewer_scheduler.py`: the priority queue, cancellation and the
  byte-budget LRU.
- `viewer/`:
  - `page.py`: the window/page, toolbars and side panel;
  - `canvas.py`: traces, overlay and measurement;
  - `strip.py`: the overview strip and recording band;
  - `filters.py`: the filter panel and response plot;
  - `keymap.py`: the key table plus generated help;
  - `dialogs.py`: jump, annotation editor, import preview.
- Widgets never import mef3io (an existing test enforces this).

## Tests

- **Fixtures:** synthetic sessions written by mef3io in tmp; no real data,
  ever.
- **DSP:**
  - A sine through each filter matches the response function within 0.5%,
    at 256 Hz, 1 kHz and **8 kHz with a 0.5 Hz high-pass**.
  - Zero-phase mode does not shift a pulse.
  - The causal delay matches the group delay.
  - NaN gaps stay NaN; runs are filtered independently; no step at the
    page edges with the computed margin.
- **Montage:** the lead parser and pairing cases, and reference
  subtraction with NaN and with mixed rates.
- **Source:**
  - Pages are byte-bounded (`/proc/self/io`).
  - MultiReader ownership spans and refusal messages.
  - Records come from all levels and all recordings.
- **Scheduler:** priority order, cancellation of stale look-ahead, LRU
  eviction under the byte budget.
- **UI (pytest-qt, offscreen):**
  - keys and wheel move the view;
  - the boundary banner appears when crossing recordings;
  - edit mode is off on open;
  - additions save by append (the reopened recording holds the old
    annotations plus the new ones);
  - a removal offers only a copy;
  - the export range reaches the joiner prefilled.
- **UI text:** passes tests/test_ui_language.py.

## Many channels and long recordings (phase 4)

The viewer must not make anyone zoom just to see that channels have signal,
nor lose their place in hundreds of channels or days of time.

**Traces on open (`viewer/canvas.py`, `viewer/window.py`)**

The guiding rule: the picture must never mislead about amplitude. A dead channel
looks dead; a trace that is cut, old, re-centred or not at the toolbar's
sensitivity says so on screen.

- **Centred, and the centre is held.** Each row has a held level at its middle
  (`TraceCanvas._levels`, keyed by channel and minus channel, in the data's
  unit): kept while the page's own level (the median, from a sample of at most
  a thousand points at fixed pseudo-random positions, `model/sampling.bounded_sample`: a
  regular stride locks to a periodic signal, a sine whose period divides it read at one phase
  only: `_RowShape.centre`) stays within half a row height (at the
  row's sensitivity) of it, otherwise the page's level is taken. So a small step
  in the level moves the trace (it is visible) and a big one starts the middle
  again; a DC offset of millivolts never moves a trace away from its row. The
  old picture that stands in while a page loads never moves the level, so it
  and the new picture agree. The levels are forgotten on a far jump (a page more
  than one page length from the last), when the overview starts or ends, and
  when the montage, the reference, the filters or the hidden channels change
  (`reset_levels`); rows that are gone are dropped. What was taken off is said:
  the hover line of a row (`status_row`) reads "shown 4.0 mV lower" when it is
  half a millimetre or more (`viewer_view.format_offset`). Measurements
  (Shift+drag) read the true values of the pieces.
- **A bounded band.** A trace is cut off at `CLIP_HALF_ROWS` (0.75) row heights
  above and below its middle, and the cut is marked with small arrows in the
  accent colour on that line (every `CLIP_MARK_SPACING_PX`), so a flat run along
  the line is never mistaken for the signal. The paler old picture is marked
  the same way, fainter.
- **Auto on open.** The sensitivity is fitted (`_auto_on_open`) on a complete
  page the first time one can be judged, only while the person has not chosen
  one (`"sensitivity" in _chosen`, or a saved `viewer/sensitivity`). A page that
  cannot be judged (`_page_spread` is None: flat, only gaps, fewer than
  `MIN_JUDGE_VALUES` = 200 values per row, `MIN_JUDGE_ENVELOPE` = 20 block
  extremes in the overview) leaves the default and the next complete page tries
  again (a page is not tried twice, and after `MAX_FIT_TRIES` = 5 complete pages it could not
  judge it stops and keeps the default; the overview switch arms it again); the first
  choice ends it, and so does a successful fit: it is kept (no page-to-page refit) until
  Auto, a chosen value or the overview switch. A row is judged from at most `JUDGE_SAMPLE` =
  8192 of its values (the same sampler), so the GUI thread pays about 0.3 ms a row, not 20. The spread is the
  98th percentile of the distance from the row's median, over the page's values
  or, in the overview, the blocks' lowest and highest; the fit is the median
  over the EEG rows in view. A fitted value is never in `_chosen`, so never
  saved. When it changed the value, the status line says so once ("Sensitivity
  set to 300 µV/mm to fit this recording."). Switching between a normal page and
  the overview (`_refresh`) arms it again while the value is still only fitted.
  The Auto button works in both, and says in the status line when it cannot
  decide.
- **Channels that are not EEG** (`montage.is_non_eeg`: ECG, EMG, EOG, DC, SpO2,
  trigger, ...; a channel of a real lead is never one) are placed after the
  others (`montage.non_eeg_last`, judged by the row's own channel, so `ECG − Cz`
  is an ECG row) and each has a sensitivity of its own (`DrawRow.own_scale`,
  `own_sens`): a step of the toolbar's list (`SENS_STEPS`) chosen from a robust
  stretch of its data (`_choose_own_scales`: when the row first has a page it can
  be judged from, and when Auto is pressed, a filter change, or when the overview starts or
  ends), then held across pages. The row is
  drawn at the coarser of its own value and the toolbar's
  (`TraceCanvas.row_sensitivity`), never finer, so a lead-off ECG of 0.2 µV noise
  is a flat line. Its gutter has a small second line ("500 µV/mm") whenever it
  differs from the toolbar's, and the hover line says it too.

**Many channels**
- With more rows than fit, the toolbar says **"Rows 33-64 of 256"** and a
  vertical scroll bar sits beside the traces (hidden when everything fits).
  Page Up / Page Down (already the next / previous screenful) and Shift+wheel
  keep working; the scroll bar and the keys set the same `row_offset`.
- **Spacing** has a first choice **Fit all rows** (`ViewState.fit_rows`): the
  distance is worked out from the rows and the window (a hair under the exact
  division, `FIT_MARGIN`, so the rows never add up to more than the plot) and never
  goes under `READABLE_SPACING_MM` (4 mm); beyond that the rest is reached by
  scrolling, never squashed. The canvas counts whole rows with the same tolerance
  (`rows_fitting`), so a fit shows every row and the "Rows a-b of n" label names
  the rows fully shown. "Fewer, larger rows" is a larger number in the same box.
  Remembered through the existing `viewer/spacing` preference (0 = fit all).
- Tile reads follow the rows on screen: blocks are asked for with the visible
  priority only for the groups holding rows in view (a test scrolls a
  200-row recording and checks which groups are requested).

**Leads**
- A **lead** (`montage.real_leads`) is a name prefix that, among ALL recorded
  channels, has at least `MIN_LEAD_CONTACTS` = 4 contacts, contact 1 among them
  (`LA1` ... `LA12` is one; scalp names `F3 F4 F7 F8`, `T3`-`T6`, `A1 A2`,
  `Fp1 Fp2` are not). A **10-10 cap** has four or more of `F1..F8`, `FC1..`, `C1..C6`,
  `P1..P8`: when the names include a midline scalp name (`Fz Cz Pz Oz Fpz AFz FCz CPz POz`, any
  case, with or without an `EEG ` prefix) the prefixes of the standard scalp set (`Fp AF F FC
  FT C T CP TP P PO O A M`) are never leads; depth leads in the same recording (`LA1..LA12`)
  still are. The window computes it once (`_leads`). A channel of a lead
  is EEG whatever its letters (`HR1`..`HR8`, `CO1`.., `DC1`.., `LOC` as lead names
  are neither sent to the end nor given a scale of their own).
- Rows of one lead in a row (runs of at least `montage.MIN_GROUP` = 3) are a
  group: a thin line above it, the lead named once (its first row, bold; the
  following rows show only the contact number, `DrawRow.short`; the row's real
  label is unchanged everywhere else). `montage.group_rows(rows, leads)` is
  pure. Only a row that is a channel as recorded is shortened: lead-bipolar and
  referenced labels (`LA2 − LA3`) are never shortened (the thin line stays). The
  first row on screen is always named in full (`TraceCanvas.row_name`) when its
  lead's first row is scrolled away.
- The channel chooser lists the **leads** (by the rule above) over the
  channels: tick or untick a whole lead; a lead with some channels shown is
  half ticked; the search finds a lead by its name or any channel in it.

**Long recordings**
- **Overview strip.** The data is drawn as pixel runs (`strip.pixel_runs`: at
  most one per pixel column, a few array operations per recording, never a
  loop over stretches). Local midnights are a line through the bar with the
  date; the first date also names the clock (the zone), as the main axis does, and
  so does the readout. The **real stretches with data** come from the block index
  of ONE channel PER RECORDING (the one that recording holds most of,
  `ViewerWindow._strip_channels`; the union of their stretches,
  `strip.block_stretches`, read once on the strip's thread after the first page
  and after the page and look-ahead are loaded, in the SAME call as the overview
  band's channels (item 3 above),
  `controller.strip_stretches` = `(stretches, channels read)`): a pause inside
  one recording is a gap, which the segment times alone do not tell. Time the
  channels read do not reach at all (`vv.unreached` of their coverage) keeps the
  segment coverage, so a recording without the first channel is never "no data";
  until the index has been read the strip uses the segment times. The read is
  ended (`controller.drain_strip`, its result dropped) before a save reopens the
  data, and a save of notes does not read it again when the coverage and the
  boundaries are as they were. Hover shows the time with its day and
  "There is data here" or "No data: a gap of 3 h" (`readout`, also the tooltip).
- **Signal files kept open (mef3io 2.4 `open_files`).** On a network drive every file open
  is a round trip, and paging reads the same few files again and again, so the viewer's
  reader (the `MultiReader` of several recordings: one budget for all) keeps up to N signal
  files open between reads; the wizards and Explore keep nothing open. `model/open_files.py`
  chooses N: two files per channel (the page's segment and the next one, for a page across a
  border), at most an EIGHTH of the process's descriptor limit (the library says "well under
  a quarter"; the app holds descriptors of its own) and never more than 128: Linux soft
  limit 1024 -> 128, macOS 256 -> 32 (the usual soft limit of a GUI program, assumed).
  Windows: 128, the number the library itself suggests there; the library opens operating-
  system handles on Windows (no C-runtime stream limit applies), and the library's notes say
  that a folder cannot be renamed or removed while a file in it is held. NOTHING was
  measured on Windows or macOS: the numbers are assumptions to be checked. A recording with
  16 channels asks for 32. Files are
  let go (`ViewerData.release_files`, any thread) when the window closes (SYNCHRONOUSLY at
  its very start, before anything is waited for: `_let_go_now` sets the number to 0 and
  releases, on a thread the window waits for at most 2 s; the window counts as holding its
  recordings, for Rename's refusal and for every task's release, until it has really shut
  down: the `released` signal, not `closed`), before the app
  writes into the recording (a save of notes, removing or changing a stored note), before
  EVERY job the app starts (`JobManager.prologue`, run on the job's own thread: renaming,
  joining, anonymizing, packing, "make them again" removing an earlier copy: none meets a
  held recording, which on Windows blocks a rename, a removal or a replace) and after
  `IDLE_RELEASE` (8 s) without a read (`ViewController._maybe_release_idle`, the library
  call on a thread of its own: a recording left open in a window is not locked). Rename also
  refuses a recording that is open in a viewer (`in_use_keys`). All reads count as reads
  (`ViewerData._reading`), so a read in progress is never idle. Measured by the system, not
  the library: `tests/test_viewer_open_files.py` counts the kernel's opens of the `.tdat`
  files (inotify) over three passes of the same six tiles of four channels: 72 without,
  4 with.
  **Windows (stated, not changed):** a folder cannot be moved or renamed while a file in
  it is held open, so on Windows another program cannot move or rename a recording that a
  viewer window shows while it is being read; it is let go of `IDLE_RELEASE` (8 s) after
  the last read, at once when the window closes, and before any task of this app. Seen in
  CI: two tests that renamed a recording under an open viewer failed with "Access is
  denied" until they released the viewer's files first (`ViewerData.release_files`, as the
  idle release does). On macOS and Linux a rename is not blocked.
- **Toolbars:** four short rows (Navigation; Notes and export; Display; View), not two
  long ones. A toolbar wider than the window loses its LAST controls to the ">>" menu,
  whatever the font, so no row holds more than a 1280 px window shows at a large font
  (tested at 9 to 15 pt): the colours (last of the last row) are the only control that
  may be pushed out. The axis under the traces is `2.6 lines + 10 px` high, so the time
  labels and the note under them are always more than half a line apart.
- **Keys and buttons:** Ctrl+Right / Ctrl+Left one hour, Ctrl+Shift+Right /
  Left one day (on macOS, where Ctrl+Arrow belongs to the system, the table is
  built for the platform, `keymap.build_keymap`: Alt+Shift+Right / Left one hour,
  Ctrl+Alt+Right / Left (Command+Option) one day, Alt+Up / Down for spacing), `.` / `,` the next / previous stretch with data (a jump over a
  gap; `vv.stretch_start`: the first start after the page, the last one before
  it). The six toolbar buttons show only where they mean something (an hour
  needs 2 h of timeline, a day 2 days, stretches two or more stretches).
- **Timebase** goes down to 0.002 mm/s (a wide window then holds three to four
  days); the "= 1 day 6 h" label already reads in days.
- **Go** takes a date and time (`2000-01-31 14:30`) or a date alone
  (`2000-01-31`, midnight) as well as a time of day beside the Day box; Jump
  takes the same.

**Paint speed (`viewer/canvas.py`).** A polygon is built by resizing a `QPolygonF`
and writing the points into its own memory (shiboken6 pointer, ctypes, a numpy
view: `_polygon_direct`), not through `QDataStream`; a one-time self-check
(`_direct_polygon_is_sound`) compares it with the stream build on small arrays
and falls back to the old way for good on any difference or error. A row's level
is a partition over a sample of at most a thousand points, and the unused spread
is gone. Measured on 44 rows at 2560 px: warm trace layer 55 ms -> 39.5 ms, cold
(shapes rebuilt) 80 ms -> 62 ms. Two experiments were NOT adopted: (1) drawing the
dense (min/max) rows without antialiasing saved a further 8 ms warm, but the
traces turn visibly jagged (screenshots compared), which a clinical picture should
not; (2) painting the next page's trace layer on a worker thread: with the trace
layer drawn for free (a stub) the held-key bench rows did not improve (3 runs of
each: the pages asked for rose from 14-16 to 18-20 of 20 and the last press took
longer to complete, because painting is also what spaces the requests), so a
prerendered picture cannot help there; it would also have to guess the exact page
of a held key and copy the canvas state to another thread.

**Notes carried into a page.** Notes that began before the page and last into
it are drawn one by one only up to `MAX_BANDS` (200); beyond that they are ONE
marker at the left edge, "N notes and events continue from before this page"
(a counted tick and a chip "N from before"): hover lists the nearest eight, a
click on the chip goes to the latest of them, `[` steps to them like to any
note. The counts of everything drawn still add up to the notes in view.

**A save that is stopped.** The save task (`edit_mode._start_save`) is guarded
from `begin_data_change` to `submit`; a task that ends without an outcome
(stopped before it ran, or from inside the library) frees the draft and the
window (`_save_ended`). The write loop reports its own stops and failures with
exactly the notes appended, so the notes file is either complete for every step
it reports (each step is one `write_annotations` call, stopped only between
calls) or, if the process dies, torn (the existing "left unfinished" message).
The flag "a write may have begun" is set only once the loop has asked and is
about to write (a stop answered at that question is before any write). When it
is set and no outcome was reported, the window stays locked while the recordings
are reopened and their notes read again off the GUI thread
(`_look_again`); every new draft item that is stored now (same time, type, text
and channel, counted against what was stored before: `items_now_stored`) leaves
the draft, and the window says how many. If that read fails, Save stays off
(`EditMode.stale`) and the window says in plain words that the recordings must be
opened again, so nothing is stored twice.

**Held keys.** A held navigation key (auto-repeat) moves the page at every repeat
but asks for the picture once per batch of repeats
(`ViewerWindow._refresh_soon`, `REPEAT_FRAME_MS`): the repeats that arrive
together (the window was painting) cost one request, and nothing is read or
computed for the pages passed. After a visible block is given up the held
look-ahead goes on in the same pump.

## Phases

1. **Viewer:**
   - one or more recordings, with the recording band, banner and side
     panel;
   - scales, keys, profiles;
   - the three montages;
   - filters with the response plot;
   - annotations read-only;
   - the export range with Copy times and Send to Join or trim.
2. **Edit mode, additions (built):** draft, undo, import/export, save by
   append.
3. **Edit mode, changes and removals (built, mef3io 2.4):** a stored note is taken
   out (one line, then one task) or replaced by its changed version (remove + add in
   one library call) in place; see above.
