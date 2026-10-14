# mef3io-studio — assistant context

Desktop app (PySide6) for exploring and anonymizing MEF 3.0 recordings, built
on the mef3io library (github.com/bnelair/mef3io). Status: planning. Design in
`docs/`, work in issues, library-side plan in mef3io#71.

Hard rules:
- The app NEVER reads or writes MEF bytes itself; only mef3io's public API
  (no `mef3io._*` imports). Missing capability → mef3io issue first.
- Originals change only when the user explicitly says so. Content changes
  (anonymization, encryption, channel subsets) ALWAYS write a verified copy;
  renames (via mef3io's rename_session: folder + session .rdat/.ridx + stored
  names, journaled) and metadata edits are explicit in-place operations with
  preview, confirmation and undo. TIMESTAMPS ARE NEVER EDITED IN PLACE —
  times change only through a verified transformed copy.
- Recovery artifacts (rename journals, header/metadata backups) live in the
  per-user app-data dir under opaque names; paths and session names count as
  identifying (log filter, no recent-files list by default).
- Local time comes from a user-chosen site time zone via zoneinfo; MEF's
  GMT_offset is one fixed number and meflib ignores the DST fields.
- Never load a whole recording (sessions reach TBs) — use inspect/windowed reads.
- Privacy: no patient details or original dates in logs, reports, fixtures,
  screenshots; no network access in v0.1.
- Users include students and non-technical staff: plain language first,
  illustrations, technical detail behind a toggle.
- v0.1 scope = explorer (incl. order/rename), metadata view/edit, passwords,
  channel presets + batch conversion, anonymizer; installers only (no PyPI).
  Viewer (on mef3io-server), coverage view, annotations, export, remote
  access are NEXT DIRECTIONS needing their own specs.
