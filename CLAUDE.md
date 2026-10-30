# mef3io-studio — assistant context

Desktop app (PySide6) for exploring, organizing, protecting and anonymizing
MEF 3.0 recordings, built
on the mef3io library (github.com/bnelair/mef3io). Status: early development;
test version 0.1.0.dev4 = Explore + Anonymize on mef3io 1.5.1; Join or trim
(#31) built after it (ordering/renaming, metadata edit, passwords, channel
presets, cutting into pieces still to come). Design in
`docs/`, work in issues, library-side plan in mef3io#71.

Hard rules:
- The app NEVER reads or writes MEF bytes itself; only mef3io's public API
  (no `mef3io._*` imports). Missing capability → mef3io issue first.
- Originals change only when the user explicitly says so. Content changes
  (anonymization, encryption, channel subsets) ALWAYS write a verified copy;
  annotation ADDITIONS (viewer edit mode: append-only via write_annotations,
  preview + confirmation, no in-place undo; changes/removals only as a new
  verified copy, docs/design/viewer.md) and renames (via mef3io's rename_session: folder + session .rdat/.ridx + stored
  names + description where it equals the old name; fix_name_drift for drift)
  and metadata edits are explicit in-place operations with preview,
  confirmation and undo. Undo is a LOGICAL INVERSE (rename back / edit_metadata
  with the old values of changed fields), never a byte restore, and runs the
  same safety checks. TIMESTAMPS ARE NEVER EDITED IN PLACE — times change only
  through a verified transformed copy. GMT_offset is read-only in v0.1;
  edit_metadata gets field-level dicts only, never a Metadata dataclass.
- Crash safety never depends on optional artifacts: an opaque in-session
  "operation in progress" marker (no names/paths) + a MANDATORY journal in
  backup_dir; on completion delete marker first, then journal. Byte backups
  are crash rollback only and ALWAYS deleted. Keeping undo info afterwards is
  the only optional part, and it never lives beside the recordings.
- Recovery artifacts live in the per-user app-data dir under opaque names;
  paths and session names count as identifying (log filter, no recent-files
  list by default, no paths in clear in preferences: the work folder is
  stored sealed).
- Local time comes ONLY from a user-chosen site time zone via zoneinfo (one
  per app profile, plus a per-window override that is never saved); UTC,
  labelled, until one is chosen. Never from GMT_offset: one fixed number, in
  L2-encrypted section 3, and meflib ignores the DST fields.
- Never load a whole recording (sessions reach TBs) — use inspect/windowed reads.
- Privacy: no patient details or original dates in logs, reports, fixtures,
  screenshots; no network access in v0.1.
- Users include students and non-technical staff: plain language first,
  illustrations, technical detail behind a toggle.
