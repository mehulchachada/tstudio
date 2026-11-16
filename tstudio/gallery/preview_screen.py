"""The "view this clip" dialog: a real look at a frame before cutting it.

Pressing Preview in the editor used to print an ffmpeg command, which is a
preview of nothing. This is the dialog that replaces it - a large amber-framed
modal holding a coloured frame of the clip, a caption with the timestamp and
the clip's in/out points, and arrow keys that step through ten evenly spaced
moments so the user can actually look at what they are about to cut.

Two things shape the code more than anything else:

* `art_for` shells out to ffmpeg and ImageMagick and takes between half a
  second and two and a half seconds. It must never run on the event loop, so
  every render goes through an exclusive thread worker and comes back via
  `call_from_thread`. Holding an arrow key therefore cancels superseded
  renders instead of queueing a second of lag per keypress.
* Textual is an optional dependency, so the dialog is produced by a builder
  function that imports textual on the inside. Nothing at this module's top
  level touches textual - `tstudio/gallery/screens.py` sets the pattern and a
  test enforces it.

The module-level imports of `art_for`, `probe`, `mpv_available` and
`play_with_mpv` are deliberate: the dialog calls them through this module's
globals, which is also where the tests patch them so nothing shells out.
"""

from __future__ import annotations

from pathlib import Path

from tstudio import ClipError
from tstudio.gallery import branding
from tstudio.gallery.playback import (
    MPV_HINT,
    mpv_available,
    play_with_mpv,
    plays_in_terminal,
    scrub_timestamps,
)
from tstudio.gallery.thumbs import art_for
from tstudio.media import probe
from tstudio.timespec import format_time

# How much of the terminal the dialog takes. Big enough that the frame reads
# as a picture rather than a postage stamp, small enough to still be a modal.
DIALOG_PERCENT = 80

# Rows inside the dialog that are not the frame: a blank spacer, the caption
# and the key hints. The frame gets everything else.
RESERVED_ROWS = 3

# ImageMagick will happily render a 3x1 frame; nobody can read one. The frame
# never goes below this, even in a terminal too small to hold it.
MIN_FRAME_COLS = 20
MIN_FRAME_ROWS = 8

# How many moments to offer. `scrub_timestamps` spaces them evenly and keeps
# them strictly inside the duration, so the last one is never a black frame
# past the end of the stream.
SCRUB_STEPS = 10

LOADING_TEXT = "loading…"
KEY_HINTS = "←/→ scrub · home/end ends · p play · q close"

#: Shown after playing when this terminal cannot draw video and mpv had to
#: open its own window - otherwise nothing on screen changes and it looks
#: as though "p" did nothing at all.
WINDOW_HINT = "playing in an mpv window · this terminal cannot show video inline"


def _preview_css(selector: str) -> str:
    """The dialog's CSS, with every brand colour substituted in exactly once.

    `selector` is the screen class's own type name: Textual scopes a widget's
    DEFAULT_CSS to that type, so the rule that centres and dims the screen has
    to name it rather than the `ModalScreen` base.
    """
    return f"""
    {selector} {{
        align: center middle;
        /* Dim whatever is behind the dialog, as the other modals do. */
        background: black 60%;
    }}
    {selector} #dialog {{
        width: {DIALOG_PERCENT}%;
        height: {DIALOG_PERCENT}%;
        padding: 1 2;
        background: $surface;
        border: round {branding.AMBER};
        border-title-color: {branding.AMBER};
    }}
    {selector} #frame {{
        width: 1fr;
        height: 1fr;
        /* Frames arrive as ANSI, so this colour only shows on the
           loading line and on an error. */
        color: {branding.GREY};
        overflow: hidden;
    }}
    {selector} #frame.error {{
        color: {branding.RED};
        text-style: bold;
    }}
    {selector} #caption {{
        height: 1;
        margin-top: 1;
        color: {branding.GREEN};
    }}
    {selector} #hints {{
        height: 1;
        color: {branding.VIOLET};
    }}
    """


