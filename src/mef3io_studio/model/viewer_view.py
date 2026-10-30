"""The signal viewer's view state, physical scales and colour profiles.

Plain Python, no Qt. Spec: ``docs/design/viewer.md`` ("Layout",
"Navigation"). The page length is physical: the plot width in millimetres
divided by the timebase (mm per second); millimetres come from the screen's
logical resolution, so a second is the same length on every screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .dsp import FilterSettings

#: Sensitivity, microvolts per millimetre (default 100).
SENS_STEPS = (
    1, 2, 3, 5, 7, 10, 15, 20, 30, 50, 70, 100, 150, 200, 300, 500, 700, 1000, 1500, 2000,
    3000, 5000,
)  # fmt: skip
#: Row spacing, millimetres (default 7).
SPACING_STEPS = (2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30)
#: Timebase, millimetres per second (default 30): the clinical steps...
TIMEBASE_CLINICAL = (3, 5, 7.5, 10, 15, 20, 30, 40, 60, 90, 120, 180, 240)
#: ...and the slower ones that zoom out into the overview.
TIMEBASE_OVERVIEW = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2)
TIMEBASE_STEPS = TIMEBASE_OVERVIEW + TIMEBASE_CLINICAL
#: Alt+1 ... Alt+5.
TIMEBASE_KEYS = (10, 15, 30, 60, 120)
DEFAULT_SENSITIVITY = 100
DEFAULT_TIMEBASE = 30
DEFAULT_SPACING = 7
#: Pages longer than this many seconds are drawn as an overview.
OVERVIEW_PAGE_S = 300.0

MONTAGE_RECORDED = "recorded"
MONTAGE_REFERENCE = "reference"
MONTAGE_BIPOLAR = "bipolar"
MONTAGES = (MONTAGE_RECORDED, MONTAGE_REFERENCE, MONTAGE_BIPOLAR)
MONTAGE_NAMES = {
    MONTAGE_RECORDED: "As recorded",
    MONTAGE_REFERENCE: "Referenced to a channel",
    MONTAGE_BIPOLAR: "Lead bipolar",
}

#: Keys of the profile list: "auto" follows the app theme.
PROFILE_NAMES = {
    "auto": "Match the app",
    "white": "White",
    "green": "Green (paper grid)",
    "dark": "Dark",
    "contrast": "High contrast",
}


def step_value(steps, current: float, delta: int) -> float:
    """The step ``delta`` places from the one nearest to ``current``."""
    nearest = min(range(len(steps)), key=lambda i: abs(steps[i] - current))
    return steps[max(0, min(len(steps) - 1, nearest + delta))]


def nearest_step(steps, value: float) -> float:
    return min(steps, key=lambda s: abs(s - value))


def page_us(plot_px: float, px_per_mm: float, timebase: float) -> int:
    """Length of a page in microseconds."""
    if plot_px <= 0 or px_per_mm <= 0 or timebase <= 0:
        return 1_000_000
    return max(1, round(plot_px / px_per_mm / timebase * 1e6))


def is_overview(length_us: float) -> bool:
    return length_us > OVERVIEW_PAGE_S * 1e6


@dataclass(frozen=True)
class ViewState:
    """What the user chose. Kept per window while it is open."""

    t0: int
    sensitivity: float = DEFAULT_SENSITIVITY
    timebase: float = DEFAULT_TIMEBASE
    spacing: float = DEFAULT_SPACING
    montage: str = MONTAGE_RECORDED
    reference: str | None = None
    hidden: frozenset = field(default_factory=frozenset)
    filters: FilterSettings = field(default_factory=FilterSettings)
    #: First visible row (rows scroll in whole rows).
    row_offset: int = 0
    profile: str = "auto"

    def with_(self, **changes) -> ViewState:
        return replace(self, **changes)


def clamp_start(t0: int, length_us: int, timeline_start: int, timeline_end: int) -> int:
    """Keep a page inside the timeline (or at its start when the page is longer)."""
    last = timeline_end - length_us
    if last <= timeline_start:
        return timeline_start
    return max(timeline_start, min(int(t0), last))


def aligned_block(t0: int, length_us: int, timeline_start: int) -> tuple[int, int]:
    """The view block holding the page that starts at ``t0``: 1.5 page
    lengths long, starting on a half-page boundary of the timeline, so every
    page lies inside ONE block and small moves reuse it."""
    half = max(1, length_us // 2)
    k = (int(t0) - timeline_start) // half
    start = timeline_start + k * half
    return start, start + length_us + half


def next_block_start(block: tuple[int, int], length_us: int, direction: int) -> int:
    """Start of the page after/before the block's page, for look-ahead."""
    return block[0] + (length_us if direction >= 0 else -length_us)


# --- colours ---------------------------------------------------------------


