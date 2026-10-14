# Design: explorer (v0.1)

Open recordings and see what's in them. No signal data is loaded. Everything
here is **read-only**, except two explicit **in-place** operations:
- **renaming sessions**, which always updates the name stored inside the
  session too (one mef3io operation);
- **editing metadata** ([#14](https://github.com/bnelair/mef3io-studio/issues/14)).

Each one shows a preview, asks for confirmation, and can be undone (see
*Undo*). Everything that changes *content* (anonymization, encryption, channel
subsets) always writes a **copy** instead. See
[vision → principles](../vision.md#principles).

**Timestamps are never edited, here or anywhere in the app.** Recording
times are display-only. The only way to change them is to save a new,
verified copy with the anonymizer's time shift
([#5](https://github.com/bnelair/mef3io-studio/issues/5)). mef3io itself
refuses time edits ([mef3io#73](https://github.com/bnelair/mef3io/issues/73)).

## Opening

- **File → Open** a `.mefd` folder or a `.mefd.tar` file, drag and drop, or **File → Scan folder** to find every recording under a directory.
- **Each recording loads in the background** with a progress indicator; the window never freezes. A recording that fails to open shows the reason in plain language, never a traceback.
- **What is read:** `mef3io.inspect()` ([mef3io#67](https://github.com/bnelair/mef3io/issues/67)) reads each segment's metadata file and the annotation files. **It does not read the signal**, and reads the block index (about 2% of the data) **only on demand**, e.g. for the detailed gap view.
- **Encrypted recordings** ask for a password. Without one, only what MEF leaves unencrypted is shown, with an explanation of what is hidden and why: "In files written by common tools, MEF passwords lock the patient details, settings and annotations. They don't hide the signal, the recording's name, or its timing. Some files keep the true date hidden until the level-2 password is given."

## How times are shown

MEF stores every time as **UTC** (microseconds since 1970), which is
unambiguous across daylight-saving changes. The app can always show UTC.

**Local time** needs the recording site's time zone, and the file can't
reliably provide it:
- **One fixed number.** MEF stores the site's offset from UTC (`GMT_offset`), not a time zone.
- **DST is ignored.** meflib displays local time as UTC + that offset and **ignores** the two daylight-saving fields (meflib.c:4649-4650).
- **The result:** a recording made in the other season shows local times one hour off. One that spans a daylight-saving change is an hour off on one side of it.

So:
- **Choose the site's time zone** (e.g. *Europe/Prague*, *America/Chicago*). It is set **per app profile**, with optional **per-folder overrides**. The app then computes local time per instant from the operating system's time-zone database, correct before and after any daylight-saving change.
- **No paths in preferences.** A per-folder override is stored under a **salted hash** of the folder path, never the path itself, so the settings file doesn't list where recordings live.
- **Recorded offset as fallback only.** It is shown as "recorded offset (fixed, ignores daylight saving)" and used only when no time zone is chosen. `GMT_offset` is in metadata section 3, which is level-2 encrypted, so for an encrypted recording opened **without the level-2 password** this fallback is **unavailable**: the view offers UTC or a chosen time zone only, and says why.
- **Every view says** which one it uses (UTC / site time zone / recorded offset).

**Times you can't trust are labelled *unverified*,** never shown as if
absolute, and kept out of the absolute order:
- **Encrypted, no level-2 password:** the recording-time offset is stored in the encrypted section, so it is **unknown**.
- **Legacy files with a non-zero recording-time offset:** older tools stored such times with the opposite sign convention to meflib, and the two can't be told apart from the bytes. mef3io reports these as not absolute (`offset_known=False`, [mef3io#72](https://github.com/bnelair/mef3io/issues/72)).

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

- **Copy summary:** a text or JSON summary of a recording, for an email or a data-management log. This is a **user-initiated export**: the user's explicit act, not something the app writes on its own. **Names and dates are masked** in it unless the user chose "Show" for that recording, in which case the app warns that the summary will contain them before copying.
- **Pack / unpack:** `.mefd` ↔ `.mefd.tar` via mef3io's `archive_session` / `extract_session`, with progress.

## Ordering and renaming sessions

A common case is a folder of sessions whose names are long random codes
(UUIDs). Tracked in [#12](https://github.com/bnelair/mef3io-studio/issues/12).

### Order by time

- **Fast:** each session's first and last sample time comes from mef3io's `time_range` ([mef3io#72](https://github.com/bnelair/mef3io/issues/72)), which reads a few small metadata files per channel and never the data.
- **The table** sorts by start and flags overlaps and gaps between sessions.
- **Sessions with unverified times** (above: encrypted without the level-2 password, or a legacy non-zero recording-time offset) are marked and kept out of the absolute order.

### Why a rename has to be more than renaming the folder

A session's name is stored in **four** places:
1. the folder name;
2. the file names of the **session-level annotation files** (`<name>.rdat` / `<name>.ridx` inside the folder);
3. the `session_name` field in **every file's header**;
4. the **session description** in every metadata file, **only where it equals the old name** (mef3io's default when writing).

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

The safety rules below (recent writes, open handles, crash safety,
confirmation) apply to every in-place operation in the app: a rename, a drift
fix, a metadata edit, and the **undo** of any of them.

- **Names:** from templates (`{start:%Y-%m-%d_%H%M%S}.mefd`, `{index:03d}_…`) or typed per row.
  - `{start}` uses the time zone shown in the table (stated next to the template).
  - Sessions with unverified times can't use `{start}`.
  - Two sessions with the same start get `_1`, `_2`, ….
  - The `.mefd` / `.mefd.tar` suffix is enforced.
  - **At most 250 bytes of UTF-8** (before the suffix). Then `<name>.mefd` and `<name>.ridx` fit a 255-byte file name, and the name fits the 255 usable bytes of the 256-byte stored-name field (the last byte is the terminator). Longer names are **refused, never truncated**.
- **Encrypted sessions need the level-1 password.** The session description lives in metadata section 2, which is level-1 encrypted, and it must be checked (and updated where it equals the old name). Without the password the rename is **refused** with that explanation. There is no partial rename.
- **Preview** (current → new) of every change. These block the run:
  - collisions, compared **case-insensitively** and after **Unicode normalisation** (Windows and macOS treat `A`/`a`, and composed/decomposed accents, as the same name); a case-only rename of a session itself is allowed;
  - names that already exist;
  - characters or names Windows forbids (`<>:"/\|?*`, trailing dot or space, `CON`, `NUL`, …);
  - paths that would exceed Windows' limits at the **deepest file** inside the session, not just the folder.
- **Not while it's being written:** sessions modified in the last few minutes, or containing an unfinished write (`.partial`), are refused with an explanation, because a recorder may still be appending.
  - **The limit, stated plainly:** an *idle* recorder that holds a session open without writing to it can't be detected by the "recently modified" check. The confirmation asks the user to make sure no recorder or other program has the session open.
- **Open handles:**
  - Before renaming, the app closes **its own** readers for those sessions, and tells the recording source (the future viewer's mef3io-server, whose decode workers cache one reader per path, reader_pool.py:24-34) to drop them.
  - On Windows, a file held open by another program (an antivirus scanner, OneDrive) makes the rename fail. That's an expected outcome: it rolls back cleanly.
- **Crash-safe, two-phase.**
  1. **Before anything changes,** mef3io writes a small **"operation in progress" marker inside the session**. Its file name is opaque, and it holds no old names or paths: only which tool started the operation, the operation kind, and a random operation ID. **Every mef3io call that touches the session detects it.**
  2. The **journal** of the planned steps (with the old names) is written to `backup_dir` in the app's per-user data folder and flushed to disk, also before anything moves. **The journal is mandatory** and is deleted when the operation completes.
  3. Each session then goes to a temporary name the app recognises (and other tools ignore), then to its final name. Swaps and cycles (A→B, B→A) therefore work.
  4. Renames use the operating system's **no-replace** calls, so an existing file can never be overwritten, even if it appears between the check and the rename: `renameat2(RENAME_NOREPLACE)` on Linux, `renamex_np(RENAME_EXCL)` on macOS, `MoveFileExW` without replace on Windows. Where the file system doesn't support them (some network and FAT file systems report `EINVAL` / `ENOTSUP`), the rename is **refused**. It **never falls back** to a plain rename.
  5. **If the app is killed part-way**, the next start finds the marker and the journal and finishes or rolls back the batch. Every session ends up fully old or fully new.
  6. **Marker found but journal missing** (the operation was started by another user, on another machine, or by a script): mef3io and the app **refuse** to touch the session, with a clear message naming the tool that started the operation and how to finish or roll it back from there.
- **Same folder only:** a rename never moves a session to another drive.
- **Confirmation** states how many sessions change, with no default "Yes".
- **Undo:** see *Undo* below.
- **Warnings:**
  - a name built from dates reveals **when** a recording was made;
  - **MEF passwords don't hide the recording's name or its timing**: the name is stored unencrypted in every file's header, and so are the times (some files keep the true date hidden until the level-2 password is given);
  - anonymize first if the sessions are meant for sharing.

### Drift between the folder name and the stored name

Sessions renamed by other tools have a stale stored name and, if a
session-level annotation file exists, annotations meflib can't find. The
explorer flags these and offers **Fix**, a separate mef3io operation,
`fix_name_drift(path, *, password, backup_dir)`
([mef3io#72](https://github.com/bnelair/mef3io/issues/72)):
- **The target** is the folder's current name.
- **The old name** comes from the names stored in the headers, or from the single session-level `*.rdat` file.
- **It refuses when that is ambiguous:** the stored names disagree with each other, or there are several `.rdat` files. The app explains which, and changes nothing.
- **Case-only drift** (e.g. folder `Patient.mefd`, files `patient.rdat`) is handled explicitly on case-insensitive file systems, by going through a temporary name, not refused as "target exists".
- It uses the same marker, journal, no-replace rules and undo as a rename.

### Archives

A `.mefd.tar` can only be renamed as a file, since archives are read-only. The
old name **stays inside it**: in the archive's internal folder, its file names
and the stored names. The app says so, and offers extract → rename →
re-archive.

## Undo

**Undo is a logical inverse, never a byte restore.**
- **Undoing a rename** is `rename_session(new → old)`.
- **Undoing a metadata edit** is `edit_metadata` with the **old values of only the fields that changed**.

Restoring saved bytes would also restore whatever those bytes held at the
time, for example header end times from before a later append. A logical
inverse touches only the names or fields the operation changed, never times.

**An undo is itself an in-place operation,** so it applies the same safety
checks as the operation it reverses (see *Rename safely*):
- sessions modified in the last few minutes, or with a `.partial` file, are refused (a recorder may be appending under the new name right now), with the same note that an idle recorder holding the session open can't be detected;
- the mandatory crash journal and the in-session "operation in progress" marker;
- no-replace renames, refused where unsupported;
- preview and confirmation.

**What blocks an undo, and what doesn't.** At operation time the app records
the new name or field values, and each segment's end time and entry counts.
- **Blocked:** anything that makes the inverse wrong, i.e. the name or the edited fields **changed again since** (by another tool or a later operation). Undo refuses and explains what changed.
- **Not blocked: an append.** The recording grew (end times and entry counts differ), but the inverse touches only names or fields. The dialog says so: "This recording has grown since the rename. Undoing only changes the names back; the new data and all times are kept."

## Recovery artifacts

Two kinds of thing remember what names or fields contained **before** a
change:
- **Crash-rollback data**, needed only *while* an operation runs: the journal (old names) and byte-level backups (headers, `.tmet` images). The journal is **mandatory**. All of it is used only to roll back an interrupted operation within that same operation, and is **deleted when the operation completes**, unless the user chose to keep it.
- **Undo information**, kept *after* the operation: the old name or the old field values, plus the segment end times and entry counts used to explain changes. **Keeping it is optional**, per operation; without it there is no undo, and the confirmation says so.

Those old values can include patient IDs or original dates, so:
- **Where they live:** by default in the app's **per-user data folder**, never next to the recordings, under file names that contain no session name. Storing them beside the recordings is an explicit opt-in, with a warning. The only thing ever written inside a session is the non-identifying "operation in progress" marker, and only while the operation runs.
- **Never** copied into logs, reports, summaries or exports.
- **Recovery artifacts window:** lists them (counts and dates only) and deletes them per operation or all at once. The app reminds the user to clear them before handing a computer or folder to someone else.
- **mef3io writes its journal and backups where the app tells it to** (a `backup_dir` argument, [mef3io#72](https://github.com/bnelair/mef3io/issues/72), [mef3io#73](https://github.com/bnelair/mef3io/issues/73)), not into a folder beside the session.

These, and the anonymizer's optional key file, are the **only** things the
app writes on its own that can contain identifying information.
User-initiated exports (the copy summary with "Show" chosen) are the user's
explicit act, with a warning. See [SECURITY.md](../../SECURITY.md).

## Not in v0.1

- Viewing the signal, and the block-level coverage view (see [next directions](next-directions.md)).
- Repair and recovery. Integrity *checking* only.

## Acceptance

- **Opening and inspecting** a large generated recording reads **no signal data** and no block index unless asked. It reads well under 1% of the bytes in total, checked in CI with a byte count, the same method mef3io uses.
- **Patient details** never appear unless "Show" was clicked for that recording.
- **After a rename,** pymef and meflib-based readers still find the session-level annotations, and no old name remains anywhere in the session's files.
- **Killing the app** during a batch rename, then restarting, leaves every session fully old or fully new. A leftover marker with no journal is refused with the explanatory message.
- **Undo is a logical inverse:** append to a session after a rename, undo the rename, and every timestamp is byte-identical to the post-append state.
- **Across a daylight-saving change,** local times shown with a chosen site time zone match the operating system's time-zone database at every instant.
