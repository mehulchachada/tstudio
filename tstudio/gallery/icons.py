"""Block art for a file type, for the preview pane to show instead of a picture.

Drawing the actual frame inside the pane turned out to be the wrong thing to
promise. It needs the terminal to support sixel - which is asked for properly
(``sixel.query_da1``) but is absent in plenty of terminals, the Claude desktop
one among them - and even where it is present, rendering a frame out of a
3.7 GB 4K HDR source means an ffmpeg seek plus an ImageMagick convert, which
leaves the pane blank for seconds with nothing to say for itself.

So the pane shows what it can always show, instantly and in any terminal: what
kind of file this is, what is in it, and how to actually watch it. The real
picture lives one keypress away, in mpv, where it is a real picture.

Every glyph here is from Box Drawing (U+2500) or Block Elements (U+2580),
which any terminal font that can draw a border already has. Nothing from the
dingbat ranges, which is where the tofu lives: the one time a Circled Latin
``ⓘ`` was used in this project it rendered as an empty box in Cascadia Mono,
the default Windows Terminal font.
"""

from __future__ import annotations

from tstudio import ClipError

#: A strip of film, holes down both sides, with a play triangle in the middle.
FILM: tuple[str, ...] = (
    "▐▌▀▀▀▀▀▀▀▀▀▀▀▐▌",
    "▐▌           ▐▌",
    "▐▌     ▶     ▐▌",
    "▐▌           ▐▌",
    "▐▌▄▄▄▄▄▄▄▄▄▄▄▐▌",
)

#: A framed picture: a sun top left, hills along the bottom.
PICTURE: tuple[str, ...] = (
    "╭─────────────╮",
    "│  ◉          │",
    "│         ▄▆█ │",
    "│  ▁▄▆██████▆ │",
    "╰─────────────╯",
)

#: Anything the pane is asked about that it has no art for.
UNKNOWN: tuple[str, ...] = (
    "╭─────────────╮",
    "│             │",
    "│      ?      │",
    "│             │",
    "╰─────────────╯",
)

_BY_KIND: dict[str, tuple[str, ...]] = {
    "video": FILM,
    "image": PICTURE,
}

#: How a kind is named in the one-line action hint under the metadata.
_ACTIONS = {
    "video": "p  play in mpv",
    "image": "p  open",
}


def art_for_kind(kind: str) -> list[str]:
    """The block art for a listing ``kind``: "video", "image", anything else.

    An unknown kind is not an error - the listing grows new kinds before this
    module hears about them - so it gets the question-mark frame rather than
    an exception that would take the pane down.
    """
    return list(_BY_KIND.get(kind, UNKNOWN))


def action_for_kind(kind: str) -> str:
    """The one-line hint under the metadata, or "" when there is nothing to do."""
    return _ACTIONS.get(kind, "")


def width(art: list[str] | tuple[str, ...]) -> int:
    """How many cells wide the art is - every row is the same width by design.

    Nothing in the app needs this: the preview pane centres the block with
    ``content-align: center middle``, which handles the vertical axis too and
    cannot be done by padding strings. It is here because "every row is the
    same width" is the property that keeps a frame from leaning, and a
    property worth relying on is worth being able to check.
    """
    rows = list(art)
    if not rows:
        raise ClipError("that art has no rows, so it has no width")
    widths = {len(row) for row in rows}
    if len(widths) != 1:
        raise ClipError(
            f"art rows are not all the same width: {sorted(widths)}"
        )
    return widths.pop()
