# mef3io-studio

**A desktop app for exploring and anonymizing MEF 3.0 recordings, with no programming needed.**

> **Status: early development.** The app shell and the installers exist; the
> Explorer and Anonymizer are being built. Follow the
> [roadmap](docs/roadmap.md) or the [tracking issue](https://github.com/bnelair/mef3io-studio/issues/1).

## Getting the app

Download the installer for your computer from the
[**Releases** page](https://github.com/bnelair/mef3io-studio/releases):

| Computer | File |
|---|---|
| Windows | `mef3io-studio-<version>-windows-x64-setup.exe` (installs without administrator rights) |
| Mac | `mef3io-studio-<version>-macos-arm64.dmg` (Apple silicon) or `-macos-x86_64.dmg` (Intel) |
| Linux | `mef3io-studio-<version>-linux-x86_64.AppImage` (or the `.tar.gz`) |

There is **no pip package**: mef3io-studio is only distributed as these
installers. For scripting, use the [mef3io](https://github.com/bnelair/mef3io)
library directly.

> The current builds are **not code-signed yet**. Windows may show a
> "Windows protected your PC" message (choose *More info → Run anyway*), and a
> Mac may ask you to confirm the first launch (right-click the app → *Open*).

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

## What the app will do

### First release: look at recordings, and anonymize them

**1. Explore.** Open one or many recordings and see what's in them, without
loading any signal:
- channels, sampling rates and units;
- start and end time, duration, gaps;
- segments and annotations;
- whether the session is encrypted;
- which mef3io version wrote it (its provenance);
- an integrity check.

Patient details are **hidden by default**.

**2. Anonymize.** Make a **copy** of one or many recordings with the patient
details removed and every time shifted. The originals are never modified.

<p align="center">
  <img src="docs/img/anonymize-workflow.svg" alt="Anonymization workflow: select recordings, pick a reference recording and its new date, choose relative or aligned mode, choose what identity to remove, then write verified copies." width="760">
</p>

- **Pick a reference** recording (by default the earliest) and give it a new
  date, and optionally a new time of day.
- **Relative mode:** every other recording moves by the same amount, so they
  still follow each other exactly as before.
- **Aligned mode:** every recording starts at the same chosen moment.
- **Verified:** every copy is checked before it is kept. The samples must be
  bit-identical, every time must be shifted by exactly the chosen amount, and a
  scan confirms that no original date remains anywhere in the files.

See the [anonymizer design](docs/design/anonymizer.md) for the details.

### Next directions (to be specified later)

- **Signal viewer:** scroll and zoom through the traces, with gaps and
  annotations shown, fast even on multi-TB recordings.
- **Annotations:** view, and later create and edit, events and notes.
- **Export** to other formats (e.g. EDF, NWB).
- **Remote recordings** through [mef3io-server](https://github.com/bnelair/mef3io-server).

These need their own specifications before work starts; see
[next directions](docs/design/next-directions.md).

## How it fits together

<p align="center">
  <img src="docs/img/architecture.svg" alt="mef3io-studio is the user interface; all reading, writing and anonymization happens in the mef3io library, which the app calls." width="640">
</p>

**The app never edits MEF files itself.** All reading, writing and
anonymization is done by the mef3io library, where it is tested against the
reference implementations. The app is the workflow and the screens on top.
The same operations are also available from Python, MATLAB and the command
line, for batch work on servers.

## Privacy

- **Nothing leaves your computer.** The app makes no network connections.
- **Logs never contain** patient details or original recording dates.
- **Anonymized copies are only written once verified;** the originals are never
  touched.
- **Key file:** if you choose to save one (it maps the original recordings to
  their pseudonyms and time shifts), the app warns you that it re-identifies
  the data and must be stored separately.

## For developers

### Run from source

You need Python 3.11 or newer. Work in a virtual environment:

```bash
git clone https://github.com/bnelair/mef3io-studio
cd mef3io-studio
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"              # pins mef3io and PySide6 exactly
mef3io-studio                        # or: python -m mef3io_studio
mef3io-studio --self-test            # no window: write + read a tiny synthetic recording
```

`pyproject.toml` exists for development and tests only; the app is never
published to PyPI (its `Private :: Do Not Upload` classifier makes PyPI reject
it). The version lives in the single `VERSION` file.

### Run the tests

```bash
QT_QPA_PLATFORM=offscreen python -m pytest   # headless; tests/conftest.py also sets it
ruff check . && ruff format --check .
```

The suite fails if anything tries to open a network connection, if `model/`
imports Qt, or if a patient name or a recording date reaches the log file.

### Layout

```
src/mef3io_studio/
  app.py        entry point, main window, --self-test
  model/        plain Python, no Qt: progress, cancellation, plain-language errors,
                the RecordingSource interface (local files today, a server later)
  workers/      background jobs on QThreadPool: throttled progress, cancel, errors
  common/       privacy log filter, settings (preferences only), theme, icons,
                activity panel
  explorer/, anonymizer/   placeholders for the next roadmap steps
packaging/      PyInstaller spec, Inno Setup script, dmg and AppImage scripts
```

### Build an installer locally

```bash
pip install ".[build]"
pyinstaller --noconfirm --clean packaging/mef3io-studio.spec
dist/mef3io-studio/mef3io-studio --self-test   # macOS: dist/mef3io-studio.app/Contents/MacOS/mef3io-studio
```

CI (`.github/workflows/build.yml`, run manually or on a `v*` tag) builds the
Windows installer, the macOS `.dmg`s and the Linux AppImage, smoke-tests each
with `--self-test`, and attaches them to a GitHub Release on tags.

### Design

- [Vision and scope](docs/vision.md)
- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)
- Design notes: [explorer](docs/design/explorer.md) · [anonymizer](docs/design/anonymizer.md) · [next directions](docs/design/next-directions.md)
- [Contributing](CONTRIBUTING.md)

The library side of the plan is tracked in
[mef3io#71](https://github.com/bnelair/mef3io/issues/71).

## License

[Apache 2.0](LICENSE). Developed at the Mayo Clinic Bioelectronics
Neurophysiology and Engineering Lab (BNEL).
