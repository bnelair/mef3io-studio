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
  - **Default, "keep local time of day":** the shift is **D + off_ref**, where **D** is a whole number of days and **off_ref** is the reference start's UTC offset in the chosen site time zone (at that instant, so daylight saving is included). The copy's UTC clock then reads the original **local** time of day at the reference start. Opening the reference copy shows the right time of day with no time-zone arithmetic; for other recordings, see the per-recording warning below.
  - **Alternative, "keep UTC time of day":** the shift is D alone, whole days in UTC.
- **Keep the day of the week:** off by default. When on, only **D** is constrained to a multiple of 7: the app picks the nearest new date with the same weekday as the original. **off_ref is never rounded.**
- **Default new date: 2000-01-01.** It is far enough from 1970-01-01 that every shifted time stays positive, which MEF's time convention requires. mef3io refuses a shift that would break it.

**Preview:** a table and a timeline show, for every recording, its original
start → new start. The app warns:
- when aligned mode makes recordings overlap;
- **per recording, when its UTC offset at its start differs from the reference's** (relative mode with "keep local time of day"). One shift serves every recording, and off_ref is the reference's offset, so a recording made in the other daylight-saving season shows a time of day **one hour off** in the copy. The warning says by how much;
- when a recording crosses a daylight-saving change. Elapsed time always stays exact, so after the copy is shifted, the part of the recording after the change is an hour away from what the wall clock read at the time.

The report repeats these notes per recording.

**Leak-scan safety:** the app refuses a shift whose size is smaller than the
time span of the batch's originals plus one day. With a smaller shift,
shifted times would fall inside the original time range, and the leak scan
could not tell a leaked original date from a legitimate shifted one.

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

- **Recordings run in sequence or a few in parallel,** limited by disk speed. Anonymizing costs about the same as copying: one read and one write, with no decompression.
- **Progress** is shown per recording and overall; **cancel** at any time.
- **A cancelled or failed recording leaves nothing behind.** mef3io writes to a temporary location and renames only after verification.

## Step 6: report

For every recording:
- **result:** done / failed with reason;
- **verification:**
  - samples bit-identical;
  - every time shifted by exactly the planned amount, except the time-zone and daylight-saving fields, which are cleared rather than shifted (`GMT_offset = 0`, DST fields = NO_ENTRY, `recording_time_offset = 0`);
  - **leak scan clean**: no value from the original time range remains anywhere in the copy;
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
- **No identity string or original date** appears in the app's log, the report, or anything written apart from the explicit key file.
