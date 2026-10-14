# Security Policy

## Reporting a vulnerability

Please report security and privacy issues privately, **not** as a public
GitHub issue. Use GitHub's
[private vulnerability reporting](https://github.com/bnelair/mef3io-studio/security/advisories/new)
(Security → Report a vulnerability), or contact the maintainers directly.

Useful details: app version, platform, steps to reproduce, and the impact you
observed. We aim to acknowledge reports within a week.

## Scope

This app handles clinical recordings and produces anonymized copies of them,
so **privacy failures are security bugs**. In scope:

- **An anonymized copy that still contains** patient details, an original date or time, or anything that links it back to the original. Includes anything the verification step failed to catch.
- **Identifying information where it must not be.** Patient details, original recording dates, recording names or folder paths showing up in logs, reports, crash dumps, settings and preferences, temporary files left behind, or anything else the app writes on its own. Preferences hold no paths: there is one site time zone per app profile, and a per-window override that is never saved. Only two kinds of artifact the app writes may contain such information, both explicit and both warned about in the UI:
  - the anonymizer's **key file** (written only on request);
  - **recovery artifacts**: the **crash journal** (old names; mandatory while an operation runs, deleted when it completes), byte-level backups for crash rollback (**always** deleted when the operation completes), and optional **undo information**. Undo information holds the old **and new** names or field values, which descriptions or `.tmet` files changed, the post-operation modification times and sizes of every file touched, and each segment's end time and entry counts (the end times are original dates). All of these are stored only in the app's per-user data folder under opaque names, **never beside the recordings**, never copied into logs, reports or exports, and listed and deletable in the app. The app never deletes the journal of an unfinished operation without first finishing or rolling it back.
- **The in-session operation marker identifying anything.** While a multi-step in-place operation runs, mef3io places a small "operation in progress" marker inside the session. It must hold no old names, paths or dates: only the tool, the operation kind and a random ID.
- **User-initiated exports** (e.g. a copy summary with names and dates shown) are not artifacts the app writes on its own: they are the user's explicit act, preceded by a warning. Identifying information in one that the user did **not** choose to show is in scope.
- **An original recording modified** without the user's explicit confirmation, or beyond what was confirmed:
  - Anonymization, encryption changes and channel subsets must never touch the original.
  - Renames (folder, session annotation files and stored names together) and metadata edits change it in place only as confirmed, previewed operations. Their undo is a logical inverse (rename back, or re-apply the old field values to the segments the edit patched), itself a confirmed in-place operation with the same safety checks, asking again for any password it needs, never a restore of saved bytes.
  - **Timestamps are never changed in place,** by any operation.
- **A rename that loses data:** annotations that other MEF software can no longer find after a rename, or a session left half-renamed after a crash. Crash safety must not depend on anything optional: an interrupted operation is always finished or rolled back from the mandatory journal, and a marker whose journal is missing is refused with an explanation, never guessed at. It can be cleared only by an explicit, confirmed `clear_operation_marker`, and only when mef3io's consistency check shows the session is not half-changed.
- **Any network access** by the app.
- **Crashes or out-of-bounds reads** caused by a crafted file. Those usually belong in [mef3io](https://github.com/bnelair/mef3io/security), which does the parsing.

## Supported versions

Fixes land on the latest release. Please upgrade before reporting.