def _truncate(text: str, width: int) -> str:
    """`text` cut to `width` cells with an ellipsis, for the border title."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def build_preview_screen(path: Path, *, start: float | None = None,
                         end: float | None = None):
    """Build the clip preview dialog for `path` - a `ModalScreen[None]`.

    `start`/`end` are the editor's in and out points in seconds. When both are
    given the dialog scrubs *between* them rather than across the whole file,
    because the point is to preview the clip and not its source, and the
    caption shows the bounds so they stay visible while scrubbing.

    Dismisses `None`, on Escape or `q` - there is nothing to decide here.
    """
    clip_path = Path(path)

    from textual import work
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.screen import ModalScreen
    from textual.widgets import Static

    from rich.text import Text

    class PreviewScreen(ModalScreen[None]):
        """A frame of the clip over a dimmed screen, scrubbable with arrows."""

        DEFAULT_CSS = _preview_css("PreviewScreen")
        BINDINGS = [
            Binding("escape", "close", "Close", priority=True),
            Binding("q", "close", "Close"),
            Binding("left", "step(-1)", "Back"),
            Binding("right", "step(1)", "Forward"),
            Binding("h", "step(-1)", "Back", show=False),
            Binding("l", "step(1)", "Forward", show=False),
            Binding("home", "first", "First"),
            Binding("end", "last", "Last"),
            Binding("p", "play", "Play"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.path = clip_path
            self.start = start
            self.end = end
            # Absolute timestamps into the file, already offset by `start`.
            self.timestamps: list[float] = []
            self.index = 0
            # The size the last render was asked for, so a resize can tell
            # whether it actually needs to spend two seconds on ffmpeg.
            self._rendered_for: tuple[int, int, int] | None = None
            self.error: str | None = None
            self.hint: str = KEY_HINTS

        # -- geometry ---------------------------------------------------

        def frame_size(self) -> tuple[int, int]:
            """The frame's size in cells, from the dialog's own content box."""
            try:
                dialog = self.query_one("#dialog", Vertical)
            except Exception:
                return MIN_FRAME_COLS, MIN_FRAME_ROWS
            cols = max(MIN_FRAME_COLS, int(dialog.size.width))
            rows = max(MIN_FRAME_ROWS, int(dialog.size.height) - RESERVED_ROWS)
            return cols, rows

        # -- scrub points -----------------------------------------------

        def _bounds(self) -> tuple[float, float]:
            """The window to scrub over, as (offset, end) seconds.

            Both points given means no probe is needed at all, which is the
            common case from the editor and keeps the dialog instant.
            """
            if self.start is not None and self.end is not None:
                return float(self.start), float(self.end)
            total = float(probe(self.path).duration)
            low = 0.0 if self.start is None else float(self.start)
            high = total if self.end is None else float(self.end)
            return low, high

        def _build_timestamps(self) -> None:
            low, high = self._bounds()
            span = max(0.0, high - low)
            self.timestamps = [low + offset
                               for offset in scrub_timestamps(span, SCRUB_STEPS)]
            if not self.timestamps:  # a zero-length clip still has one frame
                self.timestamps = [low]
            self.index = 0

        @property
        def timestamp(self) -> float:
            """The timestamp currently on screen."""
            if not self.timestamps:
                return 0.0
            return self.timestamps[self.index]

        # -- composition ------------------------------------------------

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Static(LOADING_TEXT, id="frame", markup=False)
                yield Static("", id="caption", markup=False)
                yield Static(KEY_HINTS, id="hints", markup=False)

        def on_mount(self) -> None:
            dialog = self.query_one("#dialog", Vertical)
            dialog.border_title = self.path.name
            try:
                self._build_timestamps()
            except ClipError as exc:
                self._show_error(str(exc))
                return
            self._write_caption()
            # Percentage sizing is only real after a layout pass, so the
            # first render waits for one rather than guessing at the size.
            self.call_after_refresh(self._refit)

        def on_resize(self, event) -> None:  # noqa: ANN001 - Textual event
            self.call_after_refresh(self._refit)

        def _refit(self) -> None:
            """Fit the border title to the dialog and render if the size moved."""
            if self.error is not None and not self.timestamps:
                return
            try:
                dialog = self.query_one("#dialog", Vertical)
            except Exception:
                return
            # The title sits in the top border, between its two corners.
            dialog.border_title = _truncate(self.path.name,
                                            max(0, int(dialog.size.width)))
            cols, rows = self.frame_size()
            if self._rendered_for == (self.index, cols, rows):
                return
            self._start_render()

        # -- rendering --------------------------------------------------

        def _start_render(self) -> None:
            if not self.timestamps:
                return
            cols, rows = self.frame_size()
            self._rendered_for = (self.index, cols, rows)
            self._write_caption()
            frame = self.query_one("#frame", Static)
            frame.remove_class("error")
            frame.update(LOADING_TEXT)
            self._render_frame(self.timestamp, cols, rows)

        @work(thread=True, exclusive=True, group="preview-frame")
        def _render_frame(self, at: float, cols: int, rows: int) -> None:
            """Shell out to ffmpeg off the event loop; newest render wins.

            `exclusive` means holding an arrow key cancels the renders it
            overtook instead of queueing a couple of seconds of lag each.
            """
            try:
                art = art_for(self.path, cols, rows, at=at)
            except ClipError as exc:
                self.app.call_from_thread(self._show_error, str(exc))
                return
            except Exception as exc:  # a broken tool must not kill the app
                self.app.call_from_thread(
                    self._show_error, f"could not render this frame: {exc}")
                return
            self.app.call_from_thread(self._show_frame, list(art))

        def _show_frame(self, art: list[str]) -> None:
            self.error = None
            frame = self.query_one("#frame", Static)
            frame.remove_class("error")
            frame.update(Text.from_ansi("\n".join(art), no_wrap=True,
                                        overflow="crop"))
            self._write_caption()

        def _show_error(self, message: str) -> None:
            """Put a ClipError in the dialog, in brand red. Never crash."""
            self.error = message
            frame = self.query_one("#frame", Static)
            frame.add_class("error")
            frame.update(message)
            self._write_caption()

        def _write_caption(self) -> None:
            parts = [format_time(self.timestamp)]
            if self.timestamps:
                parts.append(f"{self.index + 1}/{len(self.timestamps)}")
            if self.start is not None and self.end is not None:
                parts.append(f"in {format_time(float(self.start))}")
                parts.append(f"out {format_time(float(self.end))}")
            self.query_one("#caption", Static).update(" · ".join(parts))
            self.query_one("#hints", Static).update(self.hint)

        def _set_hint(self, hint: str) -> None:
            self.hint = hint
            self.query_one("#hints", Static).update(hint)

        # -- actions ----------------------------------------------------

        def _goto(self, index: int) -> None:
            if not self.timestamps:
                return
            # Clamped, so leaning on an arrow key parks at an end rather
            # than walking off the list.
            self.index = max(0, min(index, len(self.timestamps) - 1))
            self._start_render()

        def action_step(self, delta: int) -> None:
            self._goto(self.index + delta)

        def action_first(self) -> None:
            self._goto(0)

        def action_last(self) -> None:
            self._goto(len(self.timestamps) - 1)

        def action_play(self) -> None:
            """Hand the clip to mpv, or say how to get mpv. Never fail."""
            if not mpv_available():
                self._set_hint(MPV_HINT)
                return
            in_terminal = plays_in_terminal()
            try:
                # With a terminal video output mpv takes over the terminal, so
                # Textual has to let go of it. Suspending costs nothing in the
                # windowed case, where mpv still reads keys from this terminal.
                with self.app.suspend():
                    play_with_mpv(self.path)
            except ClipError as exc:
                self._show_error(str(exc))
                return
            # Say where it went. A window opening somewhere else is a surprise
            # worth one line, and it is the only clue that this terminal
            # cannot draw video - which is otherwise indistinguishable from
            # the clip simply having no picture.
            self._set_hint(KEY_HINTS if in_terminal else WINDOW_HINT)

        def action_close(self) -> None:
            self.dismiss(None)

    return PreviewScreen()
