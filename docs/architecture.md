# Architecture

![mef3io-studio calls the mef3io library; nothing in the app reads or writes MEF bytes directly.](img/architecture.svg)

## The split

| Layer | Owns | Does not own |
|---|---|---|
| **mef3io** (library) | Every byte on disk: reading, writing, validation, repair, recovery, tar archives, **anonymization**, `inspect()`, and the time conversions | Any workflow, UI or user preference |
| **mef3io-studio** (this repo) | Screens, workflow, batch planning (which shift each recording gets), settings, installers | Any knowledge of the MEF byte layout |

The rule that keeps this honest: **the app does not import any mef3io module
starting with `_`, and never opens a `.tmet` / `.tidx` / `.tdat` file
itself.** If the app needs something mef3io can't provide, the feature goes
into mef3io first. It is then tested against meflib/pymef, and is available
to scripts and the CLI too.

## mef3io-server: for remote recordings, later

**As built (test version 0.1.0.dev6):** the signal viewer does not use
mef3io-server. It reads local recordings through mef3io directly, with its own
tile cache and look-ahead (`viewer/` and `model/viewer_*.py`), because mef3io
2.0 provides the parallel decode, cancellation and index-only overview the
server was going to supply. The rest of this section is the original plan,
kept for **remote recordings**, which stay a later "recording source".

