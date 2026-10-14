# Design: metadata explorer (v0.1)

Open recordings and see what's in them. No signal data is loaded. Everything
is **read-only** except three explicit **in-place** operations:
- renaming sessions;
- updating the name stored inside them;
- editing metadata.

Each one shows a preview and asks for confirmation, and each can be undone
(a rename record, or header backups). Everything that changes *content*
(anonymization, encryption, channel subsets) always writes a **copy**
instead. See [vision → principles](../vision.md#principles). Everything comes from `mef3io.inspect()`
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

## Ordering and renaming sessions

Common case: a folder of sessions with UUID names. Tracked in
[#12](https://github.com/bnelair/mef3io-studio/issues/12).

- **Order by time.** Each session's first and last sample time comes from
  mef3io's fast `time_range` ([mef3io#72](https://github.com/bnelair/mef3io/issues/72)),
  which reads a few small metadata files per channel and never the data.
  - The table sorts by start and flags overlaps and gaps between sessions.
  - A session whose times can't be made absolute (encrypted, a non-zero
    recording-time offset, no password) is marked *relative* and kept out of
    the absolute order.
- **Rename safely.**
  - **Names:** from templates (`{start:%Y-%m-%d_%H%M%S}.mefd`, `{index:03d}_…`) or typed per row.
  - **Preview** of every change; the `.mefd` / `.mefd.tar` suffix is enforced.
  - **Clashes block the run:** case-insensitive collisions, existing names, characters or names Windows forbids.
  - **Two-phase rename** (via temporary names), so swaps and cycles work and a failure rolls back.
  - **Same folder only;** no cross-drive moves.
  - **Undo:** a **rename record** (old → new names) is written *before* renaming, and *Undo last rename* replays it backwards.
    - **It's sensitive.** Old names can contain patient IDs or dates, so it is treated like the anonymizer's key file: one of the two artifacts the privacy policy allows ([SECURITY.md](../../SECURITY.md)).
    - **Where it's written:** by default into the **same folder** as the sessions. It reveals nothing that folder didn't already show before the rename. The user can choose another location or switch it off (then there is no undo).
    - **Never** copied into logs, reports or summaries. The anonymizer refuses to include it in an output folder.
    - **The dialog says plainly:** "This file contains the previous names. Delete it before sharing this folder." *Delete rename record* removes it once the user is satisfied.
  - **Warning:** a name built from dates reveals when the recording was made; anonymize first if sharing.
- **Stored name.**
  - **Why it's safe to rename:** readers use the folder name (meflib derives the session name from the path), but every file also stores the name in its header.
  - **After a rename:** the app offers *Update stored name too*. This is a header-only rewrite via mef3io's `set_session_name`: only the 1024-byte header of each file changes; the headers are backed up first; the change is recorded in provenance; and mef3io verifies the session reads identically afterwards.
  - **Why in place, not a copy:** making a copy of a session that may be hundreds of GB just to change a name is not practical. The stored-name update and drift fix are therefore explicit in-place operations: preview, confirmation, header backup, undo. Users who want an untouched original can make a copy first (*Save as copy*).
  - **Drift:** the explorer flags sessions whose folder and stored names differ and offers *Fix stored name*.
  - **Archives:** `.mefd.tar` are renamed as files only (archives are read-only), and the app explains how to update the stored name if needed.

## Not in v0.1

- Viewing the signal (see [next directions](next-directions.md)).
- Repair and recovery. Integrity *checking* only; writing fixes comes later, behind a confirmation.

## Acceptance

- Opening a 1 TB recording reads less than ~1% of it. Checked with a byte-count test in CI on a generated session, the same method mef3io uses.
- Every visible string is in plain language. An expert detail toggle shows the raw values.
- Patient details never appear unless the user clicked "Show" for that recording.
