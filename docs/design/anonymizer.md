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
- **Keep the time of day** (default): only the date changes. The shift is a whole number of days, so every recording keeps its clock time.
- **Set the time too:** the time of day is anonymized as well.

**Mode:**

| Mode | What happens | Use when |
|---|---|---|
| **Relative** (default) | Every recording is shifted by **the same amount** as the reference. Order, gaps and overlaps between recordings are preserved exactly. | Recordings of one subject that must still follow each other |
| **Aligned** | **Every** recording is moved to start at the same chosen moment. | Making recordings anonymous *and* comparable from a common start |

**Options:**
- **Time zone:** keep the local time of day as it appeared at the recording site (default), or keep the UTC time of day. MEF stores UTC plus the site's offset; "keep local" writes the offset that applied at the start.
- **Keep the day of the week:** off by default. When on, the shift is rounded to a whole number of weeks.
- **Default new date: 2000-01-01.** It is far enough from 1970-01-01 that every shifted time stays positive, which MEF's time convention requires. mef3io refuses a shift that would break it.

**Preview:** a table and a timeline show, for every recording, its original
start → new start. The app warns when aligned mode makes recordings overlap,
and when a recording crosses a daylight-saving change (its later part would be
an hour off the local wall clock, because elapsed time must stay exact).

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
  - every time shifted by exactly the planned amount;
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
- every timestamp in every file, including those inside annotation records;
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
