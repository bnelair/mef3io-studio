# Vision and scope

## Why this exists

MEF 3.0 recordings are opened mostly by code: Python or MATLAB scripts built on
[mef3io](https://github.com/bnelair/mef3io). Many of the people who handle
these recordings don't write code: clinicians, research coordinators, students
and data managers. Their questions are practical:

- *What's in this recording?* Channels, how long it is, when it starts, whether there are gaps, whether it's encrypted, whether it's intact.
- *Which of these folders with random-code names is which?* Order them by time, and give them meaningful names without breaking anything.
- *Can I share it?* Remove the patient details and the real dates, consistently across every recording of a subject, without breaking the files.
- *Can I look at it?* Later: scroll through the signal and its annotations.

mef3io-studio answers those questions in a desktop app that runs on Windows,
macOS and Linux.

## Who it is for

- **Students and researchers new to MEF.** They need to see what a recording contains before writing any code.
- **Clinical and data-management staff** preparing recordings for sharing or for research use.
- **Developers.** A quick visual check of a session, without writing a script.

Everything on screen is in plain language. Technical detail (offsets, block
counts, CRCs) is available, but never in the way.

## First release (v0.1)

1. **Explorer:** open recordings and inspect them; no signal is loaded. It also **orders sessions by time and renames them safely**: one mef3io operation changes the folder, the session's annotation files and the names stored inside it together. It also packs a recording into a single file and back. See [design/explorer.md](design/explorer.md) and [#12](https://github.com/bnelair/mef3io-studio/issues/12).
2. **Metadata view and edit:** subject, location and descriptions, single or batch, in place with undo or into a copy ([#14](https://github.com/bnelair/mef3io-studio/issues/14)). **Timestamps are never editable.**
3. **Passwords:** save copies with new passwords or without any ([#15](https://github.com/bnelair/mef3io-studio/issues/15)).
4. **Channel selection and batch conversion:** reusable channel presets applied to many sessions, combined with the other copy options in one pass ([#16](https://github.com/bnelair/mef3io-studio/issues/16)).
5. **Anonymizer:** batch de-identification into verified copies, with the time shift and the channels chosen by the user, and unattended batches into shareable archives. See [design/anonymizer.md](design/anonymizer.md).
6. **Installers** for Windows (`.exe`), macOS (Apple silicon and Intel) and Linux, on GitHub Releases. The app is **not** published to PyPI.

**Where it stands:** test version 0.1.0.dev3 has the installers, the
Explorer without ordering and renaming, and the Anonymizer. Items 2-4 and
ordering and renaming are still to come; see the [roadmap](roadmap.md).

## Next directions (need their own specifications)

- Saving a part of a recording, cutting it into pieces, and merging recordings, always into new checked copies ([#31](https://github.com/bnelair/mef3io-studio/issues/31)).
- A signal viewer, built on mef3io-server.
- A data coverage view: when data exists and at what rate, per channel.
- Annotation viewing, then editing.
- Export to other formats.
- Opening recordings served remotely by [mef3io-server](https://github.com/bnelair/mef3io-server).

See [design/next-directions.md](design/next-directions.md).

## Out of scope

- **Analysis:** filtering, spectra, detection and the like belong in analysis toolboxes, not here.
- **Video channels:** MEF 3.0 video (`.vidd`) is out of scope in mef3io too.
- **Recording or acquiring data.**
- **Network features** of any kind in v0.1: the app makes no connections.

## Principles

1. **The app never touches the format itself.** Every read, write and anonymization goes through mef3io, where it is tested against the reference implementations. If the app needs something mef3io cannot do, it gets added to mef3io first.
2. **Originals change only when the user explicitly says so.**
   - **Content changes always write a verified copy:** anonymization, encryption changes, channel subsets. These never touch the original.
   - **In place only on request:** renaming (which updates the stored name too) and editing metadata. Each is an explicit operation with a preview, a confirmation, and saved information so it can be undone, unless the user turns undo off. Copying a multi-hundred-GB recording just to rename it is not practical.
   - **Timestamps are never edited in place.** The only way to change recording times is a new verified copy (the anonymizer's time shift).
   - **Nothing ever changes an original as a side effect.**
3. **Never load a whole recording.** Recordings run to terabytes; the app reads only what it shows.
4. **Private by default.**
   - Patient details are hidden until asked for.
   - Logs carry no patient data, no recording names or paths, and no original dates.
   - Undo information lives in the app's own data folder, never beside the recordings.
   - There is no network access.
5. **Plain language first.** Technical detail is there, but never the first thing a user sees.
6. **Modern and never frozen.**
   - Every long task runs in the background, with a progress bar and Stop. The window never freezes, even while converting a multi-GB recording.
   - The interface looks current (light and dark themes, crisp on high-DPI screens) and scales from a laptop to a large monitor.
