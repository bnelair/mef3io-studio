# Design: anonymizer (v0.1)

Make **de-identified copies** of one or many recordings: patient details
removed, every time shifted. The originals are never modified. All the work
on the files is done by mef3io's `anonymize_session` and `verify`
([mef3io#70](https://github.com/bnelair/mef3io/issues/70)). The app's job is
to let the user decide **how much each recording is shifted**, and to run and
report the batch.

![The anonymization workflow in six steps.](../img/anonymize-workflow.svg)

## Step 1: select recordings

The same list as the explorer: open, drag and drop, or scan a folder. Each
recording is inspected in the background; no signal is read. For each
recording the app shows its start and end, channels, encryption, and **what
identifying information it contains**, including annotation text.

It flags anything that will stop a recording from being anonymized:
- an encrypted session without the password;
- video channels;
- a legacy time convention mef3io cannot interpret unambiguously.

## Step 2: choose the time mapping

**Reference recording.** By default the one that starts **earliest**; the user
can pick any other.

**New start for the reference:**
- **Keep the time of day** (default): only the date changes. The reference recording starts at the same clock time on the new date. Local or UTC clock: see *Time zone* below.
- **Set the time too:** the time of day is anonymized as well.

**Mode:**

| Mode | What happens | Use when |
|---|---|---|
| **Relative** (default) | Every recording is shifted by **the same amount** as the reference. Order, gaps and overlaps between recordings are preserved exactly. | Recordings of one subject that must still follow each other |
| **Aligned** | **Every** recording is moved to start at the same chosen moment. | Making recordings anonymous *and* comparable from a common start |

**Options:**
- **Time zone:**
  - **The problem:** MEF stores times in UTC plus one fixed offset (`GMT_offset`). meflib ignores the daylight-saving fields when it shows local time (meflib.c:4649-4650), so the file alone can't say what the clock on the wall read.
  - **The local clock:** the user picks the recording site's time zone (e.g. *America/Chicago*), and the app computes the local wall-clock time from the operating system's time-zone database. That's correct on either side of a daylight-saving change.
  - **What every copy carries, in both modes below:** `GMT_offset = 0`, `DST_start_time` and `DST_end_time` set to NO_ENTRY (**not shifted**), and `recording_time_offset = 0`. The site's time zone (a location hint) is gone.
  - **D** is a whole number of days, and depends on the mode: in "keep local time of day", **D = (the chosen new date) − (the reference's local start date in the site time zone)**; in "keep UTC time of day", **D = (the chosen new date) − (the reference's UTC start date)**. Each mode measures D on the same clock it keeps; otherwise the copy's date can land a day off when the local and UTC dates differ.
  - **Default, "keep local time of day":** the shift is **D + off_ref**, where **off_ref** is the reference start's UTC offset in the chosen site time zone (at that instant, so daylight saving is included). The copy's UTC clock then reads the original **local** time of day at the reference start. Opening the reference copy shows the right time of day with no time-zone arithmetic; for other recordings, see the per-recording warning below.
  - **Alternative, "keep UTC time of day":** the shift is D alone, whole days in UTC.
- **Keep the day of the week:** off by default. When on, only **D** is constrained to a multiple of 7: the app picks the nearest new date with the same weekday as the original. **off_ref is never rounded.**
- **Default new date: 2000-01-01.** It is far enough from 1970-01-01 that every shifted time stays positive, which MEF's time convention requires. mef3io refuses a shift that would break it.

**Preview:** a table and a timeline show, for every recording, its original
start → new start. The app warns:
- when aligned mode makes recordings overlap;
- **per recording, when its UTC offset at its start differs from the reference's** (relative mode with "keep local time of day"). One shift serves every recording, and off_ref is the reference's offset, so a recording made in the other daylight-saving season shows a time of day **one hour off** in the copy. The warning says by how much, and also says when that hour moves the recording across midnight, so its **weekday** (and date) in the copy flips too;
- when a recording crosses a daylight-saving change. Elapsed time always stays exact, so after the copy is shifted, the part of the recording after the change is an hour away from what the wall clock read at the time.

The report repeats these notes per recording.

**Leak-scan safety:** the app refuses a shift whose size is smaller than the
time span of the batch's originals plus one day. With a smaller shift,
shifted times would fall inside the original time range, and the leak scan
could not tell a leaked original date from a legitimate shifted one. In
aligned mode, where every recording gets its own shift, the floor applies to
**each recording's own shift**.

## Step 3: identity

- **Pseudonym:** per recording, or one per subject; typed by the user or generated (`SUBJ-001`, ...).
- **Fields to clear:** subject name and ID, recording location, the session, channel and reference descriptions. All on by default.
- **Free text:** annotations and descriptions can contain names or dates, which cannot be removed automatically. A review screen lists every text item; each one can be **dropped** (the default) or **kept**.
- **Channel names** are kept (they're electrode labels); the user can rename them if needed.

## Step 4: output

- **Output folder and naming pattern,** e.g. `{pseudonym}_{index}.mefd`. The original folder name is never reused, since it often contains a patient ID.
- **Encryption of the copy:** none (default, the usual case for sharing), or new passwords.
- **Key file** (optional, off by default): maps each original to its pseudonym and shift, which makes the anonymization reversible. The app warns that the key file re-identifies the data and must be stored separately and securely.

## Step 5: run

- **Recordings run in sequence or a few in parallel,** limited by disk speed. Anonymizing costs about the same as copying (one read and one write, with no decompression), **plus verification**: it re-reads the output, and the signal comparison reads both the original and the copy.
- **Progress** is shown per recording and overall; **cancel** at any time.
- **A cancelled or failed recording leaves nothing behind.** mef3io writes to a temporary location and renames only after verification.

## Step 6: report

For every recording:
- **result:** done / failed with reason;
- **verification:**
  - samples exactly the same (the compressed signal is byte-identical to the original's);
  - every time shifted by exactly the planned amount, except the time-zone and daylight-saving fields, which are cleared rather than shifted (`GMT_offset = 0`, DST fields = NO_ENTRY, `recording_time_offset = 0`);
  - **leak scan clean**: none of the recording's original time values remain in the copy. Every **time-valued field** is checked, one field at a time, never by sliding over raw bytes. That covers the file headers, metadata sections, index entries, block headers and annotation records, including the times inside annotation bodies. Fields that hold sizes, counts or sample values are never combined into a would-be time value. Data-derived bytes, meaning the compressed signal and each block's statistics table, are checked **byte-identical to the original's** (after decryption, where encrypted) instead of scanned, since they look like random bytes and a scan would report false hits;
  - text the user chose to keep (notes, descriptions) was shown for review first and stays the user's responsibility;
  - integrity check passed.

The report can be saved as PDF or CSV. It contains no patient details and no
original dates, unless the user also chose to write the key file, which is
saved separately.

## What changes in the files

Defined and verified in mef3io: see the field-by-field list in
[mef3io#70](https://github.com/bnelair/mef3io/issues/70) and the
format specification ([mef3io#65](https://github.com/bnelair/mef3io/issues/65)).
In short:
- every timestamp in every file, including those inside annotation records, shifted;
- the time-zone fields cleared, not shifted: `GMT_offset = 0`, `DST_start_time` / `DST_end_time` = NO_ENTRY, `recording_time_offset = 0`;
- the subject fields;
- the session name, which is stored in every file;
- file identifiers that would link the copy back to the original;
- free text, per the user's choices;
- folder names.

## Acceptance

- **Relative mode:** for every pair of recordings, the time between them is identical before and after.
- **Aligned mode:** every recording starts at the chosen instant.
- **Every copy passes `mef3io.anonymize.verify`,** including the leak scan, on all three platforms in CI.
- **The leak scan reports zero false positives** on a multi-GB copy, and still catches a leak planted in any structured field.
- **No identity string or original date** appears in the app's log, the report, or anything written apart from the explicit key file.

## As built (wizard for 0.1.0.dev2)

The wizard (`src/mef3io_studio/anonymizer/`) implements the steps above as
**Recordings → Channels → Dates → Identity → Passwords → Output → Review →
Make the copies**, then a result page, plus a **Privacy check** card in Explore. It
runs on mef3io 1.4.0 (pinned): `anonymize_session`, `anonymize.check`,
refusal codes and the report's original times. Nothing is written before
"Make the copies".

- **Recordings** come from the Explore list (no second open). A recording is
  untickable, with the reason in plain words, when `inspect()` marks it as
  blocking anonymization (`SummaryWarning.blocks_anonymize`), when it was
  opened with the basic password only, or when the planner blocks it. The
  list is a lazy model (texts worked out when painted, the reason cached per
  recording) and is rebuilt once, a moment after the Explore list stops
  changing, and only while the step is on screen: 2000 recordings rebuild in
  well under a second (test). Every later step re-checks that the chosen
  recordings are still listed with their details; one that was removed or is
  being read again is taken out, and the wizard goes back to say so.
- **Channels** (`model/channels.py`, `anonymizer/picker.py`), per the
  256-channel comment on #5: all kept by default; groups by electrode
  (recorder labels `EEG ` / `POL ` and references `-Ref` / `-AVG` stripped,
  a bipolar `LAH1-LAH2` grouped by its first contact, the trailing number
  stripped, case ignored; prefixes with one channel pooled into "Other
  channels"), tri-state group boxes, folded when there are more than 64
  channels; Shift+click ranges over the rows ON SCREEN only (never a folded
  group or a filtered-out channel); Ctrl/Cmd+click highlights and Space
  toggles; a filter with "Exclude N shown" / "Keep N shown", and while it is
  on a group box acts on the group's shown channels only; Keep all / Exclude
  all / "Swap kept and left out"; undo/redo; "N channels changed" after
  every change; "Keeping N of M channels" always shown, Continue disabled at
  0 or when any recording of a batch would keep none. In a batch the choice
  is by name; a name missing from some recordings is marked ⚠ and listed.
  Changing the batch carries the left-out names over and says so (undo
  starts again). Each recording gets
  `exclude_channels=` with the excluded names it actually has. Bulk actions
  repaint instead of signalling every row, so each stays well under 50 ms on
  256 channels (a test pins < 250 ms including the repaint).
- **Dates** use the planner (`model/shiftplan.py`). Its `TIMES_UNVERIFIED`
  blocker is narrowed to what mef3io#70 still refuses: parts that disagree on
  the recording-time offset, an offset that could not be read (not locked),
  and a time at or before a non-zero offset (`time_offset.ambiguous`). A
  non-zero offset that resolves is planned: mef3io reads the true times and
  the copy has none. The batch span (and so the leak-scan floor and the
  earliest-time check) now covers notes as well as samples, from
  `inspect()`'s `first_time` / `last_time`, the span mef3io measures, so a
  plan called runnable is not refused by mef3io because a note lies outside
  the signal.
- **Identity:** a name (default `SUBJ-001`, …) and a pseudonym per recording
  (it follows the name until edited); what to clear (all by default); note
  text "remove" (default) or "keep", with the plain warning that dates and
  other people's names in free text are not found. Names are checked
  locally (empty, characters a folder name cannot hold, duplicates) and then
  **by mef3io itself** before leaving the step, so a refusal ("the name of
  the copy contains the patient's name…") appears where the name is typed.
  `model.anonymize.check_names` calls `mef3io.anonymize.check()`, which
  runs every check `anonymize_session` makes before writing, and writes
  nothing. The destination is not chosen yet, so the name sits in a folder
  that does not exist and refusals about the folder are ignored there; the
  batch's up-front check (`check_copies`) uses the real destination. The
  copy's new passwords are never passed (the original's full-access password
  is, to open it). The name table is locked while mef3io checks, and a
  verdict is used only for the names it was asked about. (Development builds
  before 1.4 probed with a throw-away copy; their leftover folders are swept
  once at start.)
- **Passwords:** none (default) or a basic + full-access pair, typed twice,
  which must differ. A plain warning when other programs may not accept a
  password (`mef3io.password_interop_issue`: longer than 15 characters or 15
  UTF-8 bytes, or a character above U+00FF). The form is cleared when the copies start, when
  the wizard restarts and whenever the page is hidden; if that happens after
  the passwords step, the wizard goes back to it and says why. During and
  after the run only the copies' full-access password is kept, for "Open the
  copy".
- **Review:** output folder; per recording the copy's name and pseudonym,
  kept/excluded channels (excluded listed by name), original → new start and
  end, the shift in plain words, the destination, estimated size (the kept
  channels' files), free space. Start is refused when a name is taken, an
  unfinished `<copy>.partial` is in the way, the folder is inside any
  recording of the batch or any `.mefd` folder, or space is short. Names
  Windows reserves (CON, NUL, COM1, …) are refused on the identity step.
- **Run:** one task, copies in sequence, each in a helper process with
  mef3io's byte progress and Stop (`ctx.run_isolated(...,
  stop_grace=STOP_GRACE_SECONDS)`). A refusal or failure ends that copy
  only. mef3io's refusals are translated by their stable `.code`
  (`model/refusals.py` has words for every code in
  `mef3io.anonymize.REFUSAL_CODES`; a test checks none is missing), never by
  their message; without a code, by kind of error (a folder that cannot be
  written, damaged data, a risk of a wrong date). Its text is kept under
  "Technical details" without paths, without the recording's name (also
  inside file names and a single file's inner folder) and passed through the
  log filter's `scrub()` (dates, raw times). `VerifyReport.notes` and the
  report's notes are listed under "Technical details" with a plain summary.
  On Stop, mef3io removes `<copy>.partial`; the task then checks it is really
  gone (removing it if the helper had to be ended after the grace) and, if it
  is still there, the result says so plainly and names the folder.
- **Result:** original → new start and end per recording and the shift in
  words; "Open the copy" opens it in Explore on its Privacy check (with the
  new full-access password, memory only). **Date key:** shown on screen;
  The original start and end shown and saved are mef3io's own record
  (`AnonymizeReport.source_start_us` / `source_end_us`, kept in memory only),
  else what was read before the batch.
  "Save the date key…" asks first, then writes a CSV with the columns
  original recording, anonymized copy (its name; the pseudonym is not a
  column), original start and end, new start and end (UTC, ISO 8601) and the
  shift in microseconds, through a temporary file to a path the user picks
  (the dialog starts in the home folder), readable by its owner only on
  POSIX. A path inside the output folder, a copy or a recording is refused.
  Never logged, never in settings, never in the copy (`tests/test_privacy.py`,
  `tests/test_anonymizer_ui.py`). This replaces the "key file" of step 4
  above, which also listed the pseudonym.
- **Privacy check card** (`model/privacycheck.py`, `explorer/privacy.py`):
  red headline when something is found; amber "Some details could not be
  checked: open it with the full-access password" when nothing was found
  but an identity row (names, location, descriptions, note text, time zone,
  date-hiding offset) could not be read; green only when every one was
  checked and empty. Values masked until Show (shared with Patient
  details), a ✓/✗/– checklist, "Made by the anonymizer" when every part's
  history has `anonymize` (code 6) as its LAST operation (the set of
  operations ever done is inherited from the source, and an append to a copy
  leaves "anonymize" in it), "changed afterwards, so we can't confirm" when
  some parts were anonymized and later changed, otherwise "This recording
  wasn't made by the anonymizer, so we can't confirm its dates were moved.",
  and the
  start/end labelled as moved dates. Kept note text on an anonymizer copy is
  "!" ("Kept, as chosen"), not a found detail. Other programs' private areas
  cannot be seen through `inspect()`: ✓ only for an anonymizer copy (mef3io
  clears them), "–" otherwise. Built from the details the Explorer already
  read; the 1%-of-bytes test now builds the card in the measured helper.

### The unattended batch (pipeline)

The issue #5 comment "one unattended pipeline from selection to shareable
archives" (2026-10-16) is built as follows (review rounds 1 and 2 included).

- **Output step** (before Review): folders (as before), one archive per
  recording, or one archive for the whole batch (`model/pipeline.py`).
- **Per recording, in sequence** (`workers.tasks.run_batch` → `_one`):
  anonymize (helper process) into `<work>/work-<batch>-<n>/<name>.mefd`.
  - **Archives:** the SHA-256 of every file of the checked work copy is
    computed first; `model.archive.plan` + `run_in_helper` (mef3io
    `archive_session` to a temporary `.mefd.tar` next to the target, then
    `inspect` of the archive vs the work copy: channels, rates, every part's
    start, end, samples and blocks); then every member of the written,
    fsynced archive is re-read and its SHA-256 compared with the copy's;
    only then `finish` (rename, never replacing).
  - **Bundle:** `model.bundle.BundleWriter.add` (Python `tarfile`,
    deterministic: sorted members, mtime 0, uid/gid 0, no user names,
    0755/0644, pax header only where a name does not fit ustar; 1 MB chunks,
    never a whole signal file). Each file is hashed while it is read into the
    bundle; after the fsync the added members are re-read from the bundle and
    their names, sizes and SHA-256 compared.
  - What the byte check can and cannot see: the re-read right after the
    fsync usually comes from the operating system's cache, not from the
    disk. It catches the app's own mistakes and errors while writing (a
    short write, a wrong member, a failed write the OS reported), not
    corruption that happens later, or on the far side of a network drive.
  - Only after that byte check is the work copy deleted. A mismatch keeps the
    work copy (the user is told; the start-up offer removes it later) and
    reports the recording failed ("The archive did not match the checked
    copy"). Folders mode is unchanged (mef3io writes `<dest>.partial`,
    verifies and publishes).
- **Up-front checks** (`preflight_batch`, before anything is written): every
  original exists and opens with ITS OWN password with full access (each
  recording keeps the password it was opened with in Explore; originals may
  have different ones), the work folder is writable and outside any
  recording, nothing is in the way at the destination, free space (the
  destination for all estimated copies; the work folder for the largest; on
  one drive, the total plus the largest), and mef3io's name refusal. Problems
  are listed on the Review step and nothing starts.
- **Failures:** a refusal or a failure ends one recording. A failure to
  reach the source or the destination (an `OSError` / `IoError` that is not a
  refusal) is RETRYABLE: the recording stays waiting in the journal; after 3
  such failures in a row the batch pauses, saying plainly that the network
  drive may be unavailable, and offers "Retry the failed ones" (the batch's
  passwords stay in memory only while something can be retried).
- **Stop:** each step cleans up (mef3io removes `.partial`; a temporary
  archive is removed; a half-added bundle recording is cut back to the last
  checked offset). With one archive for the batch, Stop (or a pause) keeps
  the unfinished archive and the journal, so Continue can complete it; "Finish
  the archive with what is done" names it and reports it as partial ("3 of
  the 7 recordings; 4 are not in it"), in the summary and the notification.
  Rows say "Added to the batch archive" until the archive is finished, and
  "Not delivered" (left out of the date key) if finishing fails.
- **Keep awake** (`model/awake.py`, on by default, a preference): Windows
  `SetThreadExecutionState`; macOS `caffeinate -i -w <pid>`; Linux
  `systemd-inhibit --what=sleep:idle … tail --pid=<pid> -f /dev/null`, so the
  inhibitor ends with the app even if it crashes; a quiet no-op elsewhere.
  **Notification** when the batch ends: the system tray message (the tray
  icon is hidden again after it) plus a taskbar/dock alert.
- **Journal** (`model/journal.py`): in the app-data folder, one JSON per
  batch with ONLY opaque ids in clear (batch id, item ids, states); the rest
  (sources, destinations, names of the copies, shifts, channels, temporary
  files, the bundle's temporary file and its last checked offset) is a sealed
  blob: a SHA-256 keystream and HMAC-SHA-256 under separate keys derived as
  `HMAC(key, "enc")` and `HMAC(key, "mac")` from a random key in
  `journal.key` (DPAPI-protected on Windows, 0600 elsewhere). The key is
  created only when there is none; a key that cannot be read fails closed
  (`KeyUnavailable`: nothing is sealed, unsealed or removed, and the user is
  told) instead of being replaced, which would orphan every journal, the
  work-folder setting and the list of temporary files. It holds NO
  original names or dates: on Continue they are read again with `inspect()`
  for the date key. Passwords are never journaled. Every temporary file at the
  destination is journaled (and kept in a sealed list, `temporaries.json`)
  BEFORE it is made. On start the app offers "Continue the batch: 4 of 7
  done": Continue asks each original's password (shown with its folder name,
  on the user's screen only; originals sharing a password can be given it
  once) and the copies' new pair (typed twice) again, opens every original with its own password and one
  finished copy with the new one, removes its own unfinished files of the
  recordings still to do, asks whether results already at the destination for
  unfinished recordings (made just before a crash) are kept ("not confirmed")
  or made again (only when the result is provably this batch's: the
  anonymizer stamp as its last operation, the copy's name stored inside, and
  a start equal to the original's plus the shift, exactly the original's
  channels minus those left out, and protected exactly as asked (opens with
  the copies' full-access password and not without, or unprotected when none
  were set), read with `inspect` for a folder or an archive; anything else,
  even another anonymized copy of the same original, is left as it is, with
  a plain message), then continues. A Stop that arrives after a folder with
  the copy's name appeared credits it only if the same check says it is this
  copy; otherwise the recording is reported as "A folder with the copy's name
  appeared" and the folder is left alone.
  Refusals on Continue name positions
  ("Recording 3 of 7"), never recordings. Discard removes the batch's
  unfinished files. Leftover work copies in every work folder ever used (a
  sealed preference) and known temporaries not claimed by a journal are
  offered for removal ("They should hold no patient details, but they were
  never checked: do not use or share them"). Work copies kept because their
  archive did not match (sealed list `kept.json`) are checked copies and get
  their own question. A full drive (`ENOSPC`, `EDQUOT`) is reported as "the
  destination or the work folder is full", never as a file that could not be
  reached, and is not retried.
- **Settings** (`common/settings_dialog.py`): the work folder (default
  `<app data>/work`; stored SEALED with the same key, never a path in clear),
  and keep-awake.
- **The page is built on first use:** the wizard's few hundred widgets are
  made the first time the Anonymize page is shown or used. Built up front,
  they slowed other pages' task starts enough to fail the Explorer's 0.25 s
  responsiveness test under load.

**Deferred** (not in dev2): the per-item free-text review screen (one
drop/keep choice for all text today); pseudonym generation per subject and a
naming pattern; running copies in parallel; a PDF/CSV report export; the
"set the time too" weekday uses the site zone when one is chosen.
