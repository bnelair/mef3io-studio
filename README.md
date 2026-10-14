# mef3io-studio

**A desktop app for exploring and anonymizing MEF 3.0 recordings, with no programming needed.**

> **Status: planning.** Nothing to install yet. This repository currently holds
> the plan, the design and the open issues for the first release. Follow the
> [roadmap](docs/roadmap.md) or the [tracking issue](https://github.com/bnelair/mef3io-studio/issues/1).

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

**2. Order and rename.** Got a folder of sessions with meaningless (UUID)
names? See them sorted by when they were recorded, then rename them safely:
- **preview** every change first;
- clashes are caught before anything happens;
- **undo** is one click.

The name stored inside the files is updated to match, and sessions where the
two disagree can be fixed.

**3. Anonymize.** Make a **copy** of one or many recordings with the patient
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
- **Anonymizing, encrypting and channel subsets always write verified copies;**
  the originals are not touched. Renaming and metadata edits change a
  recording in place only when you confirm, and can be undone.
- **Key file:** if you choose to save one (it maps the original recordings to
  their pseudonyms and time shifts), the app warns you that it re-identifies
  the data and must be stored separately.

## For developers

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
