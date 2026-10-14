# UI language: everyday words first

mef3io-studio is used by students, clinicians, coordinators and data
managers, not only by programmers. Everything the app shows uses everyday
words. Technical terms are allowed **only** behind a "Technical details"
toggle (`common/dialogs.py: TechnicalDetails`), in the log file, and in the
docs for developers.

`tests/test_ui_language.py` scans the user-facing strings for the banned
terms below and fails the build when one slips in. Wrap a string in
`technical("...")` (from `mef3io_studio.common.text`) only when it is
shown inside a technical-details panel.

## Glossary

| Technical term (don't show) | Say instead |
|---|---|
| session, `.mefd` folder | **recording** |
| channel | channel (people who use the app know this word) |
| segment | **part of the recording** |
| metadata, header, section 2/3 | **details** (e.g. "recording details", "patient details") |
| subject fields | **patient details** |
| encryption, encrypted, decrypt | **password protection**, "password-protected", "remove the password" |
| level-1 (L1) password | **basic password** |
| level-2 (L2) password | **full-access password** |
| provenance | **history** ("what has been done to this recording") |
| integrity check, validator, validate | **check for problems** |
| repair | **fix** |
| recover | **rescue** (after an interrupted copy) |
| annotations, records | **notes and events** |
| discontinuity, gap in the index | **gap** |
| uUTC, timestamp, epoch | **time** (always shown as a date and time, or hidden) |
| RED block, CRC, checksum, `.tdat`, `.tmet`, `.tidx` | never in the main UI |
| exception, traceback, error code | never in the main UI; the message says what happened |
| cancel (a job) | **stop** |
| job, worker, thread | **task** |

## How to write a message

Every message, especially an error, answers three questions in this order:

1. **What happened**: "The recording could not be opened."
2. **What it means for your data**: "Nothing was changed." /
   "The unfinished copy was removed; the original is untouched."
3. **What to do next**: "Check that the drive is connected, then try again."

Keep sentences short. Address the user as "you". Never blame the user.
Put file names, error codes and library messages under "Technical details".
