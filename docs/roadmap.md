# Roadmap

Tracked on GitHub: [tracking issue](https://github.com/bnelair/mef3io-studio/issues/1)
here, and [mef3io#71](https://github.com/bnelair/mef3io/issues/71) for the
library side. The library work always lands in mef3io **before** the app uses it.

## v0.1: explore, organize, protect and anonymize

Shipped **only as installers** (Windows `.exe`, macOS, Linux) on GitHub Releases. Not on PyPI.

| Step | Where | What | Status |
|---|---|---|---|
| 0 | mef3io | Record handling fixed: [#60](https://github.com/bnelair/mef3io/issues/60), [#61](https://github.com/bnelair/mef3io/issues/61), [#62](https://github.com/bnelair/mef3io/issues/62), [#63](https://github.com/bnelair/mef3io/issues/63) | Done |
| 1 | mef3io | `inspect()` [#67](https://github.com/bnelair/mef3io/issues/67) and cancellation and progress [#69](https://github.com/bnelair/mef3io/issues/69) (released in **mef3io 1.3**); `anonymize_session` + `verify` [#70](https://github.com/bnelair/mef3io/issues/70) and `anonymize.check()`, refusal codes and original times [#111](https://github.com/bnelair/mef3io/issues/111) (released in **mef3io 1.4**); `time_range` + `rename_session` + `fix_name_drift` [#72](https://github.com/bnelair/mef3io/issues/72), `transform_session` + `edit_metadata` [#73](https://github.com/bnelair/mef3io/issues/73) | #67, #69, #70, #111 done; #72 (`time_range`, `rename_session`, `fix_name_drift`) and #73 (`transform_session`), plus cutting and merging recordings (#103), released in **mef3io 1.5** (the app now runs on mef3io 2.0.0; the in-place `edit_metadata` of #73 is not built) |
| 2 | studio | App shell, background jobs, privacy-safe logging, settings | Done |
| 3 | studio | **Installers** for Windows, macOS and Linux, built in CI. Done early, so packaging and signing problems surface before the features exist | Done; code signing still open |
| 4 | studio | **Explorer** (metadata), plus **ordering and safe renaming** of sessions ([#12](https://github.com/bnelair/mef3io-studio/issues/12); needs [mef3io#72](https://github.com/bnelair/mef3io/issues/72)) | Explore page done ([#4](https://github.com/bnelair/mef3io-studio/issues/4)), with the combined timeline, CSV export of times, real progress and Stop (on mef3io 1.3) and the Privacy check; ordering and renaming planned |
| 5 | studio | **Metadata view and edit** ([#14](https://github.com/bnelair/mef3io-studio/issues/14)); timestamps never editable | Planned |
| 6 | studio | **Passwords** ([#15](https://github.com/bnelair/mef3io-studio/issues/15)) and **channel presets + batch conversion** ([#16](https://github.com/bnelair/mef3io-studio/issues/16)) | Planned |
| 7 | studio | **Anonymizer** wizard ([#5](https://github.com/bnelair/mef3io-studio/issues/5)) | Wizard, unattended batch (folders, per-recording archives, one batch archive; resume after a crash) and Privacy check built, on mef3io 1.4.0 (test versions 0.1.0.dev2 and dev3) and 1.5.1 (0.1.0.dev4); the deferred items are listed in [design/anonymizer.md](design/anonymizer.md#as-built-wizard-for-010dev2) |
| 7b | studio | **Join or trim** ([#31](https://github.com/bnelair/mef3io-studio/issues/31)): keep part of a recording, or join parts of several into one new recording, on mef3io 1.5.1 (`time_range`, `transform_session` with a time window, `merge_sessions`) | Built, in test version 0.1.0.dev5 (also side by side, and from the Explore selection); cutting one recording into several pieces is still to come |
| 8 | studio | User guide with screenshots ([#6](https://github.com/bnelair/mef3io-studio/issues/6)); v0.1 release | Started (user guide for Explore, Anonymize and Join or trim) |

## Test versions

Published as GitHub pre-releases; what changed in each is in
[release-notes/](release-notes/).

| Version | mef3io | What testers get |
|---|---|---|
| 0.1.0.dev1 | 1.2.0 | The app shell, the installers and the Explore page |
| 0.1.0.dev2 | 1.4.0 | Explore with the combined timeline, CSV export of times and a working Stop; the Anonymize wizard with unattended batches; the Privacy check ([notes](release-notes/0.1.0.dev2.md)) |
| 0.1.0.dev3 | 1.4.0 | Clearly outlined option buttons, tick boxes and fields in both themes ([#35](https://github.com/bnelair/mef3io-studio/issues/35)); a stricter check before continuing a batch removes an earlier result ([#36](https://github.com/bnelair/mef3io-studio/issues/36)) ([notes](release-notes/0.1.0.dev3.md)) |
| 0.1.0.dev4 | 1.5.1 | Runs on mef3io 1.5.1; plain words for its new reasons to refuse a copy; History shows recordings renamed or copied with changes ([notes](release-notes/0.1.0.dev4.md)) |
| 0.1.0.dev5 | 1.5.1 | **Join or trim**: keep part of a recording, or join parts of several into one (one after another or side by side), from its own page or the Explore selection ([notes](release-notes/0.1.0.dev5.md)) |

## Next directions (each needs a specification first)

- **Cut a recording into pieces** (for example one per day) ([#31](https://github.com/bnelair/mef3io-studio/issues/31); library side [mef3io#103](https://github.com/bnelair/mef3io/issues/103)). Saving a part and joining parts of several recordings are built (**Join or trim**, step 7b above). Every result is a new, checked copy.
- **Signal viewer:** traces, zoom and pan, gaps; built on mef3io-server's cache, decode and prefetch (driven without gRPC locally; needs a password parameter and a plain-Python API there first), fast on multi-TB recordings via mef3io [#68](https://github.com/bnelair/mef3io/issues/68).
- **Data coverage view:** when data exists and at what rate, per channel, from the block index (mef3io [#68](https://github.com/bnelair/mef3io/issues/68), [mef3io-server#13](https://github.com/bnelair/mef3io-server/issues/13)).
- **Annotations:** view, then create and edit.
- **Export** to other formats.
- **Remote recordings** via [mef3io-server](https://github.com/bnelair/mef3io-server): the viewer's code path with a remote source.
- **Validate / repair / recover** front end for mef3io's existing tools, with dry run by default.

See [design/next-directions.md](design/next-directions.md) for what is known so far.
