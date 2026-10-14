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
- **Identifying information where it must not be.** Patient details, original recording dates, recording names or folder paths showing up in logs, reports, crash dumps, temporary files left behind, or anything else the app writes. Only two kinds of artifact may contain such information, both explicit and both warned about in the UI:
  - the anonymizer's **key file** (written only on request);
  - **recovery artifacts** that make undo possible: rename journals (old names) and header or metadata backups (values before an edit). They are stored in the app's per-user data folder under opaque names, never beside the recordings unless the user opts in, never copied into logs, reports or exports, listed and deletable in the app, and can be switched off.
- **An original recording modified** without the user's explicit confirmation, or beyond what was confirmed:
  - Anonymization, encryption changes and channel subsets must never touch the original.
  - Renames (folder, session annotation files and stored names together) and metadata edits change it in place only as confirmed, previewed operations with undo.
  - **Timestamps are never changed in place,** by any operation.
- **A rename that loses data:** annotations that other MEF software can no longer find after a rename, or a session left half-renamed after a crash.
- **Any network access** by the app.
- **Crashes or out-of-bounds reads** caused by a crafted file. Those usually belong in [mef3io](https://github.com/bnelair/mef3io/security), which does the parsing.

## Supported versions

Fixes land on the latest release. Please upgrade before reporting.