[mef3io-server](https://github.com/bnelair/mef3io-server) already exists
(PyPI `mef3io-server`, Docker image on GHCR) and is built directly on mef3io.
Its `FileManager` provides:
- metadata loading (`FileInfo`);
- a **byte-budgeted per-channel tile cache** with idle expiry;
- **parallel decode across worker processes;**
- **look-ahead/behind prefetch** sized in windows of the request.

Those are exactly the pieces an interactive viewer needs, so the studio
**reuses them instead of building its own cache**:
- **Local recordings:** the app drives the `FileManager` directly, **without gRPC** and without the network. It still starts its own decode worker processes, which is how the server parallelises.
- **Remote recordings:** the `Mef3Client` talks to a server over gRPC.
- **One interface:** both sit behind one "recording source" interface in the app, so the viewer code is the same either way.

**What has to change in mef3io-server first.** These are viewer-spec items,
filed against mef3io-server:
- **Encrypted sessions** ([mef3io-server#14](https://github.com/bnelair/mef3io-server/issues/14)): the server opens files with `MefReader(path)` and has no password parameter (file_manager.py:178, reader_pool.py:33; nothing in the proto), so it can't serve them today.
- **A plain-Python API** ([mef3io-server#15](https://github.com/bnelair/mef3io-server/issues/15)): `FileManager.open_file` / `get_file_info` return protobuf objects, while `read_signal_range` returns a dict. The app needs one consistent plain-Python interface (or an adapter in the app).
- **Logging hooks** ([mef3io-server#15](https://github.com/bnelair/mef3io-server/issues/15)): the server logs file paths and exception text (file_manager.py:165,188,190). Driven from the app, those would reach the app's log, and paths are identifying, so the app needs to control or filter them.
- **Packaging** ([mef3io-server#15](https://github.com/bnelair/mef3io-server/issues/15)): decode workers use the multiprocessing `spawn` start method (reader_pool.py:101-108). Under PyInstaller that needs `multiprocessing.freeze_support()`, and the worker count needs a memory-aware default for laptops.
  - **A fallback that exists today:** `FileManager(use_process_pool=False)` (file_manager.py:43) decodes on threads in the app's own process instead. It avoids spawning workers in the packaged app altogether, at the cost of parallel decode, and is an option if the frozen worker start-up proves fragile.
- **Reader eviction** ([mef3io-server#15](https://github.com/bnelair/mef3io-server/issues/15)): each decode worker caches one reader per path (reader_pool.py:24-34). Before a rename or any other in-place operation, the app must be able to tell the server to drop its cached readers for a path, in every worker.

v0.1 (explore, including ordering, renaming and pack/unpack; metadata view and
edit; passwords; channel presets and batch conversion; anonymize; the viewer)
goes straight to mef3io and doesn't need the server. Remote recordings would
be the first thing that does.

## Library APIs the first release depends on

Tracked in [mef3io#71](https://github.com/bnelair/mef3io/issues/71). The app
pins one exact mef3io version (today **2.1.1**, in `pyproject.toml` and the
hashed lock files).

| API | Used by | mef3io issue | Status |
|---|---|---|---|
| `mef3io.inspect(path)`: everything about a session without reading samples | Explorer, Privacy check, anonymizer step 1, archive and batch checks | [#67](https://github.com/bnelair/mef3io/issues/67) | In use (mef3io 1.3) |
| Cancellation tokens (`mef3io.CancelToken`, `mef3io.Cancelled`) and byte progress | Opening, packing, anonymizing | [#69](https://github.com/bnelair/mef3io/issues/69) | In use (mef3io 1.3) |
| `anonymize_session(src, dst, shift_us, ...)` and `anonymize.verify(...)` | Anonymizer | [#70](https://github.com/bnelair/mef3io/issues/70) | In use (mef3io 1.4) |
| `anonymize.check(...)` (every refusal, nothing written), refusal `.code`s and `REFUSAL_CODES`, the report's original times; `password_interop_issue` | Anonymizer (name step, batch up-front check, plain-language refusals, password warning) | [#111](https://github.com/bnelair/mef3io/issues/111) | In use (mef3io 1.4) |
| `archive_session` / `extract_session` | Pack / unpack, per-recording archives in a batch | (existing) | In use |
| `Validator` | Check for problems | (existing) | In use |
| `transform_session(..., time_range=, exact=True)` and `transform.check(...)`: a cut copy (one recording trimmed, or each part to join), with channel subsets and new passwords; refusal codes `transform.REFUSAL_CODES` | Join or trim (`model/join.py`) | [#73](https://github.com/bnelair/mef3io/issues/73), [#103](https://github.com/bnelair/mef3io/issues/103) | In use (mef3io 1.5) |
| `merge_sessions(...)` and `merge.check(...)`: several recordings into one, checked against every source; `merge.REFUSAL_CODES` | Join or trim (`model/join.py`) | [#103](https://github.com/bnelair/mef3io/issues/103) | In use (mef3io 1.5) |
| `time_range(...)`: first and last time from a few small files | Join or trim (turning "the first hour" into times, time order) | [#72](https://github.com/bnelair/mef3io/issues/72) | In use (mef3io 1.5) |
| `transform_session(...)` for password changes, channel presets and batch; `edit_metadata` in place (never times) | Passwords, channel presets and batch, metadata edit | [#73](https://github.com/bnelair/mef3io/issues/73) | Planned (the in-place `edit_metadata` is not built) |
| `rename_session(...)` (folder, session-level annotation files and stored names, as one journaled operation) and `fix_name_drift(...)` | Ordering, renaming, drift fix, undo | [#72](https://github.com/bnelair/mef3io/issues/72) | Planned (released in mef3io 1.5, not used yet) |
| Complete record parsing at every level | Explorer notes and events; anonymizer time shift | [#60](https://github.com/bnelair/mef3io/issues/60), [#61](https://github.com/bnelair/mef3io/issues/61), [#62](https://github.com/bnelair/mef3io/issues/62) | Done |
| Provenance region (versions and operations, no times) | Explorer History, Privacy check ("made by the anonymizer") | [#58](https://github.com/bnelair/mef3io/pull/58) | Done |

The viewer uses [#68](https://github.com/bnelair/mef3io/issues/68)
(an index-only overview and a min/max envelope read) and mef3io's windowed
reads, `MultiReader` for several recordings as one, and the append of notes
and events to a recording's records (edit mode; only additions).

## Technology

- **Python 3.11+ with PySide6 (Qt 6).** mef3io already ships wheels for every platform the app targets, so the whole stack is one language with no bridge.
- **Background work** runs on a worker thread pool (`QThreadPool`, `workers.JobManager`). Opening, inspecting and anonymizing never block the UI, and every long operation shows progress and can be stopped (mef3io 1.3's cancellation tokens and byte progress). Opening a recording and making each anonymized copy run in a short-lived helper process: it contains a native crash in the library and gives a hard Stop. (mef3io 1.4 releases Python's GIL during an open and in `inspect()`, [mef3io#79](https://github.com/bnelair/mef3io/issues/79), so the helper is no longer needed to keep the window live.) Pure-Python layout work that would stall the window through the GIL (the combined timeline) runs in short slices on the GUI thread instead of a thread.
- **Packaging:** PyInstaller, built in a CI matrix for Windows x64 (Inno Setup installer and a portable zip), macOS arm64 + x86_64 (`.dmg`) and Linux x64 (AppImage and tarball, built in AlmaLinux 9). Code signing and notarization are needed on Windows and macOS, or clinical IT will block the app; nothing is signed yet (certificates to be decided, [#3](https://github.com/bnelair/mef3io-studio/issues/3)).
- **Settings and preferences:** stored per user (`QSettings`), declared in `common/settings.PREFERENCES`. Never anything about a recording's contents, and **no paths in clear**: there is **one site time zone per app profile**, plus a per-window override that is never saved; the anonymizer's work folder is stored sealed.
- **Unattended batches** (anonymizer): a crash journal per batch in the app-data folder, holding only opaque ids in clear and everything else sealed with a per-user key (`journal.key`, DPAPI on Windows); no original names or dates, no passwords. Details in [design/anonymizer.md](design/anonymizer.md#the-unattended-batch-pipeline).
- **Recovery artifacts** (for the planned in-place operations: rename, metadata edit) live in the per-user app-data folder, under opaque names; mef3io is told where to write them (`backup_dir`). The crash journal is mandatory and deleted when the operation completes (after the in-session marker); byte-level backups serve crash rollback only and are always deleted on completion; keeping undo information (old and new values, touched files' post-operation times and sizes, segment end times) afterwards is optional. Nothing of this is ever stored beside the recordings. Undo is a logical inverse (`rename_session` back, `edit_metadata` with the old values), never a byte restore. The only thing written inside a session is mef3io's non-identifying "operation in progress" marker, while an operation runs.
- **Logging:** a rotating local log file. Recording names and folder paths count as identifying, alongside identity fields and absolute timestamps. A logging filter redacts all of them (registered identity strings in any case, with or without accents or separators; paths in any script; dates and times in any common form), including messages from mef3io and mef3io-server, uncaught exceptions, Qt's messages and crash reports, and a test feeds it ID-bearing and date-bearing folder names.
- **Recent files:** none, because paths are identifying (if a list is ever added, it is off by default).
- **Crashes:** a crash handler writes a local, path-redacted report and sends nothing anywhere.
- **On screen:** a "mask session names" option, for screen sharing and screenshots (planned; patient details, descriptions and note text are already hidden until "Show").
- **Time zones:** local times come from the operating system's time-zone database (`zoneinfo`) and a site time zone the user chooses, never from the file's fixed `GMT_offset`. Until a zone is chosen, times are shown in UTC, labelled as such. That offset ignores daylight saving (meflib.c:4649-4650), and it sits in level-2 encrypted section 3, so it is unavailable without the level-2 password.

## Packages

```
src/mef3io_studio/
  app.py            # entry point, main window, menus, start-up offers, --self-test
  model/            # plain data and logic, no Qt: inspect adapter, coverage,
                    # CSV export, time-shift planner, anonymize, batch pipeline,
                    # archive, bundle, journal, privacy check, errors, progress
  workers/          # background tasks (QThreadPool), helper processes, GUI slices
  explorer/         # the Explore page: list, details, timelines, privacy check
  joiner/           # the Join or trim page
  viewer/           # the signal viewer window: traces, montages, filters,
                    # notes, export range, edit mode
  anonymizer/       # the anonymization wizard, channel picker, resume dialog
  common/           # widgets, privacy log filter, settings, theme, activity panel
tests/              # pytest + pytest-qt; headless in CI
```

The **`model/`** package has no Qt dependency (a test enforces it), so the
batch-planning logic (relative and aligned modes, the leak-scan floor) is
unit-tested without a GUI. The widgets in `explorer/`, `anonymizer/` and
`common/` never import mef3io (also tested).

## Testing

- **Unit tests** for `model/`: shift calculation in every mode, DST and time-zone cases, overlap detection.
- **UI tests** (`pytest-qt`, offscreen): each wizard step.
- **End to end:** generate sessions with mef3io, anonymize them through the app's job layer (single copies and whole batches into folders, archives and one batch archive, with Stop, retry and resume), and check the copies with mef3io, on all three platforms in CI.
- **Byte budget:** opening a large generated recording for the Explorer reads under 1% of its bytes (`/proc/self/io`, in the helper).
- **Privacy test:** run every workflow with known identity strings and dates, then assert none of them appear in the log file.
