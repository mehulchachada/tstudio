"""One brand theme for both tstudio apps.

Textual derives a family of CSS tokens from a theme's ``primary`` colour, and
three different widgets read from that family: the block cursor behind text in
a focused ``Input`` (``block-cursor-background``), the selection highlight in
an ``Input`` (``input-selection-background``) and the border of selected rows
(``border``). Overriding ``primary`` once therefore re-colours all three at
once, instead of patching each widget's CSS separately.
"""

from __future__ import annotations

TSTUDIO_THEME_NAME: str = "tstudio"


def tstudio_theme() -> "Theme":  # noqa: F821 - textual is imported lazily below
    """Build the tstudio theme, reading brand colours from ``tstudio.gallery.branding``."""
    from textual.theme import Theme

    from tstudio.gallery import branding

    return Theme(
        name=TSTUDIO_THEME_NAME,
        primary=branding.AMBER,
        accent=branding.AMBER,
        secondary=branding.VIOLET,
        success=branding.GREEN,
        warning=branding.VIOLET,
        error=branding.RED,
        background="#0b0b0d",
        surface="#121214",
        panel="#17171a",
        dark=True,
    )


def apply_theme(app) -> None:
    """Register the tstudio theme on ``app`` and make it the active theme.

    Safe to call twice: re-registering a theme under a name Textual already
    knows is ignored, and the assignment still runs.
    """
    theme = tstudio_theme()
    try:
        app.register_theme(theme)
    except ValueError:
        # The name is already registered on this app; the assignment below
        # still activates it.
        pass
    app.theme = TSTUDIO_THEME_NAME
