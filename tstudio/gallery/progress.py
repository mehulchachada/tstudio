"""Pure progress display: spinner, pulse, status lines, bar, ETA and render."""

from __future__ import annotations

import math

from tstudio import ClipError

SPINNER: tuple[str, ...] = (
    "▏▏▏▏▏▏▏▏",
    "▏▏▏▏▏▏▏▎",
    "▏▏▏▏▏▏▍▏",
    "▏▏▏▏▏▌▏▏",
    "▏▏▏▏▋▏▏▏",
    "▏▏▏▊▏▏▏▏",
    "▏▏▉▏▏▏▏▏",
    "▏▐▏▏▏▏▏▏",
)

PULSE: tuple[str, ...] = ("◌", "◍", "◉", "◉", "◍", "◌")

LINES: tuple[str, ...] = (
    "reading keyframes",
    "pushing pixels through the encoder",
    "tone-mapping the highlights",
    "counting frames nobody will see",
    "asking the gpu nicely",
    "rebuilding the colour",
    "lining up the frames",
    "deciding what a shadow is",
    "smoothing the motion",
    "packing the bitstream",
    "checking the audio sync",
    "warming up the encoder",
    "counting the bits",
    "settling the levels",
)

#: One eighth through seven eighths of a cell. There is no eighth entry: a
#: full cell is "█", which the whole-cell run already draws. So this is seven
#: glyphs, not eight, and indexing it with eighths needs the clamp in `bar`.
_PARTIAL = "▏▎▍▌▋▊▉"


def frame_at(tick: int, frames: tuple[str, ...]) -> str:
    """Return the frame at this tick, cycling through the tuple."""
    if not frames:
        raise ClipError("no frames to animate")
    return frames[tick % len(frames)]


def line_at(tick: int, *, every: int = 20, lines: tuple[str, ...] = LINES) -> str:
    """Return the playful status line for this tick, changing once every `every` ticks."""
    if every < 1:
        raise ClipError("every must be at least 1")
    if not lines:
        raise ClipError("no lines to show")
    return lines[(tick // every) % len(lines)]


def bar(fraction: float, width: int) -> str:
    """A bar `width` cells wide, with a partial block for the in-between cell."""
    if width < 1:
        raise ClipError("bar width must be at least 1")
    if fraction != fraction:  # NaN
        fraction = 0.0
    fraction = min(max(fraction, 0.0), 1.0)
    cells = fraction * width
    whole = int(cells)
    if whole >= width:
        return "█" * width
    partial = cells - whole
    if partial <= 0.0:
        return "█" * whole + " " * (width - whole)
    # partial is in (0, 1), so partial * 8 reaches 7.x and floors to 7 - one
    # past the end of a seven-glyph string. That raised IndexError for every
    # fraction whose cell was at least seven eighths full: one progress value
    # in eight, hit live during a long encode.
    #
    # Clamped rather than carried into the whole-cell run on purpose. Carrying
    # would draw a full bar at 99%, and a progress bar that says finished
    # before it is finished is worse than one that sits at seven eighths.
    eighths = min(int(partial * 8), len(_PARTIAL) - 1)
    return "█" * whole + _PARTIAL[eighths] + " " * (width - whole - 1)


def eta(fraction: float, elapsed: float) -> float | None:
    """Seconds remaining, extrapolated from the fraction done so far."""
    if fraction <= 0.0 or fraction >= 1.0 or elapsed <= 0.0:
        return None
    return elapsed * (1.0 - fraction) / fraction


def format_eta(seconds: float | None) -> str:
    """Format seconds as M:SS, H:MM:SS past an hour, or --:-- when unknown."""
    if seconds is None:
        return "--:--"
    total = int(round(seconds))
    if total < 0:
        total = 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def render(fraction: float, elapsed: float, tick: int, width: int, *, label: str = "") -> list[str]:
    """Two lines, each exactly `width` characters, for the progress display."""
    if width < 24:
        raise ClipError("width must be at least 24 to fit the numbers")
    if fraction != fraction:  # NaN
        fraction = 0.0
    fraction = min(max(fraction, 0.0), 1.0)
    pct = f"{round(fraction * 100):>3}%"
    elapsed_s = f"{int(elapsed)}s"
    eta_s = format_eta(eta(fraction, elapsed))
    line1 = f"{frame_at(tick, SPINNER)} {bar(fraction, 10)} {pct} {elapsed_s} / {eta_s}"
    text = label if label else line_at(tick)
    line2 = f"{frame_at(tick, PULSE)} {text}"
    return [line1[:width].ljust(width), line2[:width].ljust(width)]
