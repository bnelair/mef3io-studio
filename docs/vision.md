# Vision and scope

## Why this exists

MEF 3.0 recordings are opened mostly by code: Python or MATLAB scripts built on
[mef3io](https://github.com/bnelair/mef3io). Many of the people who handle
these recordings don't write code: clinicians, research coordinators, students
and data managers. Their questions are practical:

- *What's in this recording?* Channels, how long it is, when it starts, whether there are gaps, whether it's encrypted, whether it's intact.
- *Which of these UUID-named folders is which?* Order them by time, and give them meaningful names without breaking anything.
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

1. **Metadata explorer:** open recordings and inspect them. No signal is loaded. It also **orders sessions by time and renames them safely**, keeping the name stored inside the files in step. See [design/explorer.md](design/explorer.md).
2. **Anonymizer:** batch de-identification into verified copies, with the time shift chosen by the user. See [design/anonymizer.md](design/anonymizer.md).
3. **Installers** for Windows, macOS (Apple silicon and Intel) and Linux.

## Next directions (need their own specifications)

- A signal viewer.
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
2. **Originals are never modified.** Anything that writes produces a new copy, and only after verifying it.
3. **Never load a whole recording.** Recordings run to terabytes; the app reads only what it shows.
4. **Private by default.** Patient details are hidden until asked for. Logs carry no patient data and no original dates. There is no network access.
5. **Plain language first.** Technical detail is there, but never the first thing a user sees.
