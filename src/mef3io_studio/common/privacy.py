"""The privacy filter for logging.

Three kinds of text must never reach the log file:

* **identity strings** — patient names, IDs, passwords, recording names —
  anything the app learns while it works. Code that comes across one calls
  :func:`register_identity`; from then on it is replaced by ``[redacted]``
  wherever it appears: in any letter case, with or without accents
  (Müller / Muller / MÜLLER / decomposed Unicode), and with any separators
  between its parts (``MRN-55501234`` also matches ``MRN55501234`` and
  ``MRN 55501234``);
* **paths** — folder and file names often contain names and dates. Any run
  of tokens joined by ``/`` or ``\\`` (any script, spaces included up to the
  next ``:``, ``,``, ``)`` or quote), a quoted string with a slash, and any
  recording file or folder name (``.mefd``, ``.segd``, ``.tdat``, ...)
  becomes ``<path>``; the user's home folder is removed first. E-mail
  addresses become ``<email>``;
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
EMAIL = "<email>"

_MONTHS = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
# Month names in the languages most likely on a European clinical machine
# (de, fr, es, it, pt, nl, cs, pl incl. their genitive forms) plus a few
# unambiguous abbreviations. Words that are also common in English
# ("mar", "set", "mai" alone) are deliberately left out.
_LOCAL_MONTHS = (
    "januar|jänner|februar|märz|mär|april|mai|juni|juli|august|september|oktober|"
    "november|dezember|janvier|février|fevrier|mars|avril|juin|juillet|août|aout|"
    "septembre|octobre|novembre|décembre|decembre|enero|febrero|marzo|abril|mayo|"
    "junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre|gennaio|"
    "febbraio|aprile|maggio|giugno|luglio|settembre|ottobre|dicembre|janeiro|"
    "fevereiro|março|marco|maio|junho|julho|setembro|outubro|novembro|dezembro|"
    "januari|februari|maart|mei|augustus|leden|ledna|únor|února|březen|března|"
    "duben|dubna|květen|května|červen|června|červenec|července|srpen|srpna|září|"
    "říjen|října|listopad|listopadu|prosinec|prosince|styczeń|stycznia|luty|lutego|"
    "marzec|marca|kwiecień|kwietnia|maj|maja|czerwiec|czerwca|lipiec|lipca|"
    "sierpień|sierpnia|wrzesień|września|październik|października|listopada|"
    "grudzień|grudnia"
)
_LETTER = r"[^\W\d_]"  # any Unicode letter
# Month names only as whole words ("Jan" must not match inside "JaneDoe").
_ANY_MONTH = rf"(?<!{_LETTER})(?:{_MONTHS}|{_LOCAL_MONTHS})(?!{_LETTER})"
_EN_MONTH = rf"(?<!{_LETTER}){_MONTHS}(?!{_LETTER})"
_DAYS = r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*"
# Digit boundaries instead of \b, so "_2019_03_14" and "ses-20190314" match.
_B = r"(?<!\d)"
_E = r"(?!\d)"
_SEP = r"[-/._]"
_CLOCK = r"\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:\s?[AaPp]\.?[Mm]\.?)?"
_ZONE = r"(?:\s?(?:Z|UTC|GMT|CES?T|[+-]\d{2}:?\d{2}))?"

# Ordered most specific first, so a whole stamp becomes one [time].
_TIME_PATTERNS = [
    # datetime / date / Timestamp reprs: datetime.datetime(2019, 3, 14, 9, 26)
    r"(?:datetime\.)?(?:datetime|date|Timestamp)\(\s*\d{4}\s*,[^)]*\)",
    # CJK: 2019年3月14日 09時26分 (any Unicode digits)
    rf"{_B}\d{{2,4}}\s*年\s*\d{{1,2}}\s*月(?:\s*\d{{1,2}}\s*日)?(?:\s*\d{{1,2}}\s*時(?:\s*\d{{1,2}}\s*分)?)?",
    rf"{_B}\d{{1,2}}\s*月\s*\d{{1,2}}\s*日",
    # ctime / RFC 2822: Thu Mar 14 09:26:53 2019, Thu, 14 Mar 2019 09:26:53 +0000
    rf"(?:{_DAYS},?\s+)?{_EN_MONTH}\.?\s+\d{{1,2}}\s+{_CLOCK}(?:\s+\d{{4}})?",
    # Day + month name (+ year, + time), any listed language:
    # 14 Mar 2019, 14. März 2019, 14 de marzo de 2019, 14-Mar-2019
    rf"{_B}\d{{1,2}}\.?[\s-]*(?:de\s+|of\s+)?{_ANY_MONTH}\.?(?:[\s,-]*(?:de\s+)?\d{{2,4}}{_E})?"
    rf"(?:\s+{_CLOCK}{_ZONE})?",
    # Month name + day (+ year): March 14, 2019, Mar-14-2019, März 14
    rf"{_ANY_MONTH}\.?[\s-]+\d{{1,2}}(?:st|nd|rd|th)?{_E}(?:[\s,-]+\d{{4}}{_E})?(?:\s+{_CLOCK})?",
    # Month name + year: March 2019, März 2019, marzo de 2019
    rf"{_ANY_MONTH}\.?,?\s+(?:de\s+)?(?:19|20)\d{{2}}{_E}",
    # ISO week dates: 2019-W11-4, 2019W114
    rf"{_B}\d{{4}}-?W\d{{2}}(?:-?\d)?{_E}",
    # Year first: 2019-03-14, 2019/3/14, 2019_03_14, 2019.03.14 (+ time, zone)
    rf"{_B}(?:19|20)\d{{2}}{_SEP}\d{{1,2}}{_SEP}\d{{1,2}}{_E}(?:[T _]{_CLOCK}{_ZONE})?",
    # Day or month first: 14/03/2019, 3-14-19, 14.3.19, 14_03_2019 (+ time)
    rf"{_B}\d{{1,2}}{_SEP}\d{{1,2}}{_SEP}(?:\d{{4}}|\d{{2}}){_E}(?:,?\s+{_CLOCK}{_ZONE})?",
    # Day and month with a clock time: on 14/03 at 09:26
    rf"{_B}\d{{1,2}}[/.-]\d{{1,2}}{_E}\.?,?\s+(?:at\s+|um\s+|à\s+|a las\s+|alle\s+)?{_CLOCK}",
    # Space-separated with a four-digit year: 14 03 2019
    rf"{_B}\d{{1,2}} \d{{1,2}} (?:19|20)\d{{2}}{_E}",
    # Compact: 20190314, 20190314T092653, 20190314_0926
    rf"{_B}(?:19[7-9]\d|20\d\d)(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:[T_]?\d{{4,6}})?{_E}",
    # Clock times on their own: 09:26:53, 09:26:53.589, 9:26 AM
    rf"{_B}\d{{1,2}}:\d{{2}}:\d{{2}}(?:[.,]\d+)?{_E}{_ZONE}",
    rf"{_B}\d{{1,2}}:\d{{2}}\s?[AaPp]\.?[Mm]\b\.?",
    # Epoch numbers: microseconds (MEF uUTC, 15-16 digits), milliseconds
    # (12-13 digits), seconds (9-10 digits: 1980-2033).
    rf"{_B}\d{{15,16}}{_E}",
    rf"{_B}1\d{{12}}{_E}",
    rf"{_B}(?:1\d{{9}}|[3-9]\d{{8}})(?:\.\d+)?{_E}",
]
_TIME_RE = re.compile("|".join(f"(?:{p})" for p in _TIME_PATTERNS), re.IGNORECASE)

# Version numbers ("Python 3.12.12", "Qt 6.12.0") look like dates; they are
# set aside before the date rules run and put back afterwards.
_VERSION_RE = re.compile(
    r"\b(?:python|qt|pyside6|shiboken6|mef3io(?:-studio)?|numpy|app|version|v)"
    r"\s*v?\d+(?:\.\d+){1,3}\b",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# Recording files and folders by name: rec.mefd, x.mefd.tar, JaneDoe-000001.segd
_MEF_NAME_RE = re.compile(
    r"[^\s:,()\"'<>]*\.(?:mefd|timd|segd|vidd|tdat|tmet|tidx|rdat|ridx)(?:\.tar)?\b",
    re.IGNORECASE,
)
_TOKEN = r"[^\s:,()\"'<>]"
# A path is any run of non-space tokens joined by / or \ (also URL-encoded
# %2F), optionally after a drive letter or URL scheme. Once one is found,
# everything up to the next ":", ",", ")", quote or line end is part of it —
# folder names contain spaces ("/Volumes/Patient Jane Doe/rec.mefd").
_PATH_RE = re.compile(
    r"'[^'\n]*[/\\][^'\n]*'"  # quoted, with a slash: '/data/Jane Doe/x'
    r'|"[^"\n]*[/\\][^"\n]*"'
    # starts at a token boundary, and not on a plain fraction ("3/10")
    rf"|(?<!{_TOKEN})(?!\d{{1,3}}/\d{{1,4}}(?![^\s:,()\"'<>]))"
    rf"(?:{_LETTER}:)?{_TOKEN}*(?:[/\\]|%2F|%5C){_TOKEN}*[^:,()\n\"'<>]*",
    re.IGNORECASE,
)
# BIDS-style names: sub-0042_ses-01
_BIDS_RE = re.compile(rf"\S*(?<!{_LETTER})(?:sub|ses)-[^\W_]\S*")


def _path_sub(match: re.Match) -> str:
    text = match.group(0)
    trailing = text[len(text.rstrip()) :]
    return PATH + trailing


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))


def _identity_pattern(value: str) -> str:
    """Regex for ``value`` matching any case, accents and separators. Applied
    to the skeleton of NFD-normalised text (see :func:`_skeleton`)."""
    base = _strip_accents(unicodedata.normalize("NFKC", value)).casefold()
    parts = [p for p in re.split(r"[\W_]+", base) if p]
    return r"[\W_]*".join(re.escape(part) for part in parts)


def _skeleton(text: str) -> tuple[str, list[int]]:
    """``text`` without combining marks and invisible format characters
    (zero-width spaces, soft hyphens), plus where each kept character sits in
    ``text``. Identities are matched on the skeleton, so accents and hidden
    characters can't split a name, and their pattern stays small (cheap to
    compile)."""
    kept, where = [], []
    for i, ch in enumerate(text):
        if unicodedata.combining(ch) or unicodedata.category(ch) == "Cf":
            continue
        kept.append(ch)
        where.append(i)
    return "".join(kept), where


class IdentityRegistry:
    """A thread-safe set of strings that must never be logged.

    Each value is compiled once, on its own, when it is registered (cost
    independent of how many are already known); a log line is checked
    against each. One combined pattern would have to be recompiled on every
    registration, which grows with every recording opened and, since
    compiling holds the GIL, would stall the window.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: set[str] = set()
        #: (pattern length, compiled), longest first so the longest match wins.
        self._regexes: list[tuple[int, re.Pattern[str]]] = []

    def register(self, *values: object) -> None:
        new = []
        with self._lock:
            for value in values:
                text = str(value).strip() if value is not None else ""
                key = _strip_accents(unicodedata.normalize("NFKC", text)).casefold()
                # A single character would redact half the log; anything
                # longer is registered, however generic it looks.
                if len(re.sub(r"[\W_]+", "", key)) < 2 or key in self._keys:
                    continue
                self._keys.add(key)
                new.append(text)
        compiled = []
        for text in new:  # compiled outside the lock
            pattern = _identity_pattern(text)
            compiled.append((len(pattern), re.compile(pattern, re.IGNORECASE)))
        if compiled:
            with self._lock:
                regexes = self._regexes + compiled
                regexes.sort(key=lambda item: -item[0])
                self._regexes = regexes

    def clear(self) -> None:
        with self._lock:
            self._keys.clear()
            self._regexes = []

    def redact(self, text: str) -> str:
        regexes = self._regexes
        if not regexes:
            return text
        decomposed = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text))
        base, where = _skeleton(decomposed)
        spans = []
        for _length, regex in regexes:
            for m in regex.finditer(base):
                if m.end() > m.start():
                    end = where[m.end()] if m.end() < len(where) else len(decomposed)
                    spans.append((where[m.start()], end))
        if not spans:
            return text
        spans.sort()
        merged = [list(spans[0])]
        for start, end in spans[1:]:
            if start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        out, pos = [], 0
        for start, end in merged:
            out.append(decomposed[pos:start])
            out.append(REDACTED)
            pos = end
        out.append(decomposed[pos:])
        return unicodedata.normalize("NFC", "".join(out))


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
    """Redact identity strings, the home folder, dates and times, e-mail
    addresses, recording names and paths (in that order)."""
    text = (registry or identities).redact(text)
    for home in _HOMES:
        text = text.replace(home, PATH)
    versions: list[str] = []

    def keep_version(match: re.Match) -> str:
        versions.append(match.group(0))
        return f"\x00{len(versions) - 1}\x00"

    text = _VERSION_RE.sub(keep_version, text)
    text = _TIME_RE.sub(TIME, text)
    text = _EMAIL_RE.sub(EMAIL, text)
    text = _PATH_RE.sub(_path_sub, text)
    text = _MEF_NAME_RE.sub(PATH, text)
    text = _BIDS_RE.sub(PATH, text)
    if versions:
        text = re.sub("\x00(\\d+)\x00", lambda m: versions[int(m.group(1))], text)
    return text


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
