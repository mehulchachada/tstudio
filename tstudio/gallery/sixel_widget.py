"""A Textual widget that shows true pixel images in a running TUI, via sixel.

Textual owns the screen. It paints cells, it repaints them whenever anything
changes, and it knows nothing about the byte stream a sixel image is. So the
obvious thing - write the sixel bytes and hope - loses the picture on the next
refresh.

The file manager yazi solves this by treating the terminal as a second, lower
layer that it keeps in sync by hand (``yazi-adapter/src/drivers/sixel.rs``).
Its ``image_show`` erases whatever was shown before, remembers the target
rectangle in *character cells*, moves the real terminal cursor to that
rectangle's top-left cell, and only then writes the sixel bytes; its
``image_erase`` walks the same rectangle writing spaces in the theme
background. This module does exactly that, with a Textual widget's ``region``
as the rectangle:

    * the widget reserves the cells - ``render()`` returns blank padding of its
      own size, so Textual lays out around a hole of the right shape;
    * the picture is painted *over* those cells, outside Textual's renderer,
      with ``ESC [ row ; col H`` (1-based, hence ``region.y + 1`` and
      ``region.x + 1``) between ``ESC 7`` and ``ESC 8`` so Textual's own cursor
      position survives;
    * every repaint erases first, because the cells underneath are Textual's
      and it will have drawn its background over part of the image.

``render_sixel`` shells out to ImageMagick and takes 0.5-2.5 seconds, so it
only ever runs in a thread worker; the bytes come back to the UI thread
through ``App.call_from_thread``.

Where the terminal cannot do sixel at all there is still a picture: the
half-block coloured art from ``tstudio.gallery.thumbs``, rendered as ANSI text
inside the same cells. ``sixel_or_art_reason`` says which of the two the user
is looking at.

Textual is an optional dependency, so the widget is produced by a builder
function that imports textual on the inside; nothing at this module's top
level touches it.
"""

from __future__ import annotations

from pathlib import Path

from tstudio import ClipError
from tstudio.gallery.sixel import (
    SIXEL_HINT,
    render_sixel,
    sixel_size_for,
    sixel_supported,
    write_sixel,
)
from tstudio.gallery.thumbs import art_for

#: DECSC - save the cursor position, so Textual's own cursor is not disturbed.
SAVE_CURSOR: bytes = b"\x1b7"

#: DECRC - restore the position saved by :data:`SAVE_CURSOR`.
RESTORE_CURSOR: bytes = b"\x1b8"

#: SGR 0 - back to the terminal's default colours after an erase.
RESET_SGR: bytes = b"\x1b[0m"


def move_to(row: int, col: int) -> bytes:
    """``ESC [ row ; col H`` - the CUP sequence that parks the cursor.

    Both coordinates are 1-based, the way the terminal counts them. A Textual
    ``region`` is 0-based, so a caller passes ``region.y + 1`` and
    ``region.x + 1``.
    """
    if row < 1 or col < 1:
        raise ClipError(
            f"the terminal numbers cells from 1, so {row};{col} is not a cell"
        )
    return f"\x1b[{row};{col}H".encode("ascii")


def background_sgr(red: int, green: int, blue: int) -> bytes:
    """``ESC [ 48 ; 2 ; r ; g ; b m`` - set the background to a 24-bit colour.

    Used by the erase: spaces in the theme background, as yazi does it, rather
    than spaces in whatever colour the image left behind.
    """
    for value in (red, green, blue):
        if value < 0 or value > 255:
            raise ClipError(f"a colour channel is 0-255, not {value}")
    return f"\x1b[48;2;{red};{green};{blue}m".encode("ascii")


def sixel_or_art_reason() -> str:
    """One line saying which renderer will be used, and why if it is not sixel.

    Either ``"sixel"`` or ``"half-block art: "`` followed by
    :data:`tstudio.gallery.sixel.SIXEL_HINT`. The UI shows this so nobody has
    to wonder whether the blocky picture is the best this terminal can do.
    """
    if sixel_supported():
        return "sixel"
    return "half-block art: " + SIXEL_HINT


