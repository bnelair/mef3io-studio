# User guide

## Explore recordings

The **Explore** page shows what is in your recordings: channels, when they
start and end, gaps, notes and events, and more. **Opening a recording never
changes it, and its signal is not loaded**, so even very large recordings
open quickly.

![The Explore page: a list of recordings sorted by start time, and the details of the selected one.](img/screenshots/explorer-overview-light.png)

### Opening recordings

A recording is a folder ending in `.mefd`, or a single file ending in
`.mefd.tar`. There are three ways to open one:

- **Open a recording** (top left, or File → Open): pick a recording folder or
  a single-file recording.
- **Drag and drop** recordings onto the page. If you drop a folder that is not
  a recording, the app looks inside it for recordings.
- **Scan a folder**: finds every recording in a folder and all its subfolders.
  This is the quickest way to sort out a folder full of recordings with
  random-looking names.

Recordings open in the background. Each task appears in the Activity panel
with a progress bar and a **Stop task** button; the window stays usable.

**Password-protected recordings** ask for their password when you open them
on their own. When they are found by a scan, the list says **Password needed**;
select the recording and choose **Enter the password…**.

- The **full-access password** opens everything, including the patient
  details.
- The **basic password** opens everything except the patient details.

Passwords are kept only while the app is open. They are never saved and never
written to the app's log. Note that MEF passwords don't hide the signal, the
recording's name or its timing.

### The list of recordings

Each row shows the recording's name, start and end, duration, number of
channels, sampling rate, size on disk, whether it is password-protected, and
the result of the last check for problems.

- The list is **sorted by start time**. Click any column heading to sort by
  that column instead; click again to reverse the order.
- Select several rows with Ctrl (⌘ on a Mac) or Shift.
- **Remove from the list** only removes rows from the list. Nothing is
  deleted.

### Times and time zones

Recordings store every time in UTC, and the app shows UTC until you choose
the time zone of the place where the recordings were made: **View → Time
zone…** (or **Change…** at the top right). After that, every time is shown
in that place's local time, with its offset from UTC, correct on both sides
of a daylight-saving change. The choice is remembered.

Times marked **(unverified)** may not be the real ones: the part of the
recording that confirms them is locked until the full-access password is
given. They are listed after the others when you sort by time.

Some recordings carry a **time offset** that older software used to hide the
real date. The app shows a warning for these: *"This recording hides its real
date in a way that can be undone; treat it as identifiable."* The real date is
still in the file, so such a recording is **not** anonymized. The times shown
are the true ones.

### The details of a recording

Select a recording to see its details in the tabs below the list:

| Tab | What it shows |
|---|---|
| **Overview** | Start, end, duration, channels, sampling rates, parts, gaps, size and password protection, plus any warnings |
| **Channels** | Each channel's sampling rate, units, number of samples and parts, and first and last sample time |
| **Timeline** | Each channel as a bar over time. Filled: recorded. Empty: a gap. Striped: a part that has gaps inside it. Point at a part to see its times |
| **Notes and events** | Notes stored for the whole recording, with each channel, and with each part. Their text is hidden until you choose Show under Patient details |
| **Patient details** | Name, patient ID, location and descriptions, **hidden until you choose Show** |
| **History** | Which version of the mef3io library created the files and last changed them, and what was done (for example "added to"). No dates are recorded. Files written by other programs have no history |
| **Check for problems** | Compares what the recording says about itself with its data. Choose **Check now**; the results are explained in plain words, with the technical details one click away. Checking never changes the recording |

![The timeline of a recording with a gap between its two parts.](img/screenshots/explorer-timeline-light.png)

### Patient details stay hidden

Patient details, descriptions and the text of notes are hidden until you
choose **Show** on the Patient details tab. That choice applies to that one
recording, only until you close the app. Patient details never go into the
app's log.

### Copy summary

**Copy summary** copies a short description of the selected recordings, as
plain text or as JSON (for scripts and data-management logs). Names and
dates are replaced by `[hidden]` unless you chose Show for that recording; in
that case the app warns you before copying.

### Pack into one file, or unpack

**Pack into one file…** turns a recording folder into a single `.mefd.tar`
file, which is easier to copy and send. **Unpack into a folder…** does the
opposite. You choose where the new copy goes:

- the original is never changed;
- nothing that already exists is ever replaced: if the name is taken, the app
  says so and writes nothing;
- the new copy is checked against the original before it gets its final
  name, and it is added to the list.

If you stop the task, the unfinished copy is removed.
