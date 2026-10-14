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
- **Patient details or original dates** in logs, reports, crash dumps, temporary files left behind, or anything else the app writes. Only **two artifacts** may contain them, both explicit and both warned about in the UI:
  - the anonymizer's **key file** (written only on request);
  - the **rename record** that makes *Undo rename* possible (old names; switchable off; never in logs or reports).
- **An original recording modified** without the user's explicit confirmation, or beyond what was confirmed. Anonymization, encryption changes and channel subsets must never touch the original. Renames, stored-name updates and metadata edits change it in place only as confirmed, previewed operations with a backup or undo.
- **Any network access** by the app.
- **Crashes or out-of-bounds reads** caused by a crafted file. Those usually belong in [mef3io](https://github.com/bnelair/mef3io/security), which does the parsing.

## Supported versions

Fixes land on the latest release. Please upgrade before reporting.