def build_image_view():
    """Build and return the ``ImageView`` widget *class* (not an instance).

    The class has two reactives - ``source`` (a ``Path``) and ``at`` (the
    timestamp in seconds for a video frame) - and re-renders when either
    changes. It never raises into the UI: a ``ClipError`` from rendering is
    caught and shown as text in the widget's own cells.
    """
    from textual import work
    from textual.app import RenderResult
    from textual.reactive import reactive
    from textual.widget import Widget
    from textual.worker import get_current_worker

    from rich.text import Text

    class ImageView(Widget):
        """Pixels where the cells are: a sixel image painted over its region.

        The widget itself draws nothing but blank padding - it is a reserved
        rectangle. The picture lives one layer below Textual, written straight
        to the terminal, and is kept in step with the rectangle by hand.
        """

        DEFAULT_CSS = """
        ImageView {
            width: 1fr;
            height: 1fr;
        }
        """

        source: reactive[Path | None] = reactive(None)
        at: reactive[float | None] = reactive(None)

        def __init__(self, source: Path | None = None, *,
                     at: float | None = None, **kwargs) -> None:
            super().__init__(**kwargs)
            #: The sixel bytes currently shown on the terminal, if any.
            self._data: bytes | None = None
            #: The message to display instead of a picture, if rendering failed.
            self._error: str = ""
            #: Bumped on every new request so a late worker's bytes are dropped.
            self._token: int = 0
            #: True once bytes have been written and not yet erased.
            self._painted: bool = False
            #: The rectangle the image was last painted into, in cells, as
            #: (x, y, width, height). Remembered rather than re-read, because
            #: by the time the image needs erasing the widget may have been
            #: hidden or moved and its own region no longer says where the
            #: pixels are. yazi keeps this for the same reason.
            self._shown: tuple[int, int, int, int] | None = None
            #: Half-block art cache: (source, at, cols, rows) -> ANSI rows.
            self._art_key: tuple[object, ...] | None = None
            self._art: list[str] = []
            self.set_reactive(ImageView.source, source)
            self.set_reactive(ImageView.at, at)

        # ------------------------------------------------------------------
        # What Textual draws: nothing, or the fallback art.
        # ------------------------------------------------------------------

        def render(self) -> RenderResult:
            """Blank padding of the widget's own size - or the fallback art.

            Blank, because the sixel image is painted over these cells and
            anything drawn here would fight it. When the terminal cannot do
            sixel there is no image to fight, so the half-block art goes here
            instead and the widget still shows a picture.
            """
            width = max(self.size.width, 0)
            height = max(self.size.height, 0)
            if self._error:
                return Text(self._error, no_wrap=False)
            if not sixel_supported():
                return self._art_text(width, height)
            return Text("\n".join([" " * width] * height), no_wrap=True)

        def _art_text(self, cols: int, rows: int) -> RenderResult:
            """The half-block art for the current source, as ANSI-coloured text.

            ``art_for`` is not free, so the result is cached against the source,
            timestamp and cell size; a resize or a new source recomputes it and
            nothing else does.
            """
            if self.source is None or cols < 1 or rows < 1:
                return Text("")
            key = (str(self.source), self.at, cols, rows)
            if key != self._art_key:
                self._art_key = key
                try:
                    self._art = list(art_for(self.source, cols, rows, at=self.at))
                except ClipError as exc:
                    # Shown as text, and remembered, so the next render does
                    # not retry a thumbnail that has already proved impossible.
                    self._art = []
                    self._error = str(exc)
                    return Text(self._error, no_wrap=False)
            return Text.from_ansi("\n".join(self._art))

        # ------------------------------------------------------------------
        # The sixel layer: render in a thread, paint and erase by hand.
        # ------------------------------------------------------------------

        @work(thread=True, exclusive=True, group="sixel")
        def _render_sixel_worker(self, source: Path, at: float | None,
                                 cols: int, rows: int, token: int) -> None:
            """Encode ``source`` as sixel off the event loop, then hand it back.

            ImageMagick takes seconds, which is several lifetimes for an event
            loop, so this is the one place that calls ``render_sixel``. The
            bytes cross back to the UI thread through ``call_from_thread``;
            nothing is written to the terminal from here.
            """
            worker = get_current_worker()
            try:
                width_px, height_px = sixel_size_for(cols, rows)
                data = render_sixel(source, width_px, height_px)
            except ClipError as exc:
                if not worker.is_cancelled:
                    self._hand_back(self._failed, token, str(exc))
                return
            if worker.is_cancelled:
                return
            self._hand_back(self._ready, token, data)

        def _hand_back(self, callback, *args) -> None:
            """``call_from_thread(callback, *args)``, tolerating a closing app.

            The app can be on its way out by the time a 2-second render
            finishes; that is not an error worth propagating into a worker
            thread, where nothing could be done about it anyway.
            """
            try:
                self.app.call_from_thread(callback, *args)
            except (RuntimeError, AttributeError, ClipError):
                return

        def _ready(self, token: int, data: bytes) -> None:
            """UI thread: adopt freshly rendered bytes and show them."""
            if token != self._token:
                return  # a newer request has already superseded this one
            self._data = data
            self._error = ""
            self.refresh()
            self.repaint()

        def _failed(self, token: int, message: str) -> None:
            """UI thread: show why there is no picture, in the widget's cells."""
            if token != self._token:
                return
            self._erase()
            self._data = None
            self._error = message
            self.refresh()

        def _paint(self) -> None:
            """Write the sixel bytes at the top-left cell of the region.

            yazi's ``image_show``, cell for cell: park the cursor on the
            rectangle, write the bytes, put the cursor back. Only ever while
            the widget is actually on screen with a rectangle to fill.
            """
            data = self._data
            if not data or not self._is_showing():
                return
            region = self.region
            try:
                write_sixel(SAVE_CURSOR + move_to(region.y + 1, region.x + 1))
                write_sixel(data)
                write_sixel(RESTORE_CURSOR)
            except ClipError:
                # The terminal would not take the bytes. The reserved cells
                # stay blank, which is ugly but alive; never raise into the UI.
                self._painted = False
                return
            self._painted = True
            self._shown = (region.x, region.y, region.width, region.height)

        def _erase(self) -> None:
            """Cover the region with background spaces, row by row.

            yazi's ``image_erase``. One write per row, each one a cursor move
            to that row's first cell followed by ``width`` spaces, so the image
            is gone whether or not Textual gets round to repainting those
            cells. The rectangle is the one the image was painted into, which
            is not always the one the widget occupies now - a hidden widget has
            no region at all, and its pixels would otherwise be stranded on
            screen forever.
            """
            rectangle = self._erase_rectangle()
            if rectangle is None:
                self._painted = False
                return
            left, top, width, height = rectangle
            blanks = b" " * width
            colour = self._background_bytes()
            try:
                write_sixel(SAVE_CURSOR)
                if colour:
                    write_sixel(colour)
                for row in range(height):
                    write_sixel(move_to(top + 1 + row, left + 1) + blanks)
                if colour:
                    write_sixel(RESET_SGR)
                write_sixel(RESTORE_CURSOR)
            except ClipError:
                pass
            self._painted = False
            self._shown = None

        def _erase_rectangle(self) -> tuple[int, int, int, int] | None:
            """Where the pixels are: the painted rectangle, else the current one."""
            if self._shown is not None:
                return self._shown
            if not self._has_region():
                return None
            region = self.region
            return (region.x, region.y, region.width, region.height)

        def _background_bytes(self) -> bytes:
            """The theme background as an SGR sequence, or empty if unknowable."""
            try:
                colour = self.background_colors[1]
                return background_sgr(*colour.rgb)
            except (AttributeError, IndexError, TypeError, ValueError, ClipError):
                return b""

        # ------------------------------------------------------------------
        # Keeping the two layers in step.
        # ------------------------------------------------------------------

        def repaint(self) -> None:
            """Erase and redraw the image. Call this after the app refreshes.

            Textual repaints the cells under the image whenever it feels like
            it, and it has no idea an image is there. Anything that triggers a
            screen refresh should follow it with this.
            """
            self._erase()
            self._paint()

        def _is_showing(self) -> bool:
            """True when the widget is on screen with cells to paint over."""
            if not self.is_mounted or not self.display or not self.visible:
                return False
            return self._has_region()

        def _has_region(self) -> bool:
            """True when the widget's rectangle has a non-zero width and height."""
            if not self.is_mounted:
                return False
            region = self.region
            return region.width > 0 and region.height > 0

        def _start_render(self) -> None:
            """Ask for the current source at the current size, or clear up.

            Every call invalidates whatever the previous one asked for: the
            worker is ``exclusive``, so Textual cancels the running thread, and
            the bumped token means its bytes are ignored even if it is already
            past the point of no return inside ImageMagick.
            """
            self._token += 1
            self._erase()
            self._data = None
            self._error = ""
            if not sixel_supported():
                self.refresh()
                return
            self.refresh()
            if self.source is None or not self._has_region():
                return
            self._render_sixel_worker(
                self.source, self.at, self.region.width, self.region.height,
                self._token,
            )

        def watch_source(self, _old: Path | None, _new: Path | None) -> None:
            """A new file means a new picture."""
            if self.is_mounted:
                self._start_render()

        def watch_at(self, _old: float | None, _new: float | None) -> None:
            """A new timestamp means a different frame of the same file."""
            if self.is_mounted:
                self._start_render()

        def on_mount(self) -> None:
            self._start_render()

        def on_resize(self) -> None:
            """A different rectangle needs differently sized pixels."""
            self._start_render()

        def on_show(self) -> None:
            """Back on screen: the terminal has forgotten the image, so redraw."""
            if self._data:
                self.repaint()
            else:
                self._start_render()

        def on_hide(self) -> None:
            """Off screen: take the image with us, or it hangs over whatever is next."""
            self._erase()

        def on_unmount(self) -> None:
            self._erase()

    return ImageView