@dataclass(frozen=True)
class Palette:
    name: str
    background: str
    trace: str
    trace_dim: str
    text: str
    muted: str
    grid_minor: str | None
    grid_major: str | None
    time_grid: str
    gap: str
    cursor: str
    selection: str
    dark: bool


PROFILES = {
    "white": Palette(
        "white",
        "#ffffff",
        "#14181d",
        "#9aa3ad",
        "#2b333c",
        "#59636f",
        None,
        None,
        "#e6e9ed",
        "#e4e7eb",
        "#2f6feb",
        "#2f6feb",
        False,
    ),  # fmt: skip
    "green": Palette(
        "green",
        "#f0faf0",
        "#0c2a0c",
        "#7ea77e",
        "#17331a",
        "#4c6a4f",
        "#d2ead2",
        "#9fcf9f",
        "#bfdcbf",
        "#dbeedb",
        "#b00020",
        "#1d6b25",
        False,
    ),  # fmt: skip
    "dark": Palette(
        "dark",
        "#13161b",
        "#dde3ea",
        "#6f7a87",
        "#dde3ea",
        "#98a2b0",
        None,
        None,
        "#262b33",
        "#20252c",
        "#5b93ff",
        "#5b93ff",
        True,
    ),  # fmt: skip
    "contrast": Palette(
        "contrast",
        "#000000",
        "#ffffff",
        "#9a9a9a",
        "#ffffff",
        "#e0e0e0",
        None,
        None,
        "#4a4a4a",
        "#303030",
        "#ffff00",
        "#ffff00",
        True,
    ),  # fmt: skip
}

#: Recording colours (letter A, B, ...), for light and dark backgrounds.
RECORDING_COLOURS_LIGHT = (
    "#15803d", "#c2410c", "#a21caf", "#b45309", "#4d7c0f", "#be123c", "#0f766e", "#854d0e",
)  # fmt: skip
RECORDING_COLOURS_DARK = (
    "#4ade80", "#fb923c", "#e879f9", "#fbbf24", "#a3e635", "#fb7185", "#2dd4bf", "#facc15",
)  # fmt: skip
#: One recording on the strip: data green, gaps red (as in the clinical viewer).
STRIP_DATA = ("#2e9e4f", "#46c06b")
STRIP_GAP = ("#d64545", "#ff6b6b")


def resolve_profile(name: str, app_dark: bool) -> Palette:
    if name == "auto" or name not in PROFILES:
        return PROFILES["dark" if app_dark else "white"]
    return PROFILES[name]


def recording_colour(index: int, dark: bool) -> str:
    colours = RECORDING_COLOURS_DARK if dark else RECORDING_COLOURS_LIGHT
    return colours[index % len(colours)]


# --- text ------------------------------------------------------------------


def display_units(units: str) -> str:
    return "µV" if units.strip().lower() in ("uv", "µv", "μv") else units.strip()


def format_measure(dt_us: float, vmin: float | None, vmax: float | None, units: str) -> str:
    """ "250 ms · 4.0 Hz · −12 to 35 µV (47 µV)" for a Shift+drag box."""
    dt = abs(dt_us)
    if dt >= 1e6:
        t_text = f"{dt / 1e6:.3f} s"
    else:
        t_text = f"{dt / 1e3:.1f} ms"
    parts = [t_text]
    if dt > 0:
        parts.append(f"{1e6 / dt:.2f} Hz")
    if vmin is not None and vmax is not None:
        u = display_units(units)
        parts.append(f"{vmin:.1f} to {vmax:.1f} {u} ({vmax - vmin:.1f} {u})".replace("-", "−"))
    return " · ".join(parts)


@dataclass(frozen=True)
class Performance:
    """The viewer's performance settings; 0 = automatic (where it applies)."""

    load_workers: int = 0
    decode_threads: int = 0
    filter_workers: int = 0
    lookahead_pages: int = 2
    memory_mb: int = 0


PERF_LIMITS = {
    "load_workers": (0, 64),
    "decode_threads": (0, 64),
    "filter_workers": (0, 64),
    "lookahead_pages": (0, 8),
    "memory_mb": (0, 16384),
}


def clean_performance(perf: Performance, cores: int) -> Performance:
    """Values kept inside their ranges (and at most the number of cores for
    the worker counts)."""
    out = {}
    for name, (lo, hi) in PERF_LIMITS.items():
        v = int(getattr(perf, name))
        if name.endswith(("workers", "threads")):
            hi = max(1, cores)
        out[name] = max(lo, min(hi, v))
    if 0 < out["memory_mb"] < 128:
        out["memory_mb"] = 128
    return Performance(**out)


DEFAULT_MEMORY_MB = 512


def memory_bytes(perf: Performance) -> int:
    return (perf.memory_mb or DEFAULT_MEMORY_MB) * 1024 * 1024
