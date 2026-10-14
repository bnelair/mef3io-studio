# mef3io-studio — assistant context

Desktop app (PySide6) for exploring and anonymizing MEF 3.0 recordings, built
on the mef3io library (github.com/bnelair/mef3io). Status: early development. Design in
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
- Anything identifying the app learns → `common.privacy.register_identity()`.
  Never log paths or recording contents.
- New preferences must be declared in `common/settings.PREFERENCES` (UI only).
- Look: Fusion + palette + QSS in `common/theme.py`; icons are bundled SVGs
  with `currentColor`; no pixel font sizes.
