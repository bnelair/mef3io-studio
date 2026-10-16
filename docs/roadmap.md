# Roadmap

Tracked on GitHub: [tracking issue](https://github.com/bnelair/mef3io-studio/issues/1)
here, and [mef3io#71](https://github.com/bnelair/mef3io/issues/71) for the
library side. The library work always lands in mef3io **before** the app uses it.

## v0.1: explore, organize, protect and anonymize

Shipped **only as installers** (Windows `.exe`, macOS, Linux) on GitHub Releases. Not on PyPI.

| Step | Where | What | Status |
|---|---|---|---|
| 0 | mef3io | Record handling fixed: [#60](https://github.com/bnelair/mef3io/issues/60), [#61](https://github.com/bnelair/mef3io/issues/61), [#62](https://github.com/bnelair/mef3io/issues/62), [#63](https://github.com/bnelair/mef3io/issues/63) | Done |
| 1 | mef3io | `inspect()` [#67](https://github.com/bnelair/mef3io/issues/67), cancellation and progress [#69](https://github.com/bnelair/mef3io/issues/69), `anonymize_session` + `verify` [#70](https://github.com/bnelair/mef3io/issues/70), `time_range` + `rename_session` + `fix_name_drift` [#72](https://github.com/bnelair/mef3io/issues/72), `transform_session` + `edit_metadata` [#73](https://github.com/bnelair/mef3io/issues/73), released as **mef3io 1.3** | In progress (work on #67 and #69 is under way in mef3io, not yet released); the app uses temporary stand-ins for both until mef3io 1.3 |
| 2 | studio | App shell, background jobs, privacy-safe logging, settings | Done |
| 3 | studio | **Installers** for Windows, macOS and Linux, built in CI. Done early, so packaging and signing problems surface before the features exist | Done; code signing still open |
| 4 | studio | **Explorer** (metadata), plus **ordering and safe renaming** of sessions ([#12](https://github.com/bnelair/mef3io-studio/issues/12); needs [mef3io#72](https://github.com/bnelair/mef3io/issues/72)) | Explore page done; ordering and renaming planned |
| 5 | studio | **Metadata view and edit** ([#14](https://github.com/bnelair/mef3io-studio/issues/14)); timestamps never editable | Planned |
| 6 | studio | **Passwords** ([#15](https://github.com/bnelair/mef3io-studio/issues/15)) and **channel presets + batch conversion** ([#16](https://github.com/bnelair/mef3io-studio/issues/16)) | Planned |
| 7 | studio | **Anonymizer** wizard ([#5](https://github.com/bnelair/mef3io-studio/issues/5)) | Wizard and Privacy check built; waits for the mef3io release with [mef3io#104](https://github.com/bnelair/mef3io/pull/104) (test version 0.1.0.dev2) |
| 8 | studio | User guide with screenshots ([#6](https://github.com/bnelair/mef3io-studio/issues/6)); v0.1 release | Started (user guide for the Explore page) |

## Next directions (each needs a specification first)

- **Signal viewer:** traces, zoom and pan, gaps; built on mef3io-server's cache, decode and prefetch (driven without gRPC locally; needs a password parameter and a plain-Python API there first), fast on multi-TB recordings via mef3io [#68](https://github.com/bnelair/mef3io/issues/68).
- **Data coverage view:** when data exists and at what rate, per channel, from the block index (mef3io [#68](https://github.com/bnelair/mef3io/issues/68), [mef3io-server#13](https://github.com/bnelair/mef3io-server/issues/13)).
- **Annotations:** view, then create and edit.
- **Export** to other formats.
- **Remote recordings** via [mef3io-server](https://github.com/bnelair/mef3io-server): the viewer's code path with a remote source.
- **Validate / repair / recover** front end for mef3io's existing tools, with dry run by default.

See [design/next-directions.md](design/next-directions.md) for what is known so far.
