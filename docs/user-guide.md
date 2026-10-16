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
- Numbers (duration, channels, sampling rate, size) line up on the right
  under their headings; names and times on the left.

### Times and time zones

Recordings store every time in UTC, and the app shows UTC until you choose
the time zone of the place where the recordings were made: **View → Time
zone…** (or **Change…** at the top right). After that, every time is shown
in that place's local time, with its offset from UTC, correct on both sides
of a daylight-saving change. The choice is remembered.

Times marked **(unverified)** may not be the real ones. They are listed
after the others when you sort by time. This happens in two cases:

- the part of the recording that confirms the times is locked until the
  full-access password is given;
- the recording carries a **time offset** that older software used to hide the
  real date. The app shows a warning for these: *"This recording hides its real
  date in a way that can be undone; treat it as identifiable."* The real date
  is still in the file, so such a recording is **not** anonymized. Older
  programs stored such times in two different ways, and the file does not say
  which, so the times shown (as the mef3io library works them out) are
  unverified.

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

### Several recordings on one timeline

Choose **Combined timeline** (next to **Details**, above the tabs) to see
several recordings together, **one row per recording**, on one shared time
line:

- **Which recordings:** the ones selected in the list when you select two or
  more (Ctrl or Shift); otherwise every recording in the list.
- **Rows are in time order.** Recordings whose times are unverified come
  after the others, and recordings with nothing to draw (still being read,
  locked, or not readable) come last, each with a line saying why.
- **What the bars mean:**
  - **dark:** every channel of the recording has data at that time;
  - **light:** only some of its channels do;
  - **striped:** a part with gaps inside it (where exactly is not known
    without reading more of the recording);
  - **a thin line** between bars: a gap inside the recording;
  - **red:** time that more than one recording covers (a band under the time
    scale, and a line under each recording concerned);
  - **grey and dotted, labelled "unverified time":** the times may not be the
    real ones (see *Times and time zones*). These are drawn so you can see
    them, but they are **not counted** in the figures and never flagged as
    overlapping.
- **The figures beside it** are worked out across the recordings with
  verified times: first and last time, the time from first to last, the time
  with data, and the gaps between recordings.
- **Overlaps are flagged plainly.** Two recordings that cover the same time
  have been made at the same time (for example on two beds), or they may be
  copies of each other, for example a folder and its single-file copy. When nearly all
  of the shorter one is shared and the channels are the same, the warning
  says they may be copies. Where two recordings have the same name, the
  timeline shows their file names (with `.mefd` or `.mefd.tar`) to tell them
  apart. When very many recordings share time, they are counted but not
  listed in pairs.
- **Zoom and move:** turn the mouse wheel over the bars to zoom in or out
  around the pointer, drag to move along the time line, and use **Fit all**
  (or the Home key) to see everything again. Over the names, the mouse wheel
  scrolls through the rows when they don't all fit.
- **Point at a bar** to see the stretch's start, end and length, and how many
  of the recording's channels have data there. **Click a row** to select that
  recording in the list; **double-click** it to open its details.

Times are shown in the chosen time zone, with the offset, like everywhere
else. Like the rest of the page, the timeline is drawn from what the app has
already read: no signal is loaded. Patient details are not part of this view.

![Six recordings on one timeline: two overlap, one has only some channels in its second part, one has gaps inside a part, one has unverified times and one is locked.](img/screenshots/explorer-combined-light.png)

### Save times as a table

**Save times as a table** (on the combined timeline) writes the start and end
times of those recordings to a CSV file that opens in Excel, LibreOffice and
other spreadsheet programs. Right-click the list for **Save their times as a
table (CSV)…** to save just the selected recordings. Two layouts:

- **One row per recording** (the usual choice): name, file name, start, end,
  duration, channels, sampling rates, number of parts, time with data, gaps
  between parts, parts with gaps inside them, and which other recordings in
  the table cover the same time, and for how long.
- **One row per part of each channel:** recording, channel, part number,
  start, end, duration, samples, sampling rate, and whether the part has gaps
  inside it.

Every start and end is written both for **people** and for **machines**:

- **Readable:** `2000-01-06 07:00:00` in UTC. Spreadsheet programs recognise
  this as a date and time, so you can sort and calculate with it. When you
  have chosen a site time zone, a second readable column gives the time on
  the site's clock (for example `2000-01-06 08:00:00`); the
  `site_time_zone` column says which zone.
- **ISO 8601:** `2000-01-06T07:00:00Z` in UTC, and with a site time zone
  also the site time with its offset (`2000-01-06T08:00:00+01:00`).
- **Unix time in seconds:** `947142000`, with decimals down to the
  microsecond when needed, as most programming languages and databases
  expect.
- **Microseconds since 1970 in UTC:** a whole number that converts back
  exactly in scripts. (Spreadsheet programs keep only 15 digits of a number
  and round these; import that column as text to keep it exact.)

The column names say which is which, for example `start_utc_readable`,
`start_utc`, `start_unix_seconds` and `start_microseconds_utc`.

- **Before saving, the app warns you:** *"This file will contain the dates of
  the recordings, which can identify people. Store it like the recordings
  themselves."*
- **Patient details** are included only for recordings where you chose
  **Show**; for the others the columns read `[hidden]`.
- **Unverified times** are marked in the `times_verified` and `time_note`
  columns and never counted as overlapping. Recordings that are locked or
  could not be opened get a row saying so, without times.
- The file is written under a temporary name and only takes its final name
  when it is complete; stopping the task leaves nothing behind. The save
  dialog asks before replacing a file.
- Text cells that a spreadsheet would treat as a formula (starting with `=`,
  `+`, `-` or `@`, for example a strange file name) get a `'` in front, so
  opening the table never runs anything.

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
