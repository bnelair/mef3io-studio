# Design: signal viewer

**Status:** specification. Built so far: the window (toolbars, canvas, overview strip, keys, montages, filter panel and response plot, jump) in `viewer/`, opened from Explore's "View signals". Still to come: recording band, banner, side panel, notes and events, export range, edit mode. Supersedes the "Signal viewer"
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
   Timebase, Filters (summary text, opens the filter panel), Colour profile,
   Types (T).
3. **Overview strip (32 px):** the whole timeline.
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
  - Channels are fetched in groups of 16. The montage's **reference channel
    and bipolar partners are always fetched with the group** that needs
    them.
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
  - Priorities: the visible page first, then the look-ahead (2 pages in the
    direction of travel, 1 behind), then the channel screens above and
    below.
  - A jump or a direction change cancels stale look-ahead through mef3io's
    `CancelToken`.
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
  - QPainter on a backing pixmap, redrawn only when the view or the data
    changes.
  - One polyline per row: min/max per pixel column when there are more
    samples than pixels, otherwise the exact samples joined by straight
    lines.
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
- **Side list:**
  - Sorted by time, with search, type filter and recording letter.
  - Click to go there; [ / ] step through.
- **Privacy:** note text follows the explorer's per-recording privacy
  state.
  - While patient details are hidden, chips show the type only and the list
    shows "(hidden)", with the same **Show** button.
  - Note text is never logged.

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
    - **After Save:** the viewer reopens the affected Readers and the
      MultiReader, so what is shown is what is stored.
  - **Any change to or removal of a stored annotation:**
    - Save offers **Save as a new copy** only, never an in-place change.
    - The copy is made with `mef3io.transform_session` (verified, original
      untouched), with the draft's annotation changes applied in the copy.
    - **This needs a mef3io feature first:** an annotation-edit option on
      transform, which removes or replaces given stored records, plus verify
      support for it. Tracked as [mef3io#163](https://github.com/bnelair/mef3io/issues/163).
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

## Phases

1. **Viewer:**
   - one or more recordings, with the recording band, banner and side
     panel;
   - scales, keys, profiles;
   - the three montages;
   - filters with the response plot;
   - annotations read-only;
   - the export range with Copy times and Send to Join or trim.
2. **Edit mode, additions:** draft, undo, import/export, save by append.
3. **Edit mode, changes and removals:** save as a new copy; after the
   mef3io transform annotation-edit feature.
