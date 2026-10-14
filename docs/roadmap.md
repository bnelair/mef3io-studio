# Roadmap

Tracked on GitHub: [tracking issue](https://github.com/bnelair/mef3io-studio/issues/1)
here, and [mef3io#71](https://github.com/bnelair/mef3io/issues/71) for the
library side. The library work always lands in mef3io **before** the app uses it.

## v0.1: explore and anonymize

| Step | Where | What |
|---|---|---|
| 0 | mef3io | Record handling fixed: [#60](https://github.com/bnelair/mef3io/issues/60), [#61](https://github.com/bnelair/mef3io/issues/61), [#62](https://github.com/bnelair/mef3io/issues/62), [#63](https://github.com/bnelair/mef3io/issues/63) |
| 1 | mef3io | `inspect()` [#67](https://github.com/bnelair/mef3io/issues/67), cancellation and progress [#69](https://github.com/bnelair/mef3io/issues/69), `anonymize_session` + `verify` [#70](https://github.com/bnelair/mef3io/issues/70), `time_range` + `set_session_name` [#72](https://github.com/bnelair/mef3io/issues/72), released as **mef3io 1.3** |
| 2 | studio | App shell, background jobs, privacy-safe logging, settings |
| 3 | studio | **Installers** for Windows, macOS and Linux, built in CI. Done early, so packaging and signing problems surface before the features exist |
| 4 | studio | **Explorer** (metadata), plus **ordering and safe renaming** of sessions ([#12](https://github.com/bnelair/mef3io-studio/issues/12); needs [mef3io#72](https://github.com/bnelair/mef3io/issues/72)) |
| 5 | studio | **Anonymizer** wizard |
| 6 | studio | User guide with screenshots; v0.1 release |

## Next directions (each needs a specification first)

- **Signal viewer:** traces, zoom and pan, gaps; fast on multi-TB recordings via mef3io [#68](https://github.com/bnelair/mef3io/issues/68).
- **Annotations:** view, then create and edit.
- **Export** to other formats.
- **Remote recordings** via [mef3io-server](https://github.com/bnelair/mef3io-server).
- **Validate / repair / recover** front end for mef3io's existing tools, with dry run by default.

See [design/next-directions.md](design/next-directions.md) for what is known so far.
