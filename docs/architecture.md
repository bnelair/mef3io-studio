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

## Library APIs the first release depends on

All planned in mef3io and tracked in [mef3io#71](https://github.com/bnelair/mef3io/issues/71):

| API | Used by | mef3io issue |
|---|---|---|
| `mef3io.inspect(path)`: everything about a session without reading samples | Explorer, anonymizer step 1 | [#67](https://github.com/bnelair/mef3io/issues/67) |
| `anonymize_session(src, dst, shift_us, ...)` and `verify(...)` | Anonymizer | [#70](https://github.com/bnelair/mef3io/issues/70) |
| Cancellation tokens and progress callbacks | Every long operation | [#69](https://github.com/bnelair/mef3io/issues/69) |
| Complete record parsing at every level | Explorer annotations; anonymizer time shift | [#60](https://github.com/bnelair/mef3io/issues/60), [#61](https://github.com/bnelair/mef3io/issues/61), [#62](https://github.com/bnelair/mef3io/issues/62) |
| Provenance region (versions and operations, no times) | Explorer | [#58](https://github.com/bnelair/mef3io/pull/58) |

The future viewer will add [#68](https://github.com/bnelair/mef3io/issues/68)
(an index-only overview and a min/max envelope read).

## Technology

- **Python 3.11+ with PySide6 (Qt 6).** mef3io already ships wheels for every platform the app targets, so the whole stack is one language with no bridge.
- **Background work** runs on a worker thread pool (`QThreadPool`). Opening, inspecting and anonymizing never block the UI, and every long operation shows progress and can be cancelled.
- **Packaging:** PyInstaller (or Briefcase), built in a CI matrix for Windows x64, macOS arm64 + x86_64 and Linux x64. Code signing and notarization are needed on Windows and macOS, or clinical IT will block the app. Certificates: to be decided.
- **Settings and preferences:** stored per user (`QSettings`). Never anything about a recording's contents.
- **Logging:** a rotating local log file. A logging filter rejects records containing known identity fields or absolute timestamps, and a test enforces it.

## Packages (planned layout)

```
src/mef3io_studio/
  app.py            # entry point, main window
  model/            # session summaries and batch plans: plain data, no Qt
  workers/          # background jobs wrapping mef3io calls (progress, cancel)
  explorer/         # metadata explorer views
  anonymizer/       # the anonymization wizard
  common/           # widgets, privacy filter, settings
tests/              # pytest + pytest-qt; headless in CI
```

The **`model/`** package has no Qt dependency, so the batch-planning logic
(relative and aligned modes, the overlap checks) is unit-tested without a GUI.

## Testing

- **Unit tests** for `model/`: shift calculation in every mode, DST and time-zone cases, overlap detection.
- **UI tests** (`pytest-qt`, offscreen): each wizard step.
- **End to end:** generate sessions with mef3io, anonymize them through the app's job layer, then verify with `mef3io.anonymize.verify`, on all three platforms in CI.
- **Privacy test:** run every workflow with known identity strings and dates, then assert none of them appear in the log file.
