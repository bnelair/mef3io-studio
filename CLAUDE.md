# mef3io-studio — assistant context

Desktop app (PySide6) for exploring, organizing, protecting and anonymizing
MEF 3.0 recordings, built
on the mef3io library (github.com/bnelair/mef3io). Status: planning. Design in
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
  through a verified transformed copy.
- Crash safety never depends on optional artifacts: an opaque in-session
  "operation in progress" marker (no names/paths) + a MANDATORY journal in
  backup_dir, deleted on completion. Byte backups are crash rollback only.
  Keeping undo info afterwards is the only optional part.
- Recovery artifacts live in the per-user app-data dir under opaque names;
  paths and session names count as identifying (log filter, no recent-files
  list by default, per-folder prefs keyed by a salted path hash).
- Local time comes from a user-chosen site time zone (per app profile, per-
  folder overrides) via zoneinfo; MEF's GMT_offset is one fixed number, sits
  in L2-encrypted section 3, and meflib ignores the DST fields.
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
