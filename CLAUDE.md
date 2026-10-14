# mef3io-studio — assistant context

Desktop app (PySide6) for exploring and anonymizing MEF 3.0 recordings, built
on the mef3io library (github.com/bnelair/mef3io). Status: planning. Design in
`docs/`, work in issues, library-side plan in mef3io#71.

Hard rules:
- The app NEVER reads or writes MEF bytes itself; only mef3io's public API
  (no `mef3io._*` imports). Missing capability → mef3io issue first.
- Originals are never modified; written copies are verified before being kept.
- Never load a whole recording (sessions reach TBs) — use inspect/windowed reads.
- Privacy: no patient details or original dates in logs, reports, fixtures,
  screenshots; no network access in v0.1.
- Users include students and non-technical staff: plain language first,
  illustrations, technical detail behind a toggle.
- v0.1 scope = metadata explorer + anonymizer. Viewer, annotations, export,
  mef3io-server access are NEXT DIRECTIONS needing their own specs.
