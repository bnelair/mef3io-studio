"""The privacy filter for logging.

Three kinds of text must never reach the log file:

* **identity strings** — patient names, IDs, passwords, recording names —
  anything the app learns while it works. Code that comes across one calls
  :func:`register_identity`; from then on it is replaced by ``[redacted]``
  wherever it appears: in any letter case, with or without accents
  (Müller / Muller / MÜLLER / decomposed Unicode), and with any separators
  between its parts (``MRN-55501234`` also matches ``MRN55501234`` and
  ``MRN 55501234``);
* **paths** — folder and file names often contain names and dates. Anything
  path-like (contains ``/`` or ``\\`` next to a letter, a quoted string with a
  slash, or a name ending in ``.mefd`` / ``.mefd.tar``) becomes ``<path>``,
  and the user's home folder is removed first;
* **absolute dates and times** — a recording's real date is identifying.
  Calendar dates in any common form, clock times, ``datetime(...)`` reprs,
  ctime and RFC 2822 stamps, ISO week dates, and raw epoch numbers (seconds,
  milliseconds, MEF microseconds) become ``[time]``.

It errs on the side of removing too much: a version number that looks like a
date is redacted too. The filter is attached to every handler the app
installs, so it covers library records, warnings and tracebacks as well. It
redacts rather than drops a record, so the log stays useful.

No Qt, no knowledge of MEF bytes.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from pathlib import Path

REDACTED = "[redacted]"
TIME = "[time]"
PATH = "<path>"

_MONTHS = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DAYS = r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*"
# Digit boundaries instead of \b, so "_2019_03_14" and "ses-20190314" match.
_B = r"(?<!\d)"
_E = r"(?!\d)"
_SEP = r"[-/._]"
_CLOCK = r"\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?"
_ZONE = r"(?:\s?(?:Z|UTC|GMT|[+-]\d{2}:?\d{2}))?"

# Ordered most specific first, so a whole stamp becomes one [time].
_TIME_PATTERNS = [
    # datetime / date / Timestamp reprs: datetime.datetime(2019, 3, 14, 9, 26)
    r"(?:datetime\.)?(?:datetime|date|Timestamp)\(\s*\d{4}\s*,[^)]*\)",
    # ctime / RFC 2822: Thu Mar 14 09:26:53 2019, Thu, 14 Mar 2019 09:26:53 +0000
    rf"(?:{_DAYS},?\s+)?{_MONTHS}\.?\s+\d{{1,2}}\s+{_CLOCK}(?:\s+\d{{4}})?",
    rf"(?:{_DAYS},?\s+)?\d{{1,2}}[\s-]{_MONTHS}\.?[\s-],?\s*\d{{2,4}}{_E}(?:\s+{_CLOCK}{_ZONE})?",
    rf"(?:{_DAYS},?\s+)?{_MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}{_E}",
    # ISO week dates: 2019-W11-4, 2019W114
    rf"{_B}\d{{4}}-?W\d{{2}}(?:-?\d)?{_E}",
    # Year first: 2019-03-14, 2019/3/14, 2019_03_14, 2019.03.14 (+ time, zone)
    rf"{_B}(?:19|20)\d{{2}}{_SEP}\d{{1,2}}{_SEP}\d{{1,2}}{_E}(?:[T _]{_CLOCK}{_ZONE})?",
    # Day or month first: 14/03/2019, 3-14-19, 14.3.19, 14_03_2019 (+ time)
    rf"{_B}\d{{1,2}}{_SEP}\d{{1,2}}{_SEP}(?:\d{{4}}|\d{{2}}){_E}(?:\s+{_CLOCK})?",
    # Space-separated with a four-digit year: 14 03 2019
    rf"{_B}\d{{1,2}} \d{{1,2}} (?:19|20)\d{{2}}{_E}",
    # Compact: 20190314, 20190314T092653, 20190314_0926
    rf"{_B}(?:19[7-9]\d|20\d\d)(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:[T_]?\d{{4,6}})?{_E}",
    # Clock times on their own: 09:26:53, 09:26:53.589
    rf"{_B}\d{{1,2}}:\d{{2}}:\d{{2}}(?:[.,]\d+)?{_E}{_ZONE}",
    # Epoch numbers: microseconds (MEF uUTC, 15-16 digits), milliseconds
    # (12-13 digits) and seconds (2001-2033: 10 digits starting with 1).
    rf"{_B}\d{{15,16}}{_E}",
    rf"{_B}1\d{{12}}{_E}",
    rf"{_B}1\d{{9}}(?:\.\d+)?{_E}",
]
_TIME_RE = re.compile("|".join(f"(?:{p})" for p in _TIME_PATTERNS), re.IGNORECASE)

_PATH_PATTERNS = [
    # Quoted strings containing a slash: '/data/Jane Doe/x', "C:\\Users\\Jane Doe"
    r"'[^'\n]*[/\\][^'\n]*'",
    r'"[^"\n]*[/\\][^"\n]*"',
    # Drive-letter and UNC paths up to the next whitespace
    r"(?<![\w])[A-Za-z]:[/\\]\S*",
    r"\\\\\S+",
    # Any token containing a slash or backslash next to a letter, or starting
    # with / ~ \ (so "3/10" progress text survives, "/data/x" does not)
    r"(?<!\S)[~/\\]\S*",
    r"\S*[A-Za-z][/\\]\S*",
    r"\S*[/\\][A-Za-z]\S*",
    # Recording names on their own, and BIDS-style names (sub-0042_ses-01)
    r"\S*\.mefd(?:\.tar)?\b\S*",
    r"\S*(?<![A-Za-z])(?:sub|ses)-[A-Za-z0-9]\S*",
]
_PATH_RE = re.compile("|".join(f"(?:{p})" for p in _PATH_PATTERNS), re.IGNORECASE)

_COMBINING = "[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]*"


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))


def _identity_pattern(value: str) -> str:
    """Regex for ``value`` matching any case, accents and separators. Applied
    to NFD-normalised text, so every base letter may carry combining marks."""
    base = _strip_accents(unicodedata.normalize("NFKC", value)).casefold()
    parts = [p for p in re.split(r"[\W_]+", base) if p]
    pieces = []
    for part in parts:
        pieces.append("".join(re.escape(c) + _COMBINING for c in part))
    return r"[\W_]*".join(pieces)


class IdentityRegistry:
    """A thread-safe set of strings that must never be logged."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: set[str] = set()
        self._patterns: list[str] = []
        self._regex: re.Pattern[str] | None = None

    def register(self, *values: object) -> None:
        with self._lock:
            added = False
            for value in values:
                text = str(value).strip() if value is not None else ""
                key = _strip_accents(unicodedata.normalize("NFKC", text)).casefold()
                # A single character would redact half the log; anything
                # longer is registered, however generic it looks.
                if len(re.sub(r"[\W_]+", "", key)) < 2 or key in self._keys:
                    continue
                self._keys.add(key)
                self._patterns.append(_identity_pattern(text))
                added = True
            if added:
                ordered = sorted(self._patterns, key=len, reverse=True)
                self._regex = re.compile("|".join(ordered), re.IGNORECASE)

    def clear(self) -> None:
        with self._lock:
            self._keys.clear()
            self._patterns.clear()
            self._regex = None

    def redact(self, text: str) -> str:
        regex = self._regex
        if regex is None:
            return text
        decomposed = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text))
        return unicodedata.normalize("NFC", regex.sub(REDACTED, decomposed))


#: The app-wide registry the log filter consults.
identities = IdentityRegistry()


def register_identity(*values: object) -> None:
    """Never log these strings (patient names, IDs, passwords, recording names)."""
    identities.register(*values)


def _home_variants() -> list[str]:
    try:
        home = str(Path.home())
    except Exception:
        return []
    if len(home) < 3:
        return []
    return sorted({home, home.replace("\\", "/")}, key=len, reverse=True)


_HOMES = _home_variants()


def scrub(text: str, registry: IdentityRegistry | None = None) -> str:
    """Redact identity strings, the home folder, paths, then dates and times."""
    text = (registry or identities).redact(text)
    for home in _HOMES:
        text = text.replace(home, PATH)
    text = _PATH_RE.sub(PATH, text)
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
            message = "(a log message that could not be formatted)"
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
