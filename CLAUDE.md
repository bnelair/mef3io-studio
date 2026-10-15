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
  list by default, no paths in preferences).
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
- Data access goes through `model.source.RecordingSource` (server source later).
  `LocalRecordingSource.isolate = True` runs opens in a spawn helper process
  until mef3io#79 releases the GIL (xfail test pins it); `freeze_support()` is
  the first thing the launcher does.
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
  for display (thin adapter over the public Reader; swap for mef3io.inspect()
  #67). It never calls read/read_raw/toc; tests/test_explorer_bytes.py pins
  < 1% of bytes read via /proc/self/io in the helper. Widgets (`explorer/`,
  `common/`) never import mef3io (test). Pack/unpack (`model/archive.py`)
  writes to an opaque `.mef3io-studio-<hex>` temp name, checks the copy, then
  finishes without replacing anything. Patient details, descriptions and
  note text are masked until "Show" per recording; Copy summary also hides
  names and dates. RTO != 0 = deprecated date masking: warn, never call it
  anonymized.
- Anonymizer (#5): `model/shiftplan.py` is the time-shift PLANNER (step 2),
  pure data in/out: `plan_shifts([RecordingTimes], ShiftSettings) ->
  ShiftPlan` with per-recording `shift_us`, warnings/blockers/refusals as
  enum CODES (words belong to the UI). It never imports mef3io or logging;
  original times and shifts are `repr=False` (the shift re-identifies).
  Recordings with unverified times (rto != 0 or unknown) or no full-access
  password are BLOCKED, not planned. Running the copy is mef3io#70's job.
