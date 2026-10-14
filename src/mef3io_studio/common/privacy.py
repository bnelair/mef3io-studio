"""The privacy filter for logging.

Two kinds of text must never reach the log file:

* **identity strings** — patient names, IDs, passwords, anything the app
  learns while it works. Code that comes across one calls
  :func:`register_identity` and from then on it is replaced by
  ``[redacted]`` wherever it appears, in any case;
* **absolute dates and times** — a recording's real date is identifying.
  Anything that looks like a calendar date, a clock time with a date, or a
  raw MEF timestamp (microseconds since 1970) is replaced by ``[time]``.

The filter is attached to every handler the app installs, so it also covers
records from libraries and from exception tracebacks. It redacts rather than
drops a record, so the log stays useful for diagnosing problems.

This file contains no Qt and no knowledge of MEF bytes.
"""

from __future__ import annotations

import logging
import re
import threading

REDACTED = "[redacted]"
TIME = "[time]"

_MONTHS = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)

# Ordered most specific first, so a full ISO datetime becomes one [time].
_TIME_PATTERNS = [
    # ISO 8601 / RFC 3339 dates with optional time and zone: 2019-03-14,
    # 2019-03-14T09:26:53.589Z, 2019/03/14 09:26
    r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{4}\.\d{2}\.\d{2})"
    r"(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?"
    r"(?:\s?(?:Z|[+-]\d{2}:?\d{2}))?)?",
    # Day-first or month-first numeric dates: 14/03/2019, 3-14-19, 14.03.2019
    # (dotted forms need a 4-digit year, so "3.12.10" version strings survive)
    r"\b(?:\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}\.\d{1,2}\.\d{4})\b"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?",
    # Month names: 14 March 2019, March 14, 2019, Mar 14 2019, 14-Mar-2019
    rf"\b\d{{1,2}}[\s-]{_MONTHS}\.?[\s-],?\s*\d{{2,4}}\b",
    rf"\b{_MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b",
    # Compact dates/datetimes 19700101..20991231 (with optional T/_ time)
    r"\b(?:19[7-9]\d|20\d\d)(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:[T_]?\d{4,6})?\b",
    # Raw epoch timestamps: microseconds (MEF uUTC, 15-16 digits) and
    # milliseconds (12-13 digits), both possibly negated on disk.
    r"-?\b\d{15,16}\b",
    r"-?\b1\d{12}\b",
]
_TIME_RE = re.compile("|".join(f"(?:{p})" for p in _TIME_PATTERNS), re.IGNORECASE)


class IdentityRegistry:
    """A thread-safe set of strings that must never be logged."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._strings: set[str] = set()
        self._pattern: re.Pattern[str] | None = None

    def register(self, *values: object) -> None:
        added = False
        with self._lock:
            for value in values:
                text = str(value).strip() if value is not None else ""
                # A single character would redact half the log; anything
                # longer is registered, however generic it looks.
                if len(text) >= 2 and text.lower() not in self._strings:
                    self._strings.add(text.lower())
                    added = True
            if added:
                ordered = sorted(self._strings, key=len, reverse=True)
                self._pattern = re.compile("|".join(map(re.escape, ordered)), re.IGNORECASE)

    def clear(self) -> None:
        with self._lock:
            self._strings.clear()
            self._pattern = None

    def redact(self, text: str) -> str:
        pattern = self._pattern
        if pattern is None:
            return text
        return pattern.sub(REDACTED, text)


#: The app-wide registry the log filter consults.
identities = IdentityRegistry()


def register_identity(*values: object) -> None:
    """Never log these strings (patient names, IDs, passwords, ...)."""
    identities.register(*values)


def scrub(text: str, registry: IdentityRegistry | None = None) -> str:
    """Redact identity strings, then absolute dates and timestamps."""
    text = (registry or identities).redact(text)
    return _TIME_RE.sub(TIME, text)


class PrivacyFilter(logging.Filter):
    """Rewrites each record's message (and its traceback) through
    :func:`scrub` before any handler formats it."""

    def __init__(self, registry: IdentityRegistry | None = None) -> None:
        super().__init__()
        self.registry = registry

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = f"{record.msg!r} (unformattable arguments)"
        record.msg = scrub(message, self.registry)
        record.args = None
        if record.exc_info:
            text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = scrub(text, self.registry)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = scrub(record.exc_text, self.registry)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info, self.registry)
        return True
