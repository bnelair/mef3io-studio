"""Colours of your own for the signal viewer (docs/design/viewer.md, "Colour
profiles").

Plain Python, no Qt. The user picks two colours, the background and the trace;
every other colour of the palette is DERIVED from them here, as a pure
function, so that text, traces and the accent keep at least 3:1 contrast
against the background (the WCAG ratio for large text and graphics). Two
colours that are themselves closer than 3:1 are refused with a sentence, and
never applied.

Colours are ``#rrggbb`` strings. Anything else (a stored value that was edited
by hand, an empty one) counts as no colour at all.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .viewer_view import PROFILES, Palette, resolve_profile

#: The key of the profile list for colours of your own.
PROFILE_CUSTOM = "custom"
#: The least contrast (a ratio) between the background and what is drawn on it.
MIN_CONTRAST = 3.0
#: What text aims for when the background allows it (the others get the best
#: the background allows, never below MIN_CONTRAST).
TEXT_CONTRAST = 7.0
MUTED_CONTRAST = 4.5

TOO_CLOSE = (
    "These two colours are too close to tell apart, so the traces would be hard to read. "
    "Choose colours that differ more (at least 3 to 1 in contrast)."
)

_HEX = re.compile(r"#([0-9a-fA-F]{6})")

RGB = tuple[int, int, int]

_SOFT_INK_LIGHT = (0x14, 0x18, 0x1D)
_SOFT_INK_DARK = (0xE8, 0xEC, 0xF1)
_ACCENT_LIGHT = (0x2F, 0x6F, 0xEB)
_ACCENT_DARK = (0x5B, 0x93, 0xFF)


# --- parsing ------------------------------------------------------------------


def parse_hex(text) -> RGB | None:
    """``(r, g, b)`` of a ``#rrggbb`` string, else None."""
    if not isinstance(text, str):
        return None
    m = _HEX.fullmatch(text.strip())
    if m is None:
        return None
    v = int(m.group(1), 16)
    return (v >> 16) & 255, (v >> 8) & 255, v & 255


def to_hex(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(c))) for c in rgb))


def normalise(text) -> str | None:
    """The lower-case ``#rrggbb`` form of a colour string, or None."""
    rgb = parse_hex(text)
    return None if rgb is None else to_hex(rgb)


def clean_pair(background, trace) -> tuple[str, str] | None:
    """Both colours, normalised, or None when either is not a colour."""
    a, b = normalise(background), normalise(trace)
    return (a, b) if a is not None and b is not None else None


# --- contrast -----------------------------------------------------------------


def _linear(c: int) -> float:
    v = c / 255.0
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def luminance(rgb: RGB) -> float:
    r, g, b = (_linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: RGB, b: RGB) -> float:
    """The WCAG contrast ratio (1 to 21) of two colours."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def is_dark(background: RGB) -> bool:
    """True when white reads better on ``background`` than black does."""
    return contrast((255, 255, 255), background) >= contrast((0, 0, 0), background)


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def _ink(background: RGB) -> RGB:
    return (255, 255, 255) if is_dark(background) else (0, 0, 0)


def _towards_ink(colour: RGB, background: RGB, target: float) -> RGB:
    """``colour``, moved towards black or white (whichever reads better on the
    background) by the least amount that reaches ``target`` contrast."""
    if contrast(colour, background) >= target:
        return colour
    ink = _ink(background)
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2
        if contrast(_mix(colour, ink, mid), background) >= target:
            hi = mid
        else:
            lo = mid
    return _mix(colour, ink, hi)


def _towards_background(colour: RGB, background: RGB, target: float, limit: float) -> RGB:
    """``colour``, moved towards the background by as much as keeps ``target``
    contrast (and at most ``limit`` of the way)."""
    if contrast(colour, background) <= target:
        return colour
    lo, hi = 0.0, limit
    for _ in range(24):
        mid = (lo + hi) / 2
        if contrast(_mix(colour, background, mid), background) >= target:
            lo = mid
        else:
            hi = mid
    return _mix(colour, background, lo)


# --- the palette ---------------------------------------------------------------


def problem(background, trace) -> str | None:
    """None when the two colours can be used; else a sentence saying why not."""
    pair = clean_pair(background, trace)
    if pair is None:
        return "These are not colours that can be used. Choose them again."
    if contrast(parse_hex(pair[0]), parse_hex(pair[1])) < MIN_CONTRAST:
        return TOO_CLOSE
    return None


@lru_cache(maxsize=32)
def _derive(background: str, trace: str) -> Palette:
    bg, tr = parse_hex(background), parse_hex(trace)
    dark = is_dark(bg)
    soft = _SOFT_INK_DARK if dark else _SOFT_INK_LIGHT
    text = soft if contrast(soft, bg) >= TEXT_CONTRAST else _ink(bg)
    muted = _towards_background(text, bg, MUTED_CONTRAST, 0.5)
    ink = _ink(bg)
    accent = _towards_ink(_ACCENT_DARK if dark else _ACCENT_LIGHT, bg, MIN_CONTRAST)
    return Palette(
        PROFILE_CUSTOM,
        to_hex(bg),
        to_hex(tr),
        to_hex(_towards_background(tr, bg, MIN_CONTRAST, 0.6)),
        to_hex(text),
        to_hex(muted),
        None,
        None,
        to_hex(_mix(bg, ink, 0.10)),
        to_hex(_mix(bg, ink, 0.06)),
        to_hex(accent),
        to_hex(accent),
        dark,
    )


def custom_palette(background: str, trace: str) -> Palette:
    """The palette for the two colours. ValueError when they cannot be used
    (not colours, or closer than 3:1); the sentence is the message."""
    why = problem(background, trace)
    if why is not None:
        raise ValueError(why)
    return _derive(normalise(background), normalise(trace))


def resolve_palette(name: str, app_dark: bool, custom=None) -> Palette:
    """The palette of a profile choice. ``custom`` = the stored pair for
    ``PROFILE_CUSTOM``; a pair that is missing or unusable (a bad stored
    value) gives the white profile."""
    if name != PROFILE_CUSTOM:
        return resolve_profile(name, app_dark)
    if custom is None or problem(*custom) is not None:
        return PROFILES["white"]
    return custom_palette(*custom)


def legible_on(palette: Palette, colour: str) -> str:
    """``colour`` as it should be drawn on ``palette``'s background. The built-in
    profiles are left exactly as they are; on colours of your own a colour
    that would fall below 3:1 is moved towards black or white just far enough."""
    if palette.name != PROFILE_CUSTOM:
        return colour
    rgb, bg = parse_hex(colour), parse_hex(palette.background)
    if rgb is None or bg is None:
        return colour
    return to_hex(_towards_ink(rgb, bg, MIN_CONTRAST))


def contrast_of(palette: Palette, colour: str) -> float:
    """The contrast of ``colour`` against the palette's background."""
    return contrast(parse_hex(colour), parse_hex(palette.background))
