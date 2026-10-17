# mef3io-studio — assistant context

Desktop app (PySide6) for exploring, organizing, protecting and anonymizing
MEF 3.0 recordings, built
on the mef3io library (github.com/bnelair/mef3io). Status: early development. Design in
`docs/`, work in issues, library-side plan in mef3io#71.

Hard rules:
- The app NEVER reads or writes MEF bytes itself; only mef3io's public API
  (no `mef3io._*` imports). Missing capability → mef3io issue first.
- Originals change only when the user explicitly says so. Content changes
  (anonymization, encryption, channel subsets) ALWAYS write a verified copy;
  renames (via mef3io's rename_session: folder + session .rdat/.ridx + stored
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
  Viewer (on mef3io-server), coverage view, annotations, export, remote
  access are NEXT DIRECTIONS needing their own specs.

Code (app shell, #2/#3):
- Dev: `python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`
  (Windows: `.venv\Scripts\activate`); `python -m pytest` (offscreen);
  `ruff check . && ruff format --check .`. CI installs from the hashed
  `requirements/*.lock` (regenerate with uv, see requirements/README.md). Installers:
  `packaging/mef3io-studio.spec` + `.github/workflows/build.yml`; every build
  must pass `mef3io-studio --self-test`. Not on PyPI, ever. Version = `VERSION`.
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
  `LocalRecordingSource.isolate = True` runs opens in a spawn helper process:
  `mef3io.Reader` still holds the GIL for a whole open (mef3io#79), while
  `mef3io.inspect()` lets other threads run (measured: ~60 ms longest stall
  vs ~1.7 s for the Reader; test_jobs pins the inspect-based summary on a
  thread; it skips, saying so, when the call takes <= 1.5x the stall bound,
  too short to show a held GIL). The helper also contains native crashes, so it stays for now;
  `freeze_support()` is the first thing the launcher does.
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
  with `currentColor`; no pixel font sizes.
- Explorer (#4): `model/inspect.py` is the ONLY place that reads a recording
  for display: a thin adapter over `mef3io.inspect(strict=False,
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
  without channel names until mef3io's ChannelSummary carries them (#96). Never read/read_raw/toc;
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
  original + shift). Stop that lands after mef3io published a folder copy
  reports it saved (`_published`). Journal (`model/journal.py`): opaque ids in clear, everything else
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
