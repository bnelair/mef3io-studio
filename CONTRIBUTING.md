# Contributing to mef3io-studio

Thanks for your interest. mef3io-studio is the desktop app on top of the
[mef3io](https://github.com/bnelair/mef3io) library. **The project is in
planning:** the design lives in [`docs/`](docs/) and the work in
[issues](https://github.com/bnelair/mef3io-studio/issues). Comments on the
design are the most useful contribution right now.

## The one rule

**The app never reads or writes MEF bytes itself.** Every file operation goes
through mef3io's public API. If the app needs something mef3io can't do, open
an issue (or a PR) in [mef3io](https://github.com/bnelair/mef3io/issues)
first. There it gets tested against the reference implementations and becomes
available to scripts too. See [docs/architecture.md](docs/architecture.md).

## Ways to help

- **Comment on the design:** the [roadmap](docs/roadmap.md) and the [design notes](docs/design/).
- **Report a bug** or **request a feature** via issues; please use the templates.
- **Send a pull request** once there is code (see below).

## Pull requests (once development starts)

- Branch from `main` and open the PR against `main`.
- Keep the batch-planning logic in `model/` free of Qt, so it can be unit-tested without a GUI.
- Add tests: unit tests for logic, `pytest-qt` for UI, end-to-end tests for anything that writes files.
- **Privacy:** no patient details or original recording dates in logs, test fixtures, screenshots or issue text. Use synthetic sessions generated with mef3io.
- CI must pass on Windows, macOS and Linux.

## Never attach real recordings

Issues, PRs and discussions are public. Never attach clinical data, or
screenshots showing patient details, even partially. A small synthetic
session generated with mef3io is always enough to reproduce a problem.
