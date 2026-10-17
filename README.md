# mef3io-studio

**A desktop app for exploring, organizing, protecting and anonymizing MEF 3.0 recordings, with no programming needed.**

[![Latest release](https://img.shields.io/github/v/release/bnelair/mef3io-studio?include_prereleases&label=latest%20test%20version)](https://github.com/bnelair/mef3io-studio/releases)
[![Installers](https://img.shields.io/github/actions/workflow/status/bnelair/mef3io-studio/build.yml?branch=main&label=installer%20build)](https://github.com/bnelair/mef3io-studio/actions/workflows/build.yml)
[![License](https://img.shields.io/github/license/bnelair/mef3io-studio)](LICENSE)

The app is in early development. The current test version, **0.1.0.dev2**,
can explore recordings and make anonymized copies of them; the other
features below are still being built. See [Project status](#project-status)
and the [release notes](docs/release-notes/0.1.0.dev2.md).

## Getting the app

Download the installer for your computer from the
[**Releases** page](https://github.com/bnelair/mef3io-studio/releases):

| Computer | File | Needs |
|---|---|---|
| Windows | `mef3io-studio-<version>-windows-x64-setup.exe` (installs without administrator rights), or the `-portable.zip` (no installation) | Windows 10 or 11, 64-bit |
| Mac | `mef3io-studio-<version>-macos-arm64.dmg` (Apple silicon) or `-macos-x86_64.dmg` (Intel) | **macOS 14 (Sonoma) or later** |
| Linux | `mef3io-studio-<version>-linux-x86_64.AppImage` (or the `.tar.gz`) | a desktop Linux from 2021 or later: Ubuntu 22.04+, Debian 12+, RHEL / Rocky / Alma 9+, Fedora 35+ |

Each release also has a `SHA256SUMS` file to check that a download is complete
and unmodified.

There is **no pip package**: mef3io-studio is only distributed as these
installers. For scripting, use the [mef3io](https://github.com/bnelair/mef3io)
library directly.

> The current builds are **not code-signed yet**, so your computer will ask
> you to confirm the first start:
>
> - **Windows:** "Windows protected your PC" → *More info* → *Run anyway*.
> - **Mac (macOS 15 or later):** open the app once and close the warning, then
>   go to *System Settings → Privacy & Security*, scroll down to the message
>   about mef3io-studio and click *Open Anyway*. (On macOS 14: right-click the
>   app → *Open*.) If macOS says the app "is damaged and can't be opened", the
>   download was quarantined: move the app to *Applications* and run
>   `xattr -dr com.apple.quarantine /Applications/mef3io-studio.app` in
>   Terminal, then open it again.
> - **Linux:** make the AppImage executable (*Properties → Allow executing*,
>   or `chmod +x`), then double-click it.

mef3io-studio runs on **Windows, macOS and Linux**. It is built on
[mef3io](https://github.com/bnelair/mef3io), the library that reads and writes
MEF 3.0 files, so everything the app does to a file is done by the same
tested code that researchers use from Python and MATLAB.

---

## What is MEF 3.0, and why would I have it?

**MEF 3.0** (Multiscale Electrophysiology Format) is a file format for long
brain and body recordings: intracranial EEG, scalp EEG, ECG. Some of these
recordings run for days or months, across hundreds of channels. A single
recording can be **hundreds of gigabytes**.

MEF handles that by cutting each channel into small, compressed **blocks** and
keeping an **index** of where each block is and when it starts. A program can
jump straight to any moment in the recording and read only that part.

<p align="center">
  <img src="docs/img/session-anatomy.svg" alt="A MEF 3.0 recording is a folder: one sub-folder per channel, one per segment, and in each segment a metadata file, an index and the compressed data blocks." width="720">
</p>

A recording (a **session**, the `.mefd` folder) contains one folder per
**channel** (`.timd`). Each channel is split into **segments** (`.segd`), and
each segment has three files:

| File | What it holds | Size |
|---|---|---|
| `.tmet` | metadata: sampling rate, units, and the patient details | always 16 KB |
| `.tidx` | the index: one entry per block (where it is, when it starts, its min/max) | small, ~2% of the data |
| `.tdat` | the compressed blocks of samples | most of the recording |

A session can also be packed into a single **`.mefd.tar`** file, which is
easier to copy and share. It is read directly; there's no need to unpack it.

## What the app does

The current test version, **0.1.0.dev2**, has two pages: **Explore** and
**Anonymize**. The rest of the first release is still being built (see
[Still to come](#still-to-come-for-the-first-release)).

<p align="center">
  <img src="docs/img/screenshots/explorer-overview-light.png" alt="The Explore page: a list of recordings sorted by start time, and the details of the selected one." width="760">
</p>

### Explore (in the test version)

Open one or many recordings and see what's in them, without loading any
signal:
- channels, sampling rates and units;
- start and end time, duration, gaps;
- the parts each channel is stored in, and the notes and events;
- whether the recording is password-protected;
- its history: which version of the mef3io library made or changed it;
- a check for problems, explained in plain words.

You can also:
- see **several recordings on one timeline**, with overlaps flagged and the
  time with data added up;
- **save their times as a table** (CSV) that opens in a spreadsheet and is
  exact for scripts;
- **pack** a recording into one file, or **unpack** it again;
- see a **Privacy check** for any recording: every kind of detail that could
  identify the patient, and whether the anonymizer made it.

Patient details are **hidden by default**. Times are shown in **UTC** until
you **choose the site's time zone** (the files can't tell the app reliably).
After that, local times are correct across daylight-saving changes.

Reading big recordings shows real progress, and **Stop** stops the work
straight away. See the [user guide](docs/user-guide.md#explore-recordings).

### Anonymize (in the test version)

Make **anonymized copies** of one or many recordings: the patient details
are removed and every date and time is moved by the same amount. **The
originals are never changed**: the app makes a new copy and checks it.

<p align="center">
  <img src="docs/img/anonymize-workflow.svg" alt="Anonymization workflow: select recordings, pick a reference recording and its new date, choose relative or aligned mode, choose what identity to remove, then write verified copies." width="760">
</p>

- **Choose the channels** that go into the copies, quickly even with hundreds
  of channels: grouped by electrode, with ranges, a filter and undo.
- **Move the dates.** Give the reference recording (by default the earliest)
  a new date. Either every recording moves by the same amount, so they still
  follow each other exactly as before, or every copy starts at the same
  moment. Keep the local or the UTC time of day, or change it too.
- **Name the copies** and give each a pseudonym. The mef3io library checks
  that no name contains the patient's details before anything is written.
- **Notes:** their text is removed by default. If you choose to keep it, the
  app warns that dates and other people's names written in free text are not
  found, and it stays your responsibility.
- **Passwords:** no password (the usual choice for sharing), or two new ones.
- **Checked:** every copy is checked before it is kept. The samples must be
  exactly the same, every time must be moved by exactly the chosen amount,
  and none of the recording's original time values may remain in the copy.
  (The copy's time-zone and daylight-saving settings are not moved: they are
  cleared, so the copy doesn't hint at where it was recorded.)
- **Unattended batches:** save the copies as folders, as one archive file per
  recording, or as **one archive for the whole batch**, and walk away. Every
  archive is checked byte for byte against the checked copy. The computer is
  kept awake, a notification says when the batch is done, and a batch
  interrupted by a crash or a power cut can be continued at the next start.
- **The date key:** the original and new dates of every recording are shown
  at the end. With them, anyone can undo the anonymization, so they are saved
  to a file only if you ask.

See the [user guide](docs/user-guide.md#anonymize-recordings) and the
[anonymizer design](docs/design/anonymizer.md).

Everything that takes time (reading, packing, anonymizing, checking big
recordings) runs in the background with a **progress bar** and a **Stop**
button. The window stays usable.

### Still to come for the first release

**Order and rename.** Got a folder of recordings whose names are long random
codes? See them sorted by when they were recorded, then rename them safely:
- **preview** every change first;
- name clashes are caught before anything happens;
- **undo** whole renames.

A recording's name is stored in several places, including inside its files.
mef3io will change all of them together, so other MEF software still finds
everything, notes included. For a password-protected recording the app will
ask for the password first, because part of the name is stored in the locked
details; without it, the rename is not done.

**View and edit details.** See, and correct, the descriptive information
stored with a recording: patient details, recording location, descriptions.
Edit one recording or many at once, with a preview and undo. **Recording
times can never be edited**; they only change through anonymization.

**Add or remove passwords.** Save a copy with new passwords, or without any.
- In files written by common tools, MEF passwords lock the **patient
  details, settings and notes**.
- **They don't hide the signal, the recording's name, or its timing.** Some
  files keep the true date hidden until the second, full-access password is
  given.
- The app explains this before you rely on it.

**Save channel choices and convert in batches.** Save a choice of channels,
and apply it to many recordings at once, combined with passwords and
anonymization, in one pass per recording.

### Next directions (to be specified later)

- **Signal viewer:** scroll and zoom through the traces, with gaps and
  notes shown, fast even on multi-TB recordings.
- **Data coverage:** see when data exists and at what rate, per channel. The
  fastest way to spot dropouts and gaps.
- **Notes and events:** view them on the timeline, and later create and edit
  them.
- **Export** to other formats (e.g. EDF, NWB).
- **Remote recordings** kept on a secure server.

These need their own specifications before work starts; see
[next directions](docs/design/next-directions.md).

## How it fits together

<p align="center">
  <img src="docs/img/architecture.svg" alt="mef3io-studio is the user interface. File operations go through the mef3io library; viewing will go through mef3io-server, which is built on mef3io." width="640">
</p>

**The app never edits MEF files itself.** Everything that touches a file is
done by the [mef3io](https://github.com/bnelair/mef3io) library, where it is
tested against the reference implementations. The same operations are also
available from Python, MATLAB and the command line, for batch work on servers.

The future viewer will use
[mef3io-server](https://github.com/bnelair/mef3io-server), which is built on
mef3io and keeps recently viewed data ready, so scrolling stays smooth. It
runs inside the app for files on your computer, or as a separate server for
recordings kept elsewhere.

## Privacy

- **Nothing leaves your computer.** The app makes no network connections.
- **Logs never contain** patient details, recording names or folder paths, or
  original recording dates.
- **Anonymizing always writes a new, checked copy;** the original is not
  touched. (Password changes and channel selections, still to come, will
  work the same way. Renaming and editing details, also still to come, will
  change a recording in place, only after you confirm, with undo.)
- **Notes about an unfinished batch** (so it can be continued after a crash)
  are kept in the app's own data folder, sealed, with no names or dates in
  clear, and removed when the batch is finished or discarded. Passwords are
  never stored.
- **The date key:** if you choose to save it (it lists each original
  recording with its copy and their original and new dates), the app warns
  you that it undoes the anonymization and must be stored separately, never
  next to the copies.

## For developers

### Run from source

You need Python 3.11 or newer. Work in a virtual environment:

```bash
git clone https://github.com/bnelair/mef3io-studio
cd mef3io-studio
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"              # pins mef3io and PySide6 exactly
# or exactly what CI uses (hashed):
#   pip install --require-hashes -r requirements/dev.lock && pip install --no-deps --no-build-isolation -e .
mef3io-studio                        # or: python -m mef3io_studio
mef3io-studio --self-test            # no window: write + read a tiny synthetic recording
mef3io-studio --self-test-gui        # the same, showing the window briefly on the real display
```

`pyproject.toml` exists for development and tests only; the app is never
published to PyPI (its `Private :: Do Not Upload` classifier makes PyPI reject
it). The version lives in the single `VERSION` file.

### Run the tests

```bash
QT_QPA_PLATFORM=offscreen python -m pytest   # headless; tests/conftest.py also sets it
ruff check . && ruff format --check .
```

The suite fails if anything tries to open a network connection (in the tests
or in any child process), if `model/` imports Qt, if a patient name, a path or
a recording date reaches the log file, or if technical jargon appears in the
user interface (see [docs/ui-language.md](docs/ui-language.md)).

Opening a recording, and making each anonymized copy, runs in a short-lived
helper process. It keeps the window responsive whatever the library does (a
mef3io `Reader` open still holds Python's GIL,
[mef3io#79](https://github.com/bnelair/mef3io/issues/79)), and a crash inside
the library cannot take the app down.

### Layout

```
src/mef3io_studio/
  app.py        entry point, main window, --self-test
  model/        plain Python, no Qt: reading a recording for display (inspect),
                coverage and CSV export, the time-shift planner, anonymize,
                the batch pipeline, archives and the batch bundle, the sealed
                batch journal, the privacy check, plain-language errors and
                refusals, progress and Stop, the RecordingSource interface
  workers/      background tasks on QThreadPool, helper processes, the tasks
                the pages start (open, scan, pack, anonymize, batch)
  common/       privacy log filter, settings and the Settings dialog, theme,
                icons, activity panel
  explorer/     the Explore page (list, details, timeline, combined timeline,
                privacy check, pack/unpack)
  anonymizer/   the Anonymize wizard (recordings, channels, dates, identity,
                passwords, output, review, run) and the resume dialog
packaging/      PyInstaller spec, Inno Setup script, dmg and AppImage scripts
```

### Build an installer locally

```bash
pip install --require-hashes -r requirements/build.lock && pip install --no-deps --no-build-isolation .
pyinstaller --noconfirm --clean packaging/mef3io-studio.spec
python packaging/check_bundle.py dist/mef3io-studio   # nothing that must not ship; licences present
dist/mef3io-studio/mef3io-studio --self-test   # macOS: dist/mef3io-studio.app/Contents/MacOS/mef3io-studio
# Linux release builds are made in AlmaLinux 9 (glibc 2.34) for compatibility:
docker run --rm -v "$PWD":/src -w /src almalinux:9 packaging/linux/build-in-container.sh
```

`.github/workflows/build.yml` builds the Windows installer, the macOS `.dmg`s
and the Linux AppImage and tarball. It runs on every push to `main`, on pull
requests that touch `src/`, `packaging/`, `requirements/`, the pins or
`VERSION`, when started by hand (*Actions → Build installers → Run workflow*),
and on `v*` tags. Each build is smoke-tested (`--self-test`, `--self-test-gui`,
Windows install → upgrade → uninstall, the Linux tarball on five
distributions, an strace check that no internet socket is opened). On tags the
installers and `SHA256SUMS` are attached to a GitHub pre-release, described by
`docs/release-notes/<version>.md` when that file exists.

Every bundle carries a `THIRD_PARTY_LICENSES` folder. Qt is used under the
LGPL v3; Help → About says so and links to the licence texts and sources.

### Design

- [User guide](docs/user-guide.md)
- [Vision and scope](docs/vision.md)
- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)
- Design notes: [explorer](docs/design/explorer.md) · [anonymizer](docs/design/anonymizer.md) · [next directions](docs/design/next-directions.md)
- [Contributing](CONTRIBUTING.md)

The library side of the plan is tracked in
[mef3io#71](https://github.com/bnelair/mef3io/issues/71).

### Make a release

1. **Revise the documentation first (required).** Open a pull request that
   checks everything people read against the app that is about to ship: this
   README, [docs/user-guide.md](docs/user-guide.md) and its screenshots
   (`docs/img/screenshots/`), the texts the installers show
   (`packaging/windows/mef3io-studio.iss`, `packaging/linux/mef3io-studio.desktop`),
   the app's own description (`--help`, in `src/mef3io_studio/app.py`), and
   [docs/roadmap.md](docs/roadmap.md) including its Status column. Write the
   release notes for testers in `docs/release-notes/<version>.md` (plain
   words: what is new, what to try, known limitations, how to report a
   problem without sharing patient data); the release uses that file as its
   description, and falls back to a short generic text when there is none.
   Have it reviewed and merge it before tagging.
2. Set the new version in `VERSION` (in the same pull request or its own) and
   merge it.
3. Tag the resulting commit on `main` and push the tag:
   `git switch main && git pull && git tag v$(cat VERSION) && git push origin v$(cat VERSION)`.
   `build.yml` builds and tests every installer and, only if the tag matches
   `VERSION` (checked after the builds), publishes them as a GitHub
   pre-release with `SHA256SUMS`. Every release is published as a
   pre-release; to mark a final version, edit the release on GitHub afterwards.

## Project status

mef3io-studio is in **early development**. The app, its installers, the
**Explore** page and the **Anonymize** wizard (anonymized copies with a
choice of channels, moved dates, new names and optional passwords, plus a
Privacy check for any recording, and unattended batches into shareable
archives) are ready to try in test version **0.1.0.dev2**, built on mef3io
1.4.0. Still to come for the first full release: ordering and renaming
recordings, viewing and editing details, adding or removing passwords, and
saving channel choices and converting in batches. Test versions are
published on the [Releases page](https://github.com/bnelair/mef3io-studio/releases)
as pre-releases; what changed in each is in
[docs/release-notes/](docs/release-notes/). Progress is tracked in the
[roadmap](docs/roadmap.md) and the
[tracking issue](https://github.com/bnelair/mef3io-studio/issues/1).

## License

[Apache 2.0](LICENSE). Maintained by Filip Mivalt, Bioelectronics
Neurophysiology and Engineering Laboratory (BNEL) at Mayo Clinic.
