# Design: metadata explorer (v0.1)

Open recordings and see what's in them. **Read-only**; no signal data is
loaded. Everything comes from `mef3io.inspect()`
([mef3io#67](https://github.com/bnelair/mef3io/issues/67)), which reads
metadata, indices and record files only.

## Opening

- **File → Open** a `.mefd` folder or a `.mefd.tar` file, drag and drop, or **File → Scan folder** to find every recording under a directory.
- Each recording loads in the background, with a progress indicator. A recording that fails to open shows the reason in plain language, never a traceback.
- An encrypted recording asks for a password. Without one, only what MEF leaves unencrypted is shown, with an explanation of what is hidden and why.

## What's shown

**Recordings list**, one row per recording:
- name, start date and time, duration;
- channels, sampling rate(s);
- size on disk, encrypted yes/no;
- integrity status (not checked / OK / problems).

**Detail panel** for the selected recording:

| Section | Content |
|---|---|
| Overview | Start and end (absolute; the time zone shown when the file records one), duration, gaps (count and total), channels, segments, size |
| Channels | Table: name, sampling rate, units, samples, segments, first and last sample time |
| Timeline | Each channel as a bar over time, with gaps visible. Drawn from the segment map; no signal is read |
| Annotations | Every record at every level (session / channel / segment): time, type, text. Text is hidden when patient details are hidden |
| Patient details | Subject name, ID, location and descriptions, **masked by default**; a "Show" button reveals them per recording |
| Provenance | Which mef3io version created the files and last modified them, and which operations were applied (append, repair, recovery, anonymize). Blank for files from other writers |
| Integrity | "Check now" runs `mef3io.Validator` and lists any findings in plain language, with the technical detail expandable |

## Also in v0.1

- **Copy summary:** a text or JSON summary of a recording (masked fields stay masked) for an email or a data-management log.
- **Pack / unpack:** `.mefd` ↔ `.mefd.tar` via mef3io's `archive_session` / `extract_session`, with progress.

## Not in v0.1

- Viewing the signal (see [next directions](next-directions.md)).
- Repair and recovery. Integrity *checking* only; writing fixes comes later, behind a confirmation.

## Acceptance

- Opening a 1 TB recording reads less than ~1% of it. Checked with a byte-count test in CI on a generated session, the same method mef3io uses.
- Every visible string is in plain language. An expert detail toggle shows the raw values.
- Patient details never appear unless the user clicked "Show" for that recording.
