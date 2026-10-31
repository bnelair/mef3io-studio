# Contributing to mef3io-studio

Thanks for your interest. mef3io-studio is the desktop app on top of the
[mef3io](https://github.com/bnelair/mef3io) library. **The project is in early
development:** test versions can explore recordings, make anonymized
copies, join or trim them and show their signals in a viewer; renaming,
editing details, passwords and channel presets are still planned. The design lives in [`docs/`](docs/) and the work in
[issues](https://github.com/bnelair/mef3io-studio/issues). Comments on the
design are among the most useful contributions right now.

Everyone taking part is expected to follow our
[code of conduct](CODE_OF_CONDUCT.md).

## The one rule

**The app never reads or writes MEF bytes itself.** Every file operation goes
through mef3io's public API. If the app needs something mef3io can't do, open
an issue (or a PR) in [mef3io](https://github.com/bnelair/mef3io/issues)
first. There it gets tested against the reference implementations and becomes
available to scripts too. See [docs/architecture.md](docs/architecture.md).

## Ways to help

- **Try a test version** from the [Releases page](https://github.com/bnelair/mef3io-studio/releases) and report what is unclear or broken.
- **Comment on the design:** the [roadmap](docs/roadmap.md) and the [design notes](docs/design/).
- **Report a bug** or **request a feature** via issues; please use the templates. A privacy problem (for example an anonymized copy that still holds patient details) goes through [SECURITY.md](SECURITY.md) instead, privately.
- **Send a pull request** (see below).

## Pull requests

- Branch from `main` and open the PR against `main`.
- Keep the batch-planning logic in `model/` free of Qt, so it can be unit-tested without a GUI.
- Add tests: unit tests for logic, `pytest-qt` for UI, end-to-end tests for anything that writes files.
- **Privacy:** no patient details or original recording dates in logs, test fixtures, screenshots or issue text. Use synthetic sessions generated with mef3io.
- CI must pass on Windows, macOS and Linux.

## Never attach real recordings

Issues, PRs and discussions are public. Never attach clinical data, or
screenshots showing patient details, even partially. A small synthetic
session generated with mef3io is always enough to reproduce a problem.