- v0.1 scope = explore (incl. order/rename, pack/unpack), metadata view/edit,
  passwords, channel presets + batch conversion, anonymize; installers only
  (no PyPI).
  Save a part + join (#31) is built ("Join or trim", below); cutting into
  pieces, viewer (spec: docs/design/viewer.md; local reads through mef3io), coverage view, annotations, export,
  remote access are NEXT DIRECTIONS needing their own specs.

Code (app shell, #2/#3):
- Dev: `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`
  (Windows: `.venv\Scripts\activate`); `python -m pytest` (offscreen);
  `ruff check . && ruff format --check .`. CI installs from the hashed
  `requirements/*.lock` (regenerate with uv, see requirements/README.md). Installers:
  `packaging/mef3io-studio.spec` + `.github/workflows/build.yml`; every build
  must pass `mef3io-studio --self-test`. Artifacts (shared org storage quota): PRs
  upload only the Linux tarball, kept 1 day; other runs everything, kept 3 days. Not on PyPI, ever. Version = `VERSION`
  (read by `_version.py`, pyproject, the spec, the installers; a test pins
  `app_version()` to it); shown in the window title, the sidebar
  (`AppVersion`, "(test version)" for .devN/aN/bN/rcN) and About. Help → "Get
  the latest version…" only SHOWS `RELEASES_URL` (opens the browser, copies
  the link): the app never checks for updates itself (no network, ever;
  automatic updates were deferred by the maintainer on 2026-10-19, and if they
  come back they need a signed manifest with a maintainer-held key, see the
  Notepad++ 2025 updater hijack). Releases: README "Make a release". The
  `bump-version` workflow (`scripts/bump_version.py`: dev/release/patch/minor/
  major/explicit) opens a PR "Release vX" changing VERSION plus a notes stub;
  CI fails while `docs/release-notes/<VERSION>.md` is missing or still the stub
  (`test_release_tools.py`). Merging it releases: build.yml's
  `release-decision` publishes only when THAT push changed VERSION (diff
  `github.event.before`..sha) and v<VERSION> has no tag; `release` then tags
  exactly that built commit (`gh release create --target`). A failed release
  run is re-run, not re-merged (a tag already on THIS commit = finish that
  release; on another commit = never touched). A hand-pushed tag must match VERSION and be
  on main. Test versions are pre-releases, finished ones `--latest`. Each
  main push has its own concurrency group (never cancelled, even queued).
  A published release is never overwritten from another commit.
- Long work goes through `workers.JobManager.submit(title, fn, ...)` — never on
  the UI thread; `fn(ctx, ...)` reports via `ctx.report(done, total, phase)`
  (throttled) and polls `ctx.check_cancelled()`. Titles shown to users carry
  no patient data.
- Stop inside mef3io: `model.cancel.CancelToken.for_library()` gives a
  linked `mef3io.CancelToken`; `mef3io.Cancelled` is a stop (`is_stop`),
  never a failure. Helper calls that can stop by themselves use
  `ctx.run_isolated(fn, ..., stop_grace=STOP_GRACE_SECONDS)`: `fn` gets
  `cancel=`, and is ended only after the grace. mef3io byte progress travels
  from helpers as `model.progress.ByteProgress` via `send`.
- Data access goes through `model.source.RecordingSource` (server source later).
  `LocalRecordingSource.isolate = True` runs opens in a spawn helper process
  because it CONTAINS NATIVE CRASHES and gives a HARD STOP, not for the GIL:
  mef3io 1.4 releases the GIL in `inspect()` and during a `Reader` open
  (mef3io#79 closed; measured on 1.4.0, 64 ch x 60 parts: Reader open 0.78 s
  with an 8 ms longest stall, inspect 1.1 s with 33 ms). test_jobs pins the
  inspect-based summary on a thread; it skips, saying so, when the call takes
  <= 1.5x the stall bound. `freeze_support()` is the first thing the launcher
  does.
- UI text: everyday words only (docs/ui-language.md, enforced by
  tests/test_ui_language.py); jargon only behind "Technical details".
- Packaging: the spec DROPS network/kiosk Qt plugins, QtNetwork, GTK theme,
  readline, libstdc++; `packaging/check_bundle.py` enforces it in CI, plus
  THIRD_PARTY_LICENSES. Linux is built in AlmaLinux 9 (glibc 2.34 = PySide6
  6.12's own floor).
- Anything identifying the app learns → `common.privacy.register_identity()`,
  called from task threads (workers/tasks.py), never the GUI thread.
  Exception: note text is NOT registered (masked in the UI, never logged;
  thousands per recording).
  Never log paths or recording contents.
- New preferences must be declared in `common/settings.PREFERENCES` (UI only).
- Look: Fusion + palette + QSS in `common/theme.py`; icons are bundled SVGs
  with `currentColor`; no pixel font sizes. Radio buttons and checkboxes
  (also in item views, also disabled) are painted by `theme.StudioStyle`
  (a QProxyStyle over Fusion) in `Theme.indicator`: Fusion's own outline is
  window.darker() = invisible in dark (1.1:1). Same for the FRAMES of input
  fields: StudioStyle paints PE_FrameLineEdit and overdraws CC_SpinBox/
  CC_ComboBox (line/spin/date/time/combo, enabled/disabled/read-only) in
  `indicator`, accent 2 px on focus; QPlainTextEdit/QTextEdit via QSS.
  tests/test_theme.py pins >= 3:1 against every `indicator_backgrounds()`
  colour AND on rendered pixels (indicators and frames), both themes. QTreeView selection = accent_soft (like QTableView),
  so a box on a selected row keeps its outline.
- Explorer (#4): `model/inspect.py` is the ONLY place the EXPLORER reads a recording
  for display (the signal viewer reads signals through `model/viewer_source.py`,
  windowed and byte-bounded, see docs/design/viewer.md): a thin adapter over `mef3io.inspect(strict=False,
  include_values=True)` (mef3io 1.3). Unreadable parts -> `part_problems`,
  the rest shown; `offset_assumed_zero` (basic password) = unverified times;
  a non-ASCII password that matched only mef3io's pre-1.3 rule (#84) ->
  `legacy_password` note (inspect() does not report it: a lenient
  `Reader(strict=False)` open with warnings caught detects it, non-ASCII
  passwords only; `detect_legacy=False` where the note is not shown, e.g.
  archive's copy check). A disputed offset has no single value but
  `date_recoverable` still makes `uses_time_offset` true (date warning).
  mef3io warnings name the path: every mef3io call that may warn (inspect,
  the legacy Reader open, Validator, archive/extract) runs inside
  `model.quiet.library_warnings_caught()`, never logged. ALL
  `warnings.catch_warnings` in the app go through model/quiet.py under one
  lock (`ignoring_warnings` for the job layer's own): it is process-wide
  and not thread-safe. Only the self-test's Writer (synthetic, temp folder)
  is deliberately outside. A channel failed in several ways is listed once.
  Unreadable parts never pass as gaps: coverage marks the recording
  `incomplete` (timeline tag + note, never "time with data"); the CSV has an
  `unreadable_parts` column in both layouts and a "could not be read" row per
  part in the per-part layout. Patient details list channel descriptions
  without channel names (#96); mef3io 1.4's ChannelSummary does carry them
  (`ChannelInfo.description`, so far used only by the batch's ownership
  check). Never read/read_raw/toc;
  tests/test_explorer_bytes.py pins < 1% of bytes read via /proc/self/io in
  the helper. Widgets (`explorer/`,
  `common/`, `anonymizer/`) never import mef3io (tests). Pack/unpack (`model/archive.py`)
  writes to an opaque `.mef3io-studio-<hex>` temp name, checks the copy, then
  finishes without replacing anything. Patient details, descriptions and
  note text are masked until "Show" per recording; Copy summary also hides
  names and dates. RTO != 0 = deprecated date masking: warn, never call it
  anonymized.
- Combined timeline (explorer/combined.py + model/coverage.py): one row per
  recording, metadata only; verified times only in the figures/overlaps.
  Layout runs in slices on the GUI thread (workers/slices.py), NOT a Python
  thread: a computing thread stalls the window through the GIL (measured
  0.5 s). Painting is per pixel column (coverage.pixel_runs). CSV export
  (model/export.py): warn first, Show policy for patient details, safe cells,
  temp file + rename. Every Explorer table aligns headings like cells (test).
- Anonymizer (#5): `model/shiftplan.py` is the time-shift PLANNER (step 2),
  pure data in/out: `plan_shifts([RecordingTimes], ShiftSettings) ->
  ShiftPlan` with per-recording `shift_us`, warnings/blockers/refusals as
  enum CODES (words belong to the UI). It never imports mef3io or logging;
  original times and shifts are `repr=False` (the shift re-identifies).
  `TIMES_UNVERIFIED` = exactly what mef3io#70 refuses (disputed or unreadable
  offset, a time at/before a non-zero offset); a resolving rto != 0 IS
  planned. No full-access password = LOCKED. The floor's span covers notes
  too (`span_start_us`/`span_end_us` from inspect's first/last_time).
  The wizard (`anonymizer/page.py` + `picker.py`) runs on mef3io 1.4.0
  (pinned): `anonymize_session`, `anonymize.check` (writes nothing), refusal
  `.code`s and the report's original times. Model in `model/anonymize.py`
  (CopyRequest/CopyResult, `make_copy` in a helper; the report crosses back
  via `with_original_times_in_pickle()` THERE ONLY, in memory, never
  persisted; `check_names` (name step: `check()` with a placeholder
  destination, path codes ignored) / `check_copies` (batch up-front: the real
  destination); no output passwords ever passed to `check()`;
  `describe_copy_error` = `model/refusals.py` words for EVERY code of
  `REFUSAL_CODES` (test) + class fallbacks; details via `technical_text`: no
  paths, no recording name, `scrub()`ed; `password_may_not_work_elsewhere` =
  `mef3io.password_interop_issue`; `write_date_key` (0600, never next to
  the copies)). The old name-check scratch folders of pre-1.4 dev builds are
  swept once at start. Caught mef3io warnings are matched by
  `MefWarning.path` (data), never by text.
  Picker rules in `model/channels.py` (electrode grouping strips EEG/POL,
  -Ref/-AVG, bipolar pairs, case; ranges only over rows on screen; group box
  = shown members while filtering; batch change carries exclusions by name).
  Tasks: `workers.tasks.anonymize_recordings` (publishes `Leftover` when a
  `.partial` survives) / `check_copy_names`. The date key is shown on screen
  and saved only on opt-in; never logged, never in settings. Passwords: the
  form clears on start/restart/hide; only the copies' full-access password is
  kept for "Open the copy". Privacy check card: `model/privacycheck.py` +
  `explorer/privacy.py`, from inspect() only; NEVER green while an identity
  row is unknown (amber); "made by the anonymizer" = every part's LAST
  operation is `anonymize` (the ops set is inherited and survives appends).
  Unattended batch (`model/pipeline.py`, `workers.tasks.run_batch` /
  `preflight_batch`): Output step = folders | archives (per-recording
  `.mefd.tar` via model.archive) | bundle (`model/bundle.py`, deterministic
  `tarfile`, a CONTAINER the app cannot open, never a merge). Work copies
  in `<work folder>/work-<batch>-<n>/` (one at a time), deleted ONLY after a
  SHA-256 byte check of every member of the written archive against the
  checked copy (mismatch = copy kept, item failed). All checks run BEFORE
  starting, incl. opening every original with ITS password. I/O failures on
  source/destination are retryable; 3 in a row pause the batch. Bundle Stop
  keeps the temp archive + journal ("Finish the archive with what is done" =
  partial). "Make them again" on Continue removes a result only if
  `pipeline.made_by_this_batch` (last op anonymize + stored name + start ==
  original + shift + exact kept channels + the asked password state + the
  identity as asked, #36: pseudonym stored, cleared patient/location/
  descriptions EMPTY; kept patient/location values <= the original's (and
  present == original's when nothing left out); kept descriptions/note
  text PRESENT where they must survive (`_has_content` MIRRORS mef3io
  1.4.0's `build_redactor`, which has no public API: a word survives if it
  contains no whole value, letter run >= 3, digit run >= 3 or first/last
  run of any identity value or left-out channel name; coarser fold. A
  mef3io redaction change must be mirrored, or the batch rejects its OWN
  copy and Continue is blocked: test_own_copy_is_recognised_whatever_the_
  names_look_like catches it. Short pieces still over-match: #39; a public
  mef3io redaction helper (mef3io#114) would replace the mirror; with text
  kept, mef3io's own verify can refuse a copy its removal left, e.g. an
  unaccented name (mef3io#115, a known limitation in the release notes). Channel descriptions per channel via
  `ChannelInfo.description`; recording/reference descriptions only when
  nothing was left out); notes == original's minus left-out channels
  at +shift; no note text when dropped; inspect facts only; remaining blind
  spots listed in docs/design/anonymizer.md). A late
  Stop credits a folder that appeared only through the same check
  (`_appeared` + `_is_this_copy`). The batch-archive name refusal names the
  matched text on screen (never logged). Journal (`model/journal.py`): opaque ids in clear, everything else
  SEALED (SHA-256 keystream + HMAC, separate enc/mac keys from
  `<app data>/journal.key`, DPAPI on Windows; created ONLY when missing,
  otherwise fails closed with `KeyUnavailable`, never replaced); NO original names/dates (read
  again with inspect on Continue); passwords never journaled; temporaries
  journaled before they exist (+ sealed `temporaries.json`); resume offered at
  start (env MEF3IO_STUDIO_NO_STARTUP_OFFERS disables, tests set it).
  Work-folder preferences stored sealed. Keep-awake (`model/awake.py`; Linux
  `systemd-inhibit … tail --pid`) — tests set MEF3IO_STUDIO_NO_KEEP_AWAKE
  (only this Python may be started). The Anonymize page builds its widgets on
  first use (`AnonymizerPage._build`): built eagerly they made Explore's
  0.25 s responsiveness test flaky.
- Signal viewer window (viewer/, spec docs/design/viewer.md; part 1 = window,
  data flow, navigation, montages, filters; recording band/banner/side panel,
  part 2 = recording band/boundaries/banner, side panel, notes and events
  (read-only), export range; edit mode is still to come. The hooks in
  `viewer/window.py` (`features`, `handlers`, `context_hooks`, `band_slot`,
  `chips_slot`, `side_panel`, `export_edge`) are now filled). Pure logic in `model/viewer_view.py`
  (scales, ViewState, profiles) and `workers/viewer_pipeline.py` (tiles ->
  montage -> `dsp.filter_block`, view blocks = 1.5 pages aligned to half
  pages); `viewer/controller.py` marshals scheduler/compute callbacks to the
  GUI thread by signals. The GUI thread never calls mef3io: `tasks.open_viewer`
  reads rates/units/ownership once on its job thread (the window uses only the
  cached facts), reads run on scheduler/compute threads, `data.close` on a
  thread. Overview beyond 300 s = envelope, as recorded, no filters. Keys:
  ONE table in `viewer/keymap.py` (also generates the help overlay). Viewer
  defaults are `viewer/*` preferences (numbers and words only).
  Part 2: Qt-free logic in `model/viewer_marks.py` (band spans, banner and
  boundary texts, the FIXED type colour table + the app's quick-type prefix
  "Spike: ...", search, stepping, snap to the sample grid, Copy times text,
  CSV/JSON rows); widgets in `viewer/recordings.py` (band, banner: fades,
  QAccessibleAnnouncementEvent), `viewer/marks.py` (chips row, Types panel),
  `viewer/side_panel.py` (Recordings / Notes and events tabs),
  `viewer/export_range.py` (the list dialog); the canvas draws marks,
  boundaries, the persistent Shift+drag selection and the export edges
  (draggable; with an edge active Left/Right nudge it one sample instead of
  paging; Esc releases). Rules: the notes are read ONCE on a thread of their
  own (`annotations_ready` signal; the GUI thread never calls mef3io); every
  name and note text follows the per-recording mask (`reveal(i)`): letter only
  / "(hidden)" while hidden, and the type key uses the text's quick label only
  when shown; the viewer's Show buttons call `show_details(i)` (launch sets
  `entry.revealed`, Explore refreshes via `ExplorerPage.refresh_entry`);
  saving the range list is REFUSED while a covered recording is hidden
  (`ask_show` then `confirm`, both replaceable in tests). UI text says "notes
  and events" (ui-language bans "annotations"). Send to Join or trim:
  `ViewerWindow.join_requested` -> `launch(on_join=)` -> `App.join_window_in_joiner`
  -> `JoinerPage.choose_window` (the joiner works in whole seconds: start
  rounded down, end up); the joiner's "View these recordings" emits
  `view_requested` -> `App.view_signals`.
- Viewer edit mode (phase 2, additions only): Qt-free `model/annotation_draft.py`
  (Draft with undo/redo; items are new or the changed version of a stored
  note (`replaces`); `route`/`make_plan` -> `SavePlan`: channel note -> the
  recording holding that channel then, whole-recording -> the recording
  holding data then, side by side -> `choice_groups` the user decides in the
  preview, outside -> refused with a reason; `stored_changes` = `needs_copy`,
  NEVER written; import `read_rows`/`preview_import`, export `export_rows`
  with ISO + offset and uUTC, `vm.safe_cell` formula escaping; point -> Note,
  with end -> EDFA, quick type = text prefix "Spike: ..."), `model/
  annotation_save.py` (the other mef3io user: appends with
  `Writer.write_annotations` per level, trying password pairs ("", pwd) then
  (pwd, pwd) because the library wants them that way for a new / an existing
  notes file; stops at the first failure and reports which recordings were
  done, so the draft drops exactly those; plain messages incl. "rescue first"
  for a torn file). `viewer/edit_mode.py` (`window.edit`: switch, banner,
  selection, add/change/take out, import/export, save as ONE JobManager task:
  write, `scheduler.drain`, `data.reopen()`, re-read notes; `window.jobs` from
  `launch`, else its own manager) and `viewer/edit_dialogs.py` (editor, save
  preview, import preview, Save/Discard/Cancel with Qt's standard buttons).
  Rules: off on every open; keys (a, 1-9, Ctrl+Z, Ctrl+Shift+Z) work only with
  the "edit" feature on; digits 2-9 add a quick type at once, "Note" opens the
  editor; quick types = `viewer/quick_types` (one per line); the window
  closes only after Save/Discard (a save in progress blocks it); while a save
  runs `_refresh` is off and `controller.abandon_all()` ran; `ViewerData.
  reopen()` primes the rate/units/ownership caches (the GUI thread must not
  call mef3io after it); a stored note of a hidden recording cannot be
  edited (it would show its text); export waits for details to be shown.
  Tests: tests/test_annotation_draft.py, tests/test_viewer_edit.py.
- Join or trim (#31): `model/join.py` (Qt-free) + `joiner/page.py` (a wizard
  like the anonymizer's, REUSING its `RecordingsStep` (subclass: texts +
  `_reason`), `PasswordsStep(what=, explanation=, none_label=)`,
  `channel_selection_for` and `ChannelPicker`; built on first use). Steps:
  Recordings, CHANNELS, What to keep, New recording, Save (channels BEFORE
  keep: the overlap rule needs the left-out channels). Keep = whole |
  first(d) | last(d) | between(t0, t1) -> `resolve_keep` -> `Span`
  `[start, end)`; a window reaching the recording's end goes to mef3io as
  `end + 1` (`Span.to_end`). END CONVENTION: a recording's end is where its
  last sample ENDS (exclusive). `time_range().end_uutc` is the end the
  headers STATE (exclusive for mef3io's files; another writer may store the
  last sample's own time), so `read_layouts` takes the end from each
  channel's last block (`Reader.block_envelope(ch, t0=stated_end - 1)`:
  only the last part's index, `end_uutc` = start + samples/fs); the page
  uses Explore's details (header ends) for the preview only. Start and
  "times confirmed" (`offset_known`) still come from `time_range`; channels
  from the Reader. `plan_join(sources, layouts, excluded=, changes=)`
  (pure) orders by time; OVERLAP IS PER CHANNEL: two kept parts conflict
  only when they overlap in time AND share a channel not left out
  (`Problem.channels`, named in the words; channels unknown = any time
  overlap conflicts); side by side (same period, different channels) joins
  into the union of channels, as merge_sessions does. Time windows are the
  recording's, so it is stricter than merge (which decides per recorded
  block and allows take-turns recordings: not a use case); merge's own
  `merge-overlap` stays the last word. A recording with every channel left
  out = `NO_CHANNELS_LEFT`. One recording kept whole with no channel left
  out and no new passwords = `NOTHING_TO_CHANGE` (refused; with a change it
  is a plain transform copy). Run (`make_joined`, ONE helper process for
  the whole run, one progress bar via `JoinProgress.overall`): one
  recording -> `transform_session(src, dst, time_range, exact=True,
  exclude, output_password)` straight to dst; several -> each non-whole part
  cut with `transform_session` into `<dst folder>/.mef3io-studio-parts-
  <hex>/part-N.mefd` (whole ones go to merge as they are), then
  `merge_sessions(parts in time order, dst, passwords, exclude_channels,
  output_password)`: channel choice and new passwords apply ONCE, in the
  merge. TEMP PARTS ARE ALWAYS PASSWORD-PROTECTED with throw-away passwords
  made in the helper (never returned, logged or stored): an unprotected cut
  copy would leave readable patient details and note text on disk beside
  dst for the run (or after a crash). PARTS FOLDER OWNERSHIP:
  `make_parts_folder` = `os.mkdir` (never an existing folder) then AT ONCE
  a marker file `PARTS_MARKER` holding the request's random `run_token`
  (marker write fails -> rmdir the still-empty folder, re-raise);
  `remove_parts` removes ONLY a `.mef3io-studio-parts-` folder, not a link,
  whose marker holds THIS run's token: a folder that was already there (a
  FileExistsError from mkdir) or another run's is never touched. It runs in
  the helper's `finally` AND in the task (`join.tidy`, also `<dst>.partial`)
  after an ended helper; `parts_left` is reported. Up-front (`check_join`,
  nothing written): `read_layouts`, `plan_for`, then one recording:
  `transform.check` as it will run; several: `transform.check` per cut
  (placeholder dst, path codes ignored) + `merge.check` over the ORIGINALS
  with the real dst/channels/passwords, ignoring `merge-overlap` when
  anything is cut. STALE CHECK: while the check runs the page disables its
  steps (`stack`) and Back; every input change, `go()` and `hideEvent`
  bump `_generation`, and the check's callback starts the run only if the
  generation is unchanged, the page still on New recording and visible.
  Words: `refusals.JOIN_WORDS` covers EVERY code of
  `mef3io.transform.REFUSAL_CODES` and `mef3io.merge.REFUSAL_CODES` (test;
  no "anonymi", "were not changed" in each); the anonymizer's `WORDS` stay
  anonymizer-only. Details via `join_technical_text` (every source/dst path
  and name stripped), then `scrubbed`. Keys are paths: `JoinSource.key`,
  `Problem.keys`, `CheckFinding.key`, `JoinPlan.order`, `Layout` are
  `repr=False` (test). Recordings needing a password use the one Explore
  opened them with (`RecordingEntry.password`); basic-only access and
  unverified times are not offered. Explore -> joiner: "Join or trim
  these…" (button under the list + context menu) emits
  `ExplorerPage.join_requested(keys)` -> `MainWindow.join_in_joiner` ->
  `JoinerPage.choose(keys)` (restart, tick exactly those, on to Channels;
  unusable ones named on step 1; refused while a check/run is going).
  "Open it in Explore" = `MainWindow.open_in_explorer` ->
  `ExplorerPage.open_and_show`. Tests: tests/test_join_model.py (exact
  samples via Reader, three-way, single trim, side by side incl. shared
  channel and leaving it out, overlap, Stop, cleanup + pre-existing folder,
  ends from blocks, reprs, password-protected parts and sources),
  tests/test_joiner_ui.py (incl. stale check and the Explore action).
