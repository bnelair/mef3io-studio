# Design: explorer (v0.1)

Open recordings and see what's in them. No signal data is loaded. Everything
here is **read-only**, except three explicit **in-place** operations:
- renaming sessions;
- editing metadata ([#14](https://github.com/bnelair/mef3io-studio/issues/14));
- updating the name stored inside a session.

Each one shows a preview, asks for confirmation, and can be undone (unless
the user switched undo off; see *Recovery artifacts*). Everything that
changes *content* (anonymization, encryption, channel subsets) always writes
a **copy** instead. See [vision → principles](../vision.md#principles).

**Timestamps are never edited, here or anywhere in the app.** Recording
times are display-only. The only way to change them is to save a new,
verified copy with the anonymizer's time shift
([#5](https://github.com/bnelair/mef3io-studio/issues/5)). mef3io itself
refuses time edits ([mef3io#73](https://github.com/bnelair/mef3io/issues/73)).

## Opening

- **File → Open** a `.mefd` folder or a `.mefd.tar` file, drag and drop, or **File → Scan folder** to find every recording under a directory.
- **Each recording loads in the background** with a progress indicator; the window never freezes. A recording that fails to open shows the reason in plain language, never a traceback.
- **What is read:** `mef3io.inspect()` ([mef3io#67](https://github.com/bnelair/mef3io/issues/67)) reads each segment's metadata file and the annotation files. **It does not read the signal**, and reads the block index (about 2% of the data) **only on demand**, e.g. for the detailed gap view.
- **Encrypted recordings** ask for a password. Without one, only what MEF leaves unencrypted is shown, with an explanation of what is hidden and why.

## How times are shown

MEF stores every time as **UTC** (microseconds since 1970), which is
unambiguous across daylight-saving changes. The app can always show UTC.

**Local time** needs the recording site's time zone, and the file can't
reliably provide it:
- **One fixed number.** MEF stores the site's offset from UTC (`GMT_offset`), not a time zone.
- **DST is ignored.** meflib displays local time as UTC + that offset and **ignores** the two daylight-saving fields (meflib.c:4649-4650).
- **The result:** a recording made in the other season shows local times one hour off. One that spans a daylight-saving change is an hour off on one side of it.

So:
- **Choose the site's time zone** (e.g. *Europe/Prague*, *America/Chicago*) **per folder**. The app then computes local time per instant from the operating system's time-zone database, correct before and after any daylight-saving change.
- **Recorded offset as fallback only.** It is shown as "recorded offset (fixed, ignores daylight saving)" and used only when no time zone is chosen.
- **Every view says** which one it uses (UTC / site time zone / recorded offset).

**Times you can't trust are labelled.** An encrypted recording opened
without its level-2 password has an **unknown** recording-time offset (it's
stored in the encrypted section), so its times are shown as *unverified*,
never as if absolute.

## What's shown

**Recordings list**, one row per recording:
- name, start date and time, duration;
- channels, sampling rate(s);
- size on disk, encrypted yes/no;
- integrity status.

It's virtualized, so hundreds of recordings stay fast.

**Detail panel** for the selected recording:

| Section | Content |
|---|---|
| Overview | Start and end, duration, gaps, channels, segments, size |
| Channels | Name, sampling rate, units, samples, segments, first and last sample time |
| Timeline | Each channel as a bar over time, with segment boundaries and gaps. Drawn from metadata; the block-level version is the coverage view (next directions) |
| Annotations | Every record at every level (session, channel, segment): time, type, text. Text is hidden while patient details are hidden |
| Patient details | Subject name, ID, location and descriptions, **masked by default**; "Show" per recording |
| Provenance | Which mef3io version created and last modified the files, and which operations were applied. Blank for files from other writers |
| Integrity | "Check now" runs `mef3io.Validator`; findings in plain language, with the technical detail expandable |

## Also in v0.1

- **Copy summary:** a text or JSON summary of a recording (masked fields stay masked) for an email or a data-management log.
- **Pack / unpack:** `.mefd` ↔ `.mefd.tar` via mef3io's `archive_session` / `extract_session`, with progress.

## Ordering and renaming sessions

A common case is a folder of sessions whose names are long random codes
(UUIDs). Tracked in [#12](https://github.com/bnelair/mef3io-studio/issues/12).

### Order by time

- **Fast:** each session's first and last sample time comes from mef3io's `time_range` ([mef3io#72](https://github.com/bnelair/mef3io/issues/72)), which reads a few small metadata files per channel and never the data.
- **The table** sorts by start and flags overlaps and gaps between sessions.
- **Sessions with unverified times** (above) are marked and kept out of the absolute order.

### Why a rename has to be more than renaming the folder

A session's name is stored in **four** places:
1. the folder name;
2. the file names of the **session-level annotation files** (`<name>.rdat` / `<name>.ridx` inside the folder);
3. the `session_name` field in **every file's header**;
4. by default, the **session description** in every metadata file.

meflib (and so pymef) builds the path to the session-level annotations
**from the folder name** (meflib.c:6335). After renaming only the folder, it
looks for `<new name>.rdat`, finds nothing, and **silently drops every
session-level annotation**. The old name also stays on disk, in those file
names and in every header, and meflib reads the stored copy too
(meflib.c:5970).

So a rename is **one mef3io operation**, `rename_session`
([mef3io#72](https://github.com/bnelair/mef3io/issues/72)), which changes all
four together. It rewrites names only: no samples, no index entries and **no
timestamps**.

### Rename safely

- **Names:** from templates (`{start:%Y-%m-%d_%H%M%S}.mefd`, `{index:03d}_…`) or typed per row.
  - `{start}` uses the time zone shown in the table (stated next to the template).
  - Sessions with unverified times can't use `{start}`.
  - Two sessions with the same start get `_1`, `_2`, ….
  - The `.mefd` / `.mefd.tar` suffix is enforced.
  - Names longer than the 255-byte stored-name field are refused, not silently truncated.
- **Preview** (current → new) of every change. These block the run:
  - collisions, compared **case-insensitively** and after **Unicode normalisation** (Windows and macOS treat `A`/`a`, and composed/decomposed accents, as the same name); a case-only rename of a session itself is allowed;
  - names that already exist;
  - characters or names Windows forbids (`<>:"/\|?*`, trailing dot or space, `CON`, `NUL`, …);
  - paths that would exceed Windows' limits at the **deepest file** inside the session, not just the folder.
- **Not while it's being written:** sessions modified in the last few minutes, or containing an unfinished write (`.partial`), are refused with an explanation, because a recorder may still be appending.
  - The app closes all its own handles to the sessions first.
  - On Windows, a file held open by another program (an antivirus scanner, OneDrive) makes the rename fail. That's an expected outcome: it rolls back cleanly.
- **Crash-safe, two-phase.**
  1. A **journal** of the planned steps is written and flushed to disk *before* anything moves.
  2. Each session then goes to a temporary name the app recognises (and other tools ignore), then to its final name. Swaps and cycles (A→B, B→A) therefore work.
  3. Renames use the operating system's **no-replace** calls, so an existing file can never be overwritten, even if it appears between the check and the rename: `renameat2(RENAME_NOREPLACE)` on Linux, `renamex_np(RENAME_EXCL)` on macOS, `MoveFileExW` without replace on Windows.
  4. **If the app is killed part-way**, the next start finds the journal and finishes or rolls back the batch. Every session ends up fully old or fully new.
- **Same folder only:** a rename never moves a session to another drive.
- **Confirmation** states how many sessions change, with no default "Yes".
- **Undo** reverts the **whole** operation: folder, annotation files, stored names and description, from the recovery artifacts. Not possible if the user switched them off.
- **Warnings:**
  - a name built from dates reveals **when** a recording was made;
  - **MEF encryption does not hide names or recording times**: both are stored unencrypted in every file's header;
  - anonymize first if the sessions are meant for sharing.

### Drift between the folder name and the stored name

Sessions renamed by other tools have a stale stored name and, if a
session-level annotation file exists, annotations meflib can't find. The
explorer flags these and offers **Fix**, which is the same `rename_session`
operation applied with the folder's current name.

### Archives

A `.mefd.tar` can only be renamed as a file, since archives are read-only. The
old name **stays inside it**: in the archive's internal folder, its file names
and the stored names. The app says so, and offers extract → rename →
re-archive.

## Recovery artifacts

Undo needs to remember what things were called, or what fields contained,
**before** a change:
- the rename journal;
- header and metadata-file backups from metadata edits and stored-name updates.

Those old values can include patient IDs or original dates, so:
- **Where they live:** by default in the app's **per-user data folder**, never next to the recordings, under file names that contain no session name. Storing them beside the recordings is an explicit opt-in, with a warning.
- **Never** copied into logs, reports, summaries or exports.
- **Recovery artifacts window:** lists them (counts and dates only) and deletes them per operation or all at once. The app reminds the user to clear them before handing a computer or folder to someone else.
- **mef3io writes its backups where the app tells it to** (a `backup_dir` argument, [mef3io#72](https://github.com/bnelair/mef3io/issues/72), [mef3io#73](https://github.com/bnelair/mef3io/issues/73)), not into a folder beside the session.
- **Turning them off** is allowed per operation. There is then no undo, and the confirmation says so.

These, and the anonymizer's optional key file, are the **only** things the
app writes that can contain identifying information. See
[SECURITY.md](../../SECURITY.md).

## Not in v0.1

- Viewing the signal, and the block-level coverage view (see [next directions](next-directions.md)).
- Repair and recovery. Integrity *checking* only.

## Acceptance

- **Opening and inspecting** a large generated recording reads **no signal data** and no block index unless asked. It reads well under 1% of the bytes in total, checked in CI with a byte count, the same method mef3io uses.
- **Patient details** never appear unless "Show" was clicked for that recording.
- **After a rename,** pymef and meflib-based readers still find the session-level annotations, and no old name remains anywhere in the session's files.
- **Killing the app** during a batch rename, then restarting, leaves every session fully old or fully new.
- **Across a daylight-saving change,** local times shown with a chosen site time zone match the operating system's time-zone database at every instant.
