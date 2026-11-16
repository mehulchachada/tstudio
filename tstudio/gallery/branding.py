"""Identity for tstudio: logo art, wordmark, tagline and palette."""

from __future__ import annotations

from tstudio import ClipError

NAME = "tstudio"
CURSOR = "▌"                      # U+258C left half block
SUBTITLE = "terminal studio"      # used by the header
WORDMARK = "tstudio" + CURSOR
TAGLINE = "browse · cut · send"
COMPACT = "◉ tstudio"

CAMERA = [
    "▄▄▄▪▄",
    "█ ◉ █",
    "▀▀▀▀▀",
]

AMBER  = "#e8c07d"   # logo, panel borders, selection
GREEN  = "#7ee787"   # success, progress, ready
VIOLET = "#c9a0ff"   # HDR badge, tone-mapping
RED    = "#ff7b72"   # errors, delete confirmation
GREY   = "#6e7681"   # metadata, hints

_LENS_STATES = {
    "idle": AMBER,
    "working": GREEN,
    "hdr": VIOLET,
    "error": RED,
}


def lens_colour(state: str) -> str:
    """Status colour for the lens in the logo.

    "idle" -> AMBER, "working" -> GREEN, "hdr" -> VIOLET, "error" -> RED.
    Anything else raises ClipError naming the four valid states.
    """
    try:
        return _LENS_STATES[state]
    except KeyError:
        raise ClipError(
            f"unknown lens state {state!r}; expected one of: "
            "idle, working, hdr, error"
        ) from None


def _centre(line: str, width: int) -> str:
    """Centre line within width; leading and trailing spaces differ by at most 1."""
    pad = max(0, width - len(line))
    left = pad // 2
    return " " * left + line + " " * (pad - left)


def header_lines(width: int) -> list[str]:
    """The three-row lockup for the top of a screen.

    Camera on the left, wordmark on the middle row, subtitle beneath.
    Under 80 columns, just the compact line. No returned line is longer
    than width; the subtitle is truncated before the wordmark is touched.
    Plain strings, no ANSI escapes - colour is applied by the caller.
    """
    if width < 80:
        return [COMPACT]
    subtitle = SUBTITLE
    wordmark = WORDMARK
    if len(CAMERA[2]) + 2 + len(subtitle) > width:
        subtitle = subtitle[: width - len(CAMERA[2]) - 2]
    if len(CAMERA[1]) + 2 + len(wordmark) > width:
        wordmark = wordmark[: width - len(CAMERA[1]) - 2]
    return [
        CAMERA[0],
        CAMERA[1] + "  " + wordmark,
        CAMERA[2] + "  " + subtitle,
    ]


def splash_lines(width: int) -> list[str]:
    """The empty-gallery panel, every line centred within width.

    A blank line, the three camera rows, a blank line, the wordmark, the
    tagline, a blank line, then the hint line. Under 80 columns, just the
    compact line and the hint line. Plain strings, no ANSI escapes.
    """
    hint = "o  open a folder      ◆  favourites"
    if width < 80:
        return [_centre(COMPACT, width), _centre(hint, width)]
    lines = ["", *CAMERA, "", WORDMARK, TAGLINE, "", hint]
    return [_centre(line, width) for line in lines]
