# Design: explorer (v0.1)

**Status (test version 0.1.0.dev2):** built: opening and scanning, how times
are shown, the recordings list and the detail tabs (plus the Privacy check
card, see [anonymizer](anonymizer.md#as-built-wizard-for-010dev2)), the
combined timeline, Copy summary, Save times as a table, and pack / unpack.
Not built yet: ordering and renaming, the drift fix, archive renames, undo
and the recovery-artifacts window, and metadata editing; they need
[mef3io#72](https://github.com/bnelair/mef3io/issues/72) and
[mef3io#73](https://github.com/bnelair/mef3io/issues/73).

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
`GMT_offset` is **read-only in v0.1** too: metadata edits pass mef3io
field-level changes only, never a whole metadata object whose defaults could
silently write an offset of 0.

## Opening

- **File → Open** a `.mefd` folder or a `.mefd.tar` file, drag and drop, or **File → Scan folder** to find every recording under a directory.
- **Each recording loads in the background** with a progress indicator; the window never freezes. A recording that fails to open shows the reason in plain language, never a traceback.
- **What is read:** `mef3io.inspect()` ([mef3io#67](https://github.com/bnelair/mef3io/issues/67)) reads each segment's metadata file and the annotation files. **It does not read the signal or the block index** (about 2% of the data). A part with gaps inside it is shown striped, without saying where; the block-level view is the coverage view (next directions). The app reads it with `strict=False`: unreadable parts are listed and the rest is shown.
- **Encrypted recordings** ask for a password. Without one, only what MEF leaves unencrypted is shown, with an explanation of what is hidden and why: "In files written by common tools, MEF passwords lock the patient details, settings and annotations. They don't hide the signal, the recording's name, or its timing. Some files keep the true date hidden until the full-access password is given." (The app says "basic password" for level 1 and "full-access password" for level 2.)

## How times are shown

MEF stores every time as **UTC** (microseconds since 1970), which is
unambiguous across daylight-saving changes. The app can always show UTC.

**Local time** needs the recording site's time zone, and the file can't
reliably provide it:
- **One fixed number.** MEF stores the site's offset from UTC (`GMT_offset`), not a time zone.
- **DST is ignored.** meflib displays local time as UTC + that offset and **ignores** the two daylight-saving fields (meflib.c:4649-4650).
- **The result:** a recording made in the other season shows local times one hour off. One that spans a daylight-saving change is an hour off on one side of it.

So:
- **Until a site time zone is chosen, times are shown in UTC,** clearly labelled as UTC. The app never shows "local time" from the file's own offset.
- **Choose the site's time zone** (e.g. *Europe/Prague*, *America/Chicago*): **one per app profile**. The app then computes local time per instant from the operating system's time-zone database, correct before and after any daylight-saving change.
- **A per-window override** shows the open recordings in another time zone. It is **not saved**: closing the window forgets it. Preferences therefore never record which folders exist or where they are.
- **The file's `GMT_offset`** may still appear as a raw value among the read-only technical details (it is in level-2 encrypted section 3, so it is missing without the level-2 password). It is never used to compute displayed times.
- **Every view says** which one it uses (UTC / site time zone).

**Times you can't trust are labelled *unverified*,** never shown as if
absolute, and kept out of the absolute order:
- **Encrypted, no level-2 password:** the recording-time offset is stored in the encrypted section, so it is **unknown**; `inspect()` reads the times assuming the common case (no offset) and says so (`offset_assumed_zero`).
- **Parts that disagree about the offset** (`inspect()`'s `time_offset` status `disputed`): there is no single value.
- **Files with a non-zero recording-time offset** (the deprecated way older tools hid the date): older tools stored such times with two sign conventions that can't be told apart from the bytes. The app shows the times as mef3io works them out, labelled unverified, and warns that the recording is **not** anonymized (the real date is recoverable).

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
| Privacy check | Every kind of identifying detail, ✓ empty / ✗ found / – unknown, and whether the anonymizer made the recording (see [anonymizer](anonymizer.md#as-built-wizard-for-010dev2)) |
| Integrity | "Check now" runs `mef3io.Validator`; findings in plain language, with the technical detail expandable |

**Combined timeline** (instead of the tabs, chosen next to them): the
recordings selected in the list (two or more), otherwise every listed
recording, **one row per recording** on a shared time axis. Per recording:
the union of its channels' parts, shaded by whether all or only some
channels have data; parts whose samples cover less time than they span are
striped ("gaps inside"). Across recordings with **verified** times only:
first and last time, span, time with data, gaps between recordings, and
**overlaps** per pair (flagged as possible copies when they share at least
90% of the shorter one's data and have the same channels; pairs are not
listed beyond 2000 comparisons). Unverified rows are drawn grey and labelled,
kept after the others and out of every figure; locked and unreadable rows say
why there is nothing to draw. Metadata only (what the open already read);
laid out in short slices on the GUI thread (a Python worker thread stalls
the window through the interpreter lock), and painted per pixel column, so
400 recordings with thousands of parts stay responsive (test-pinned).

## Also in v0.1

- **Copy summary:** a text or JSON summary of a recording, for an email or a data-management log. This is a **user-initiated export**: the user's explicit act, not something the app writes on its own. **Names and dates are masked** in it unless the user chose "Show" for that recording, in which case the app warns that the summary will contain them before copying.
- **Save times as a table (CSV):** one row per recording, or one per part of
  each channel, for the combined timeline's recordings or the selection.
  Every start and end is written for people and for machines: readable
  `YYYY-MM-DD HH:MM:SS` in UTC (spreadsheets parse it as a date), UTC ISO
  8601 with `Z`, Unix seconds (exact decimal), microseconds since 1970
  (exact in scripts; spreadsheets keep 15 digits, so import that column as
  text) and, when a site zone is chosen, site time readable and in ISO 8601
  with its offset. A **user-initiated export**
  that contains dates: the app warns before writing. Patient details only
  where "Show" was chosen (`[hidden]` otherwise); unverified times are marked
  as such. UTF-8 with BOM, formula-like text cells prefixed with `'`, written
  to a temporary name and renamed when complete.
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
timestamps**. Descriptions or annotation text that merely *contain* the old
name among other words are not changed; the app says so.

### Rename safely

The safety rules below (recent writes, open handles, crash safety,
confirmation) apply to every in-place operation in the app: a rename, a drift
fix, an archive rename, a metadata edit, and the **undo** of any of them.

- **Names:** from templates (`{start:%Y-%m-%d_%H%M%S}.mefd`, `{index:03d}_…`) or typed per row.
  - `{start}` uses the chosen site time zone if one is set, otherwise UTC, and the label next to the template says which. It never uses the file's recorded offset.
  - Sessions with unverified times can't use `{start}`.
  - Two sessions with the same start get `_1`, `_2`, ….
  - The `.mefd` / `.mefd.tar` suffix is enforced.
  - **Length, in bytes of UTF-8 before the suffix:** at most **250** for a session folder, so `<name>.mefd` and `<name>.ridx` fit a 255-byte file name and the name fits the 255 usable bytes of the 256-byte stored-name field (the last byte is the terminator); at most **246** for an archive, so `<name>.mefd.tar` fits. Longer names are **refused, never truncated**.
  - **Encoded length on the target file system:** macOS HFS+ stores names decomposed (NFD), which can make accented names longer in bytes. The limit is checked against the name as that file system will store it.
- **Encrypted sessions need the level-1 password.** The session description lives in metadata section 2, which is level-1 encrypted, and it must be checked (and updated where it equals the old name). Without the password the rename is **refused** with that explanation. There is no partial rename.
- **Preview** (current → new) of every change. These block the run:
  - collisions, compared **case-insensitively** and after **Unicode normalisation** (Windows and macOS treat `A`/`a`, and composed/decomposed accents, as the same name); a case-only rename of a session itself is allowed;
  - names that already exist;
  - characters or names Windows forbids (`<>:"/\|?*`, trailing dot or space, `CON`, `NUL`, …);
  - paths that would exceed Windows' limits at the **deepest file** inside the session, not just the folder.
- **Not while it's being written:** sessions modified in the last few minutes, or containing an unfinished write (`.partial`), are refused with an explanation, because a recorder may still be appending.
  - **The limit, stated plainly:** an *idle* recorder that holds a session open without writing to it can't be detected by the "recently modified" check. The confirmation asks the user to make sure no recorder or other program has the session open.
- **Open handles:**
  - Before renaming, the app closes **its own** readers for those sessions, and tells the recording source to drop its cached readers for those paths. mef3io-server's decode workers each cache one reader per path (reader_pool.py:24-34), so the server needs a "drop this path in every worker" call ([mef3io-server#15](https://github.com/bnelair/mef3io-server/issues/15)).
  - On Windows, a file held open by another program (an antivirus scanner, OneDrive) makes the rename fail. That's an expected outcome: it rolls back cleanly.
- **Crash-safe, two-phase.**
  1. **First, the journal.** The **journal** of the planned steps (with the old names) is written to `backup_dir` in the app's per-user data folder and flushed to disk. **The journal is mandatory.**
  2. **Then the marker,** still before anything changes: mef3io writes a small **"operation in progress" marker inside the session**, and **flushes both the marker file and its directory entry** to disk. Its file name is opaque, and it holds no old names or paths: only which tool started the operation, the operation kind and a random operation ID. **Every mef3io call that touches the session detects it.** Only then do the changes start.
  3. Each session then goes to a **temporary name**, then to its final name. Swaps and cycles (A→B, B→A) therefore work. Temporary names are **opaque and bounded**: a fixed 32-character random token plus the suffix, never `<name>` plus something, so they can't exceed the length limit or reveal the name.
  4. Renames use the operating system's **no-replace** calls, so an existing file can never be overwritten, even if it appears between the check and the rename: `renameat2(RENAME_NOREPLACE)` on Linux, `renamex_np(RENAME_EXCL)` on macOS, `MoveFileExW` without replace on Windows. Where the file system doesn't support them (some network and FAT file systems report `EINVAL` / `ENOTSUP`), the rename is **refused**. It **never falls back** to a plain rename.
  5. **On completion,** the marker is deleted **first**, then the journal.
- **After a crash:**
  - **Marker and journal both present:** the next start finishes or rolls back the batch from the journal. Every session ends up fully old or fully new.
  - **Journal present, marker absent:** the crash came between writing the journal and writing the marker, before any change (or just after the marker was deleted on completion, when everything is done). Nothing is half-changed, so mef3io verifies the four name places and **discards** the journal. The user is never refused.
  - **Marker present, journal missing** (the operation was started by another user, on another machine, or by a script, or the journal folder was lost): writes are **refused** with a plain explanation naming the tool that started the operation and how to finish or roll it back from there. If mef3io's `name_drift` shows that **all four name places agree**, the user may clear the marker with `mef3io.clear_operation_marker(path)`, an explicit, confirmed step.
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
- It uses the same marker, journal, no-replace rules and confirmation as a rename.
- **Its undo** is `fix_name_drift(path, target=<old stored name>)`: it restores the stored names and the session-level `.rdat` / `.ridx` names, and leaves the folder as it is. It is journaled, with the same safety checks as every other undo.

### Archives

A `.mefd.tar` is renamed as a file, by mef3io's
`rename_archive(path, new_name)`
([mef3io#72](https://github.com/bnelair/mef3io/issues/72)), never by the app
directly:
- **No-replace,** previewed and confirmed, with the same "not while it's being written" checks;
- **at most 246 bytes** of UTF-8 for the new name;
- **undo** renames it back, with the same checks.

**The archive's inside still carries the old name**: its internal folder, its
file names and the stored names. The app says so plainly, and offers
extract → rename → re-archive.

## Undo

**Undo is a logical inverse, never a byte restore.**
- **Undoing a rename** is `rename_session(new → old)`. It reverts only the descriptions the rename actually changed.
- **Undoing a drift fix** is `fix_name_drift(path, target=<old stored name>)` (above).
- **Undoing an archive rename** is `rename_archive` back to the old name.
- **Undoing a metadata edit** is `edit_metadata` with the **old values of only the fields that changed**, as field-level changes, and **only on the segments the edit patched**. Segments created later are untouched.

Restoring saved bytes would also restore whatever those bytes held at the
time, for example header end times from before a later append. A logical
inverse touches only the names or fields the operation changed, never times.

**An undo is itself an in-place operation,** so it applies the same safety
checks as the operation it reverses (see *Rename safely*):
- the recent-write and `.partial` checks, with the same note that an idle recorder holding the session open can't be detected (see *Recency* below for how the operation's own writes are excluded);
- the marker and the mandatory crash journal;
- no-replace renames, refused where unsupported;
- preview and confirmation;
- **the password again** for an encrypted session (level 1 for a rename or a section-2 edit, level 2 for a section-3 edit), because passwords are never stored.

**Recency.** The undo information records the **post-operation** modification
time and size of every file the operation touched. The recent-write check for
an undo counts only modifications made **after** the operation completed, so
undoing right after an operation works. A file changed since, and modified in
the last few minutes, means something may be writing now: undo refuses.

**What blocks an undo, and what doesn't.**
- **Blocked:** anything that makes the inverse wrong, i.e. the name or the edited fields **changed again since** (by another tool or a later operation). Undo refuses and explains what changed.
- **Not blocked: a finished append.** The recording grew (files, end times and entry counts differ from the recorded values), but the inverse touches only names or fields. The dialog says so: "This recording has grown since the rename. Undoing only changes the names back; the new data and all times are kept."

## Recovery artifacts

Two kinds of thing remember what names or fields contained **before** a
change:
- **Crash-rollback data**, needed only *while* an operation runs: the journal (old names) and byte-level backups (headers, `.tmet` images). The journal is **mandatory**. All of it is used only to roll back an interrupted operation within that same operation, and is **always deleted** when the operation completes. There is no option to keep it.
- **Undo information**, kept *after* the operation. It holds the **old and new** names or field values, which descriptions or `.tmet` files changed, the post-operation modification times and sizes of every file touched, and each segment's end time and entry counts. **Keeping it is optional**, per operation; without it there is no undo, and the confirmation says so.

Those values include patient IDs, old names and **original dates** (the
segment end times), so they are identifying:
- **Where they live:** only in the app's **per-user data folder**, under file names that contain no session name. **Never beside the recordings.** The only thing ever written inside a session is the non-identifying "operation in progress" marker, and only while the operation runs.
- **Never** copied into logs, reports, summaries or exports.
- **Recovery artifacts window:** lists them (counts and dates only) and deletes them per operation or all at once. It **never deletes the journal of an unfinished operation**: it finishes or rolls that operation back first. The app reminds the user to clear them before handing a computer to someone else.
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
- **After a rename,** pymef and meflib-based readers still find the session-level annotations. No old name remains in any `session_name` field, any session-level record file name, or any description that equalled the old name. (Descriptions or annotation text that merely *contain* the old name are out of scope.)
- **Killing the app** during a batch rename, then restarting, leaves every session fully old or fully new. A journal without a marker means nothing was changed yet, so it is discarded without asking; a marker without a journal is refused with the explanatory message.
- **Undo is a logical inverse:** append to a session after a rename, undo the rename, and every timestamp is byte-identical to the post-append state. Undo right after an operation is not refused as "recently modified".
- **Across a daylight-saving change,** local times shown with a chosen site time zone match the operating system's time-zone database at every instant.
