"""An optional Textual TUI: a precision single-clip editor over the clip backend.

This is a thin front-end over the same zero-dependency core the CLI uses
(`probe`, `plan_from_recipe`, `run_cut` / `run_convert`). It is NOT required
for the CLI to work.

Run it with a video file:

    python -m tstudio video.mp4 --tui
    python -m tstudio video.mp4 0:12 0:45 --tui      # pre-fill the range
    python -m tstudio video.mp4 --out clip.mp4 --tui

The screen is one list of what the output should *differ in* from the source:

    CLIP    the range, as one field
    PRESET  a starting set of values for the four below it - never a lock
    SIZE    SHAPE
    COLOUR  FPS
    AUDIO

There is deliberately no "task" or "mode" control. "Lossless" is not a thing
anyone picks; it is simply what is left when size, shape, colour and audio are
all "keep", and `recipe.is_lossless` says so. Picking a preset fills the four
controls in and leaves them editable, which is the only way "Shorts, but keep
HDR10" can be expressed at all.

Textual is an optional dependency. If it is not installed, this prints a hint
and exits; the zero-dependency CLI is unaffected.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from tstudio import ClipError
from tstudio.media import MediaInfo, probe

# Reuse the exact formatting helpers the CLI already ships, so the TUI shows
# the same numbers the user sees from `tstudio --info`.
from tstudio.cli import _ENCODERS, _fmt_size, _header_lines


# Width of the label column in every control row. One number so CLIP, PRESET,
# SIZE, COLOUR and AUDIO are the same width and the fields they introduce begin
# at the same column all the way down the screen.
LABEL_WIDTH = 10

# One glyph per action, each exactly one cell wide (rich.cells.cell_len == 1).
# A two-cell icon would shift the text after it and break the column, so
# tests/test_tui.py guards this table.
BUTTON_ICONS = {
    "export": "▶",   # the primary action, whatever the recipe turns out to be
    "play": "♪",     # play what was written, once there is something to play
}

# One glyph per preset, same one-cell rule. "none" leads the list and is the
# default: most clips are not going anywhere near a social platform.
PRESET_ICONS = {
    "none": "·",
    "shorts": "▶",
    "reels": "◉",
    "tiktok": "♪",
    "x": "✕",
    "square": "■",
}

#: What the one CLIP field accepts. Shown next to the ⓘ when the field is
#: focused, so the formats are discoverable without a manual.
RANGE_HELP = (
    "3:49-4:19  ·  3:49 (to the end)  ·  first 20%  ·  "
    "last 45s  ·  around 4:00  ·  blank = whole file"
)

#: The separator between facts on one line, matching the CLI's own.
DOT = "  ·  "


def _fmt_command(command: list[str]) -> str:
    """Join an argv list into one copy-pasteable line, quoting space-containing args."""
    parts: list[str] = []
    for arg in command:
        if " " in arg and not arg.startswith('"'):
            parts.append(f'"{arg}"')
        else:
            parts.append(arg)
    return " ".join(parts)


def initial_clip_text(start_text: str, end_text: str) -> str:
    """Fold the CLI's separate START and END into the one CLIP field.

    ``("3:49", "4:19") -> "3:49-4:19"``; ``("3:49", "") -> "3:49"``; an empty
    start with an end is still a range from the beginning, ``"0-4:19"``, because
    ``parse_range`` requires a start. Both empty gives an empty field, which
    means the whole file.
    """
    start, end = start_text.strip(), end_text.strip()
    if not start and not end:
        return ""
    if not end:
        return start
    return f"{start or '0'}-{end}"


def _usage() -> str:
    return (
        "usage: python -m tstudio VIDEO [START] [END] [--out PATH] --tui\n"
        "       START/END: 'mm:ss' (e.g. 0:12) or seconds (e.g. 12)\n"
        "       relative ranges also work in the UI: 'first 10s', 'around 4:00'"
    )


def _run(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    out_path: Path | None = None
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--out", "-o"):
            if i + 1 >= len(args):
                print("tstudio: --out needs a path", file=sys.stderr)
                return 2
            out_path = Path(args[i + 1])
            i += 2
        elif a in ("-h", "--help"):
            print(_usage())
            return 0
        elif a.startswith("-"):
            print(f"tstudio: unknown option {a}\n", file=sys.stderr)
            print(_usage(), file=sys.stderr)
            return 2
        else:
            positional.append(a)
            i += 1

    if not (1 <= len(positional) <= 3):
        print("tstudio: expected a video file, optional START and END\n", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2

    video = Path(positional[0])
    start_text = positional[1] if len(positional) > 1 else ""
    end_text = positional[2] if len(positional) > 2 else ""

    try:
        from tstudio.media import probe
        info = probe(video)
    except ClipError as exc:
        print(f"tstudio: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"tstudio: {exc}", file=sys.stderr)
        return 1

    try:
        app = _build_app(info, start_text, end_text, out_path)
    except ImportError:
        print(
            "The clip TUI needs the 'textual' package (an optional dependency).\n"
            "Install it with:  pip install textual\n"
            "The zero-dependency CLI is unchanged:  python -m tstudio <video> <range>\n"
        )
        return 1

    try:
        app.run()
    except KeyboardInterrupt:
        return 130
    return 0


def _build_app(info: MediaInfo, start_text: str, end_text: str,
               out_path: Path | None):
    """Build the ClipApp. Imports textual here so the CLI never needs it."""
    import time

    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.widgets import (
        Button,
        Footer,
        Input,
        Label,
        Select,
        Static,
    )

    from rich.text import Text

    from tstudio.convert import TARGETS, run_convert
    from tstudio.cut import run_cut
    from tstudio.media import encoders_available, is_hdr, keyframe_at_or_before
    from tstudio.presets import PRESETS, duration_warning
    from tstudio.recipe import (
        Recipe,
        describe,
        is_lossless,
        plan_from_recipe,
        recipe_for_preset,
    )
    from tstudio.timespec import (
        format_time,
        output_name,
        output_name_tagged,
        parse_range,
        resolve_relative,
        unique_name,
    )
    from tstudio.gallery import branding
    from tstudio.gallery import progress as progress_art

    available = encoders_available()
    encoders = ["auto"] + [e for e in _ENCODERS if e != "auto" and e in available]

    video = info.video
    source_is_hdr = is_hdr(video)
    source_fps = video.fps if video is not None else 0.0

    # ---- the option lists, decided once from the source -------------------
    # A choice that cannot do anything is not offered. An SDR source has no
    # HDR to keep or to tone-map away, and a 60fps source cannot be capped at
    # 120, so neither appears.

    preset_options = [(f"{PRESET_ICONS['none']}  no preset", "none")] + [
        (f"{PRESET_ICONS.get(key, '·')}  {preset.label}", key)
        for key, preset in PRESETS.items()
    ]

    size_options = [("keep", "keep")] + [(key, key) for key in TARGETS]

    shape_options: list[tuple[str, str]] = [("keep", "keep")]
    for ratio in ("9:16", "1:1", "16:9"):
        for fit in ("crop", "pad"):
            shape_options.append((f"{ratio}  {fit}", f"{ratio}|{fit}"))

    # COLOUR names the colour the output will be, not what is being done to
    # the source: "HDR10" and "SDR", the only two answers there are. The old
    # labels ("keep HDR10", "SDR  (tone-map)") described the operation, which
    # made the two read as different kinds of thing rather than as a pair.
    # An SDR source still gets one entry, because HDR cannot be invented -
    # but it is now "SDR" rather than "keep", so the control says what you
    # will get whichever file you opened.
    if source_is_hdr:
        colour_options = [("HDR10", "keep"), ("SDR", "sdr")]
    else:
        colour_options = [("SDR", "keep")]

    # "source" rather than "" for the uncapped option: Textual's Select treats
    # a blank specially (its BLANK sentinel), and an empty-string value in a
    # list built with allow_blank=False is asking for the two to be confused.
    fps_options: list[tuple[str, str]] = [(f"source  ({source_fps:g}fps)", "source")]
    for cap in (120, 60, 30, 24):
        if cap < source_fps - 0.01:
            fps_options.append((f"{cap} capped", str(cap)))

    audio_options = [("keep", "keep"), ("drop", "drop")]

    class ClipApp(App):
        TITLE = branding.NAME
        SUB_TITLE = branding.TAGLINE

        CSS = f"""
        /* ------------------------------------------------------------------
           No boxes. Groups are separated by one blank row and never split
           inside; the only rules on the screen are the single accent bars
           beside the readout and the command preview.

           Two columns hold the whole screen together:
             column 4  - every group's text starts here (screen padding 1 +
                         group padding 3, or 1 + accent bar 1 + padding 2)
             column 14 - every field starts here (column 4 + LABEL_WIDTH)
           Change a padding below and you move one of those two columns, so
           change them in pairs.
           ------------------------------------------------------------------ */
        Screen {{
            padding: 0 1;
        }}

        /* ---- the identity block and the file it is about ------------------ */
        #logo {{
            height: 3;
            padding: 0 0 0 3;
            color: {branding.AMBER};
        }}
        #file_name {{
            height: 1;
            padding: 0 2 0 3;
            color: {branding.AMBER};
        }}
        #file_facts {{
            height: 1;
            padding: 0 2 0 3;
            margin-bottom: 1;
            color: {branding.GREY};
        }}

        /* ---- fields ------------------------------------------------------
           Selects keep their natural height - forcing a row to one cell clips
           a Select's label ("PRESE1") and squashes its dropdown - so they are
           lined up by column instead, and the rhythm comes from margin-bottom
           on the groups.

           Focus is carried by background + colour, never by a border: in
           textual 8.2.8 Styles.ANIMATABLE has background, background_tint,
           color and opacity but NOT border, so a border change would snap.
           ------------------------------------------------------------------ */
        Input {{
            height: 1;
            border: none;
            padding: 0 2;
            background: $boost;
            transition: background 140ms, color 140ms;
        }}
        Input:hover {{
            background: {branding.AMBER} 8%;
        }}
        Input:focus {{
            background: {branding.AMBER} 18%;
            background-tint: {branding.AMBER} 12%;
            color: {branding.AMBER};
        }}
        Input:disabled {{
            background: $boost;
            color: {branding.GREY};
        }}
        /* padding 1 inside the Select's own border puts its value in the
           same column as an Input's text (padding 2, no border). */
        Select SelectCurrent {{
            padding: 0 1;
            background: $boost;
            transition: background 140ms, color 140ms;
        }}
        Select:hover SelectCurrent {{
            background: {branding.AMBER} 8%;
        }}
        Select:focus SelectCurrent {{
            background: {branding.AMBER} 18%;
            background-tint: {branding.AMBER} 12%;
            color: {branding.AMBER};
        }}
        Select:disabled SelectCurrent {{
            background: $boost;
            color: {branding.GREY};
        }}

        /* ---- rows and groups --------------------------------------------- */
        /* One width for every label, so the fields start in one column; full
           row height + middle alignment so a label sits on the same line as
           the value beside it, whether that row is one cell or three. */
        .ctl_label {{
            width: {LABEL_WIDTH};
            height: 100%;
            content-align: left middle;
            padding: 0;
            color: {branding.GREY};
            text-style: bold;
        }}
        .ctl_row {{
            height: auto;
            align: left middle;
        }}
        #controls {{
            height: auto;
            padding: 0 2 0 3;
            margin-bottom: 1;
        }}
        #clip {{
            width: 28;
        }}
        /* The ⓘ and the resolved length ride on the CLIP row, so they need the
           same full-height middle alignment the labels have. */
        #clip_info {{
            width: 4;
            height: 100%;
            content-align: center middle;
            color: {branding.AMBER};
        }}
        #clip_len {{
            width: 1fr;
            height: 100%;
            content-align: left middle;
            color: {branding.GREY};
        }}
        #preset {{
            width: 38;
        }}
        #preset_note {{
            width: 1fr;
            height: 100%;
            content-align: left middle;
            color: {branding.VIOLET};
        }}
        .half {{
            width: 26;
        }}
        #quality {{
            width: 14;
        }}

        /* ---- the readout, with its one accent bar ------------------------
           Green while the plan is good, red the moment it is not; the .error
           class is toggled in _refresh(). The bar is one column, so padding
           here is 2 where a plain group uses 3 - same text column. */
        #readout_panel {{
            height: auto;
            max-height: 5;
            border-left: thick {branding.GREEN};
            padding: 0 2;
        }}
        #readout_panel.error {{
            border-left: thick {branding.RED};
        }}
        #readout {{
            height: auto;
            max-height: 4;
            padding: 0;
        }}
        #why {{
            height: auto;
            max-height: 2;
            padding: 0 2 0 3;
            margin-bottom: 1;
            color: {branding.GREY};
        }}
        #preview_panel {{
            height: auto;
            border-left: thick {branding.GREY};
            padding: 0 2;
            margin-bottom: 1;
        }}
        #preview {{
            height: auto;
            padding: 0;
        }}

        /* ---- buttons -----------------------------------------------------
           One family: same height, same width floor, same gap, no border.
           A Button already draws its own frame, so border: round on top of it
           gives a double outline with broken corners - that was the single
           thing that made this row look unfinished. The primary action is the
           .primary class rather than a variant, so the set stays one species
           and only the fill tells you which one acts.
           ------------------------------------------------------------------ */
        #row_buttons {{
            height: 3;
            align: left middle;
            padding: 0 0 0 3;
            margin-bottom: 1;
        }}
        Button {{
            border: none;
            height: 3;
            min-width: 17;
            margin-right: 2;
            background: $boost;
            color: {branding.AMBER};
            transition: background 140ms, background-tint 140ms;
        }}
        Button.primary {{
            background: {branding.GREEN} 22%;
            color: {branding.GREEN};
            text-style: bold;
        }}
        /* The lift is written as an alpha of the brand colour, not as
           $boost-lighten-2: $boost is translucent white, and lightening white
           returns white, so that rule measures as no change at all. */
        Button:hover {{
            background: {branding.AMBER} 12%;
        }}
        Button.primary:hover {{
            background: {branding.GREEN} 34%;
        }}
        Button:focus {{
            background-tint: {branding.AMBER} 18%;
        }}
        Button:disabled {{
            background: $boost;
            color: {branding.GREY};
        }}

        /* ---- progress and status: one group, no blank row between them ---
           Two lines from tstudio.gallery.progress: a spinner with a sub-cell
           bar, and a breathing lens with a rotating line. */
        #bar {{
            height: 2;
            padding: 0 2 0 3;
            color: {branding.GREEN};
        }}
        #status {{
            height: 1;
            padding: 0 2 0 3;
        }}
        """

        BINDINGS = [
            Binding("f5", "cut", "Export", priority=True),
            Binding("f2", "preview_clip", "Preview", priority=True),
            Binding("f7", "toggle_preview", "Show cmd", priority=True),
            Binding("f3", "reset_inputs", "Reset", priority=True),
            Binding("f4", "copy_command", "Copy cmd", priority=True),
            Binding("f6", "play_output", "Play result", priority=True),
            # Escape is the way out. The editor is nearly always reached from
            # the gallery, which runs it as a child process and is sitting
            # there waiting - so leaving the editor IS going back, and the
            # footer should say so rather than leaving no visible way out at
            # all. Run on its own, it exits to the shell, which is the same
            # gesture meaning the same thing one level up.
            Binding("escape", "quit", "Back", priority=True),
            Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        ]

        def __init__(self, info: MediaInfo, start_text: str, end_text: str,
                     out_path: Path | None) -> None:
            super().__init__()
            self.info = info
            self.clip_text = initial_clip_text(start_text, end_text)
            self.out_path = out_path
            self._last_plan = None
            self._last_output = None
            # True while the code - not the user - is setting a Select's value,
            # so that filling the controls in from a preset does not look like
            # five manual edits and immediately mark itself "modified".
            self._syncing = False
            # Progress animation state. _tick drives the spinner; _fraction is
            # written from the ffmpeg thread and read by the timer.
            self._tick = 0
            self._fraction = 0.0
            self._started_at = 0.0
            self._progress_timer = None

        # -- layout -----------------------------------------------------------
        def compose(self) -> ComposeResult:
            header = _header_lines(self.info)
            yield Static("\n".join(branding.header_lines(100)), id="logo")
            yield Static(header[0] if header else self.info.path.name, id="file_name")
            yield Static(header[1] if len(header) > 1 else "", id="file_facts")

            with Vertical(id="controls"):
                with Horizontal(classes="ctl_row"):
                    yield Label("CLIP", classes="ctl_label row_head")
                    clip = Input(value=self.clip_text,
                                 placeholder="3:49-4:19, or blank for the whole file",
                                 id="clip")
                    clip.tooltip = RANGE_HELP
                    yield clip
                    info = Static("?", id="clip_info")
                    info.tooltip = RANGE_HELP
                    yield info
                    yield Static("", id="clip_len")
                with Horizontal(classes="ctl_row"):
                    yield Label("PRESET", classes="ctl_label row_head")
                    yield Select(preset_options, value="none",
                                 allow_blank=False, id="preset")
                    yield Static("", id="preset_note")
                with Horizontal(classes="ctl_row"):
                    yield Label("SIZE", classes="ctl_label row_head")
                    yield Select(size_options, value="keep",
                                 allow_blank=False, classes="half", id="size")
                    yield Label("SHAPE", classes="ctl_label")
                    yield Select(shape_options, value="keep",
                                 allow_blank=False, classes="half", id="shape")
                with Horizontal(classes="ctl_row"):
                    yield Label("COLOUR", classes="ctl_label row_head")
                    yield Select(colour_options, value="keep",
                                 allow_blank=False, classes="half", id="colour")
                    yield Label("FPS", classes="ctl_label")
                    yield Select(fps_options, value="source",
                                 allow_blank=False, classes="half", id="fps")
                with Horizontal(classes="ctl_row"):
                    yield Label("AUDIO", classes="ctl_label row_head")
                    yield Select(audio_options, value="keep",
                                 allow_blank=False, classes="half", id="audio")
                    yield Label("ENCODER", classes="ctl_label", id="encoder_label")
                    yield Select([(e, e) for e in encoders], value="auto",
                                 allow_blank=False, classes="half", id="encoder")
                with Horizontal(classes="ctl_row", id="row_quality"):
                    yield Label("QUALITY", classes="ctl_label row_head")
                    yield Input(placeholder="crf/cq", id="quality")

            with Vertical(id="readout_panel"):
                yield Static("", id="readout")
            yield Static("", id="why")
            with Vertical(id="preview_panel"):
                yield Static("", id="preview")

            # One button does the work, whatever the recipe turns out to be.
            # The second one is not an option you weigh up against it - there
            # is nothing to play until something has been written - so it is
            # absent until there is, and then it is the obvious next thing.
            # Preview (F2) and Copy (F4) stay on the keyboard, where the footer
            # already lists them; as buttons they were competing for attention
            # with the only action most runs need.
            with Horizontal(id="row_buttons"):
                yield Button(f"{BUTTON_ICONS['export']} Export  F5",
                             classes="primary", id="btn_export")
                yield Button(f"{BUTTON_ICONS['play']} Play result  F6",
                             id="btn_play")

            yield Static("", id="bar")
            yield Static("", id="status")
            yield Footer()

        def on_mount(self) -> None:
            self._apply_theme()
            self.query_one("#btn_play", Button).display = False
            self._sync_controls()
            self._refresh()
            self._toggle_preview(False)
            self.query_one("#clip", Input).focus()

        def _apply_theme(self) -> None:
            """Swap Textual's stock blue for the tstudio palette, if it is there.

            The theme module is built separately; until it lands the editor
            still works, it just keeps Textual's blue selection bar. Never let
            a missing or broken theme stop the editor from opening.
            """
            try:
                from tstudio.theme import apply_theme
            except ImportError:
                return
            try:
                apply_theme(self)
            except Exception:  # noqa: BLE001 - cosmetic; never fatal
                return

        # -- reading the controls ---------------------------------------------
        def _range(self) -> tuple[float, float | None]:
            """Resolve the one CLIP field to (start, end|None).

            Absolute ranges go through ``parse_range``; anything it rejects is
            offered to ``resolve_relative``, which is what makes "first 20%" and
            "last 45s" work in the same box. An empty field is the whole file.
            """
            text = self.query_one("#clip", Input).value.strip()
            if not text:
                return 0.0, None
            try:
                return parse_range(text)
            except ClipError:
                return resolve_relative(text, self.info.duration)

        def _recipe(self) -> Recipe:
            """The Recipe the five controls currently describe."""
            shape_value = self.query_one("#shape", Select).value
            if shape_value == "keep":
                shape, fit = "keep", "pad"
            else:
                shape, fit = shape_value.split("|", 1)

            fps_value = self.query_one("#fps", Select).value
            quality_text = self.query_one("#quality", Input).value.strip()

            return Recipe(
                size=self.query_one("#size", Select).value,
                shape=shape,
                fit=fit,
                colour=self.query_one("#colour", Select).value,
                audio=self.query_one("#audio", Select).value,
                fps_cap=None if fps_value == "source" else int(fps_value),
                encoder=self.query_one("#encoder", Select).value,
                quality=int(quality_text) if quality_text.isdigit() else None,
            )

        def _preset_name(self) -> str:
            return self.query_one("#preset", Select).value

        def _destination(self, start: float, end: float | None,
                         recipe: Recipe) -> Path:
            """Where the output goes, named for what was actually done to it."""
            if self.out_path is not None:
                return Path(self.out_path)
            if is_lossless(recipe):
                return output_name(self.info.path, start, end)
            preset = self._preset_name()
            if preset != "none":
                tag = preset
            elif recipe.shape != "keep":
                tag = recipe.shape.replace(":", "-")
            elif recipe.size != "keep":
                tag = recipe.size
            else:
                tag = "converted"
            tagged = output_name_tagged(
                self.info.path,
                start if start > 0.0 else None,
                end,
                tag,
            )
            return unique_name(tagged)

        # -- keeping the controls honest ---------------------------------------
        def _set_select(self, widget_id: str, value: str) -> None:
            """Set a Select's value without it counting as a manual edit."""
            select = self.query_one(f"#{widget_id}", Select)
            if select.value != value:
                select.value = value

        def _apply_preset(self, name: str) -> None:
            """Fill SIZE / SHAPE / COLOUR / FPS in from a preset, and stop there.

            This is the whole difference between a preset that helps and one
            that gets in the way: it writes values into controls that stay
            editable. Setting COLOUR back to "HDR10" afterwards leaves the
            9:16 reframe alone, which is how "Shorts, but keep HDR10" is
            expressed.
            """
            recipe = recipe_for_preset(name)
            self._syncing = True
            try:
                self._set_select("size", recipe.size)
                self._set_select("shape", f"{recipe.shape}|{recipe.fit}")
                if any(value == recipe.colour for _, value in colour_options):
                    self._set_select("colour", recipe.colour)
                cap = str(recipe.fps_cap) if recipe.fps_cap else "source"
                if any(value == cap for _, value in fps_options):
                    self._set_select("fps", cap)
                self._set_select("audio", recipe.audio)
            finally:
                self._syncing = False

        def _preset_drift(self) -> list[str]:
            """Which fields no longer match the chosen preset. Empty when none do.

            A preset is a starting point, so drifting from it is normal and is
            reported rather than prevented. With no preset chosen there is
            nothing to drift from.
            """
            name = self._preset_name()
            if name == "none":
                return []
            try:
                wanted = recipe_for_preset(name)
            except ClipError:
                return []
            current = self._recipe()
            drifted: list[str] = []
            if current.size != wanted.size:
                drifted.append("size")
            if (current.shape, current.fit) != (wanted.shape, wanted.fit):
                drifted.append("shape")
            # COLOUR and FPS only offer what this source can actually do: an
            # SDR source has no HDR to drop, and a 30fps one cannot be capped
            # at 60. A value the control was never able to hold is not
            # something the user changed, so it is not drift.
            colour_values = {value for _, value in colour_options}
            if current.colour != wanted.colour and wanted.colour in colour_values:
                drifted.append("colour")
            fps_values = {value for _, value in fps_options}
            wanted_fps = str(wanted.fps_cap) if wanted.fps_cap else "source"
            if current.fps_cap != wanted.fps_cap and wanted_fps in fps_values:
                drifted.append("fps")
            if current.audio != wanted.audio:
                drifted.append("audio")
            return drifted

        def _sync_controls(self) -> None:
            """Enable what the current recipe can use.

            The action button is deliberately NOT renamed here. It used to read
            Cut / Convert / Export depending on the recipe, which made the one
            fixed thing on the screen move about while saying nothing the
            readout does not already say in full - the readout is literally
            "lossless stream copy" or "re-encode · 1080p · 9:16 pad". One
            button, one name.
            """
            recipe = self._recipe()
            lossless = is_lossless(recipe)

            # Encoder and quality configure a re-encode. With nothing to
            # re-encode they are not "off", they are simply not about anything.
            self.query_one("#encoder", Select).disabled = lossless
            self.query_one("#quality", Input).disabled = lossless

            drifted = self._preset_drift()
            note = self.query_one("#preset_note", Static)
            if drifted:
                note.update(Text(f"modified: {', '.join(drifted)}",
                                 style=branding.VIOLET))
            else:
                note.update(Text(""))

        def _toggle_preview(self, visible: bool | None = None) -> None:
            """Show/hide the command preview panel (toggle if visible is None)."""
            panel = self.query_one("#preview_panel")
            if visible is None:
                visible = not panel.display
            panel.display = visible

        # -- the live readout ---------------------------------------------------
        def _refresh(self) -> None:
            """Recompute the readout and the command preview from the controls."""
            try:
                start, end = self._range()
            except ClipError as exc:
                self._show_readout(f"range: {exc}", error=True)
                self._set_why("")
                self.query_one("#clip_len", Static).update(Text(""))
                self.query_one("#preview", Static).update("")
                return

            length = (end - start) if end is not None else (self.info.duration - start)
            self.query_one("#clip_len", Static).update(
                Text(format_time(max(0.0, length)), style=branding.GREY))

            recipe = self._recipe()
            try:
                dst = self._destination(start, end, recipe)
                plan = plan_from_recipe(
                    self.info, dst, recipe,
                    start=start if start > 0.0 else None,
                    end=end,
                )
            except ClipError as exc:
                self._last_plan = None
                self._show_readout(f"plan: {exc}", error=True)
                self._set_why("")
                self.query_one("#preview", Static).update("")
                return
            self._last_plan = plan

            what, why = describe(recipe, self.info)

            lines = [what, f"{dst.name}{DOT}{format_time(max(0.0, length))}"]
            if is_lossless(recipe):
                keyframe = keyframe_at_or_before(self.info.path, start)
                drift = start - keyframe
                if drift >= 0.001:
                    lines.append(
                        f"snaps to keyframe {format_time(keyframe)}"
                        f"  ({drift:.3f}s before {format_time(start)})")
                else:
                    lines.append(
                        f"starts exactly at keyframe {format_time(keyframe)}")

            preset = self._preset_name()
            if preset != "none":
                warning = duration_warning(PRESETS[preset], length)
                if warning:
                    why = f"{why}{DOT}{warning}"

            self._show_readout("\n".join(lines), error=False)
            self._set_why(why)
            self.query_one("#preview", Static).update(
                _fmt_command(plan.command) + "\n")

        def _show_readout(self, text: str, error: bool) -> None:
            """Update the readout and colour its accent bar (success vs error)."""
            colour = branding.RED if error else branding.GREEN
            self.query_one("#readout", Static).update(Text(text, style=colour))
            self.query_one("#readout_panel").set_class(error, "error")

        def _set_why(self, text: str) -> None:
            self.query_one("#why", Static).update(Text(text, style=branding.GREY))

        # -- actions (bound to the footer keys) ---------------------------------
        def action_cut(self) -> None:
            self._cut()

        def action_toggle_preview(self) -> None:
            """F7: show or hide the raw ffmpeg command."""
            self._toggle_preview()

        def action_preview_clip(self) -> None:
            """F2: look at the clip, stepping through frames between in and out."""
            try:
                start, end = self._range()
            except ClipError as exc:
                self.notify(str(exc), title="bad range", severity="error")
                return
            try:
                from tstudio.gallery.preview_screen import build_preview_screen
                screen = build_preview_screen(self.info.path, start=start, end=end)
            except Exception as exc:  # noqa: BLE001 - never take the editor down
                self.notify(str(exc), title="cannot preview", severity="error")
                return
            self.push_screen(screen)

        def action_play_output(self) -> None:
            """Watch the clip that was just written."""
            if self._last_output is None:
                self.notify("nothing written yet - cut something first",
                            severity="warning")
                return
            try:
                from tstudio.gallery.playback import open_in_player
                used = open_in_player(self._last_output)
            except Exception as exc:  # noqa: BLE001
                self.notify(str(exc), title="could not play", severity="error")
                return
            self.notify(f"{self._last_output.name} in {used}", title="playing")

        def action_copy_command(self) -> None:
            """Put the ffmpeg command on the clipboard.

            The panel hard-wraps a ~200 character command, so selecting it with
            the mouse yields a line-broken, unrunnable paste. This hands over
            the real one-line command instead.
            """
            plan = self._last_plan
            if plan is None:
                self.notify("nothing to copy yet", severity="warning")
                return
            line = _fmt_command(plan.command)
            try:
                from tstudio.gallery.clipboard import copy_text
                copy_text(line)
            except Exception as exc:  # noqa: BLE001 - clipboard is best effort
                self.notify(str(exc), title="could not copy", severity="error")
                return
            self.notify(f"{len(line)} characters", title="ffmpeg command copied")

        def action_reset_inputs(self) -> None:
            """Put every control back to what the app opened with."""
            self.query_one("#clip", Input).value = self.clip_text
            self.query_one("#quality", Input).value = ""
            self._syncing = True
            try:
                self._set_select("preset", "none")
                self._set_select("size", "keep")
                self._set_select("shape", "keep")
                self._set_select("colour", "keep")
                self._set_select("fps", "source")
                self._set_select("audio", "keep")
                self._set_select("encoder", "auto")
            finally:
                self._syncing = False
            self._sync_controls()
            self._refresh()

        # -- events ---------------------------------------------------------
        def on_input_changed(self, event: Input.Changed) -> None:
            if event.input.id in ("clip", "quality"):
                self._sync_controls()
                self._refresh()

        def on_select_changed(self, event: Select.Changed) -> None:
            if self._syncing:
                return
            if event.select.id == "preset":
                name = str(event.value)
                if name != "none":
                    self._apply_preset(name)
            self._sync_controls()
            self._refresh()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            actions = {
                "btn_export": self._cut,
                "btn_play": self.action_play_output,
            }
            action = actions.get(event.button.id or "")
            if action is not None:
                action()

        # -- running it ------------------------------------------------------
        def _set_controls_enabled(self, enabled: bool) -> None:
            self.query_one("#btn_export", Button).disabled = not enabled

        def _cut(self) -> None:
            """Run the job in a background thread; the UI stays responsive."""
            try:
                start, end = self._range()
                recipe = self._recipe()
                dst = self._destination(start, end, recipe)
                plan = plan_from_recipe(
                    self.info, dst, recipe,
                    start=start if start > 0.0 else None,
                    end=end,
                )
            except ClipError as exc:
                self.query_one("#status", Static).update(
                    Text(f"error: {exc}", style=branding.RED))
                return

            lossless = is_lossless(recipe)
            on_progress = None
            if not lossless:
                plan_start = plan.start or 0.0
                total = ((plan.end - plan_start) if plan.end is not None
                         else self.info.duration - plan_start)

                def on_progress(frame: dict) -> None:
                    out = frame.get("out_seconds")
                    if out is None:
                        return
                    fraction = min(1.0, max(0.0, out / total)) if total > 0 else 0.0
                    self.call_from_thread(self._ui_progress, fraction)

            self._set_controls_enabled(False)
            self._start_animation()
            self.query_one("#status", Static).update(
                Text("working...", style=branding.GREY))
            self._run_job(lossless, plan, dst, on_progress)

        @work(thread=True, exclusive=True, group="encode")
        def _run_job(self, lossless: bool, plan, dst, on_progress) -> None:
            """Run the ffmpeg job off the event loop.

            exclusive=True means a second press supersedes the first rather
            than starting two encodes over the same destination.
            """
            try:
                if lossless:
                    run_cut(plan, on_progress=on_progress)
                else:
                    run_convert(plan, on_progress=on_progress)
                self.call_from_thread(
                    self._ui_done, str(dst), _fmt_size(dst.stat().st_size))
            except ClipError as exc:
                self.call_from_thread(self._ui_fail, str(exc))
            except Exception as exc:  # noqa: BLE001
                self.call_from_thread(self._ui_fail, str(exc))

        # -- the animated progress block --------------------------------------
        def _widget(self, selector: str):
            """The widget, or None once it has gone.

            Three separate things have now crashed this editor by assuming a
            widget is still mounted: the progress timer, and both job
            callbacks. Finishing an encode - or ticking - after the screen has
            been taken out from under it is ordinary: pushing the preview
            screen does it, and so does quitting mid-encode. The difference is
            the blast radius. NoMatches raised in a timer callback, or carried
            back into a worker by call_from_thread, does not skip one update;
            it takes the whole app down.
            """
            found = self.query(selector)
            return found.first() if found else None

        def _bar_widget(self):
            """The progress Static, or None when it is not on the screen.

            The timer outlives the widget: pushing the preview screen takes
            the editor's own screen out from under it, and a quit during an
            encode unmounts everything while the next tick is already queued.
            Both are normal, so this answers None rather than raising NoMatches
            into Textual's timer callback.
            """
            return self._widget("#bar")

        def _bar_width(self, bar) -> int:
            """How wide the two progress lines may be, never below the minimum.

            ``progress.render`` refuses anything under 24 characters, and a
            widget's size is 0 until Textual has laid it out at least once.
            """
            width = bar.size.width if bar is not None else 0
            return max(24, width - 1)

        def _start_animation(self) -> None:
            self._tick = 0
            self._fraction = 0.0
            self._started_at = time.monotonic()
            self._draw_progress()
            if self._progress_timer is None:
                self._progress_timer = self.set_interval(1 / 8, self._tick_progress)

        def _stop_animation(self) -> None:
            if self._progress_timer is not None:
                self._progress_timer.stop()
                self._progress_timer = None

        def on_unmount(self) -> None:
            """Quitting mid-encode must not leave a timer firing at nothing."""
            self._stop_animation()

        def _tick_progress(self) -> None:
            self._tick += 1
            self._draw_progress()

        def _draw_progress(self, label: str = "") -> None:
            bar = self._bar_widget()
            if bar is None:
                # Nothing to draw on and nothing to come back to: a timer still
                # firing here is just burning frames.
                self._stop_animation()
                return
            elapsed = time.monotonic() - self._started_at
            try:
                lines = progress_art.render(
                    self._fraction, elapsed, self._tick,
                    self._bar_width(bar), label=label)
            except ClipError:
                return
            bar.update(Text("\n".join(lines), style=branding.GREEN))

        # -- UI updates (called on the main thread via call_from_thread) ------
        def _ui_progress(self, fraction: float) -> None:
            self._fraction = fraction

        def _ui_done(self, dst: str, size: str) -> None:
            self._fraction = 1.0
            self._stop_animation()
            self._draw_progress(label="done")
            self._last_output = Path(dst)
            name = Path(dst).name
            # The file is written either way; only the reporting needs a
            # screen to report onto.
            play = self._widget("#btn_play")
            status = self._widget("#status")
            if play is None or status is None:
                return
            # There is something to play now, so the button appears.
            play.display = True
            status.update(Text(f"done → {name}  ({size})", style=branding.GREEN))
            self.notify(f"{name} ({size})", title="written", severity="information")
            self._set_controls_enabled(True)

        def _ui_fail(self, message: str) -> None:
            self._stop_animation()
            bar = self._bar_widget()
            if bar is not None:
                bar.update(Text(""))
            status = self._widget("#status")
            if status is None:
                return
            status.update(Text(f"error: {message}", style=branding.RED))
            self.notify(message, title="failed", severity="error")
            self._set_controls_enabled(True)

    return ClipApp(info, start_text, end_text, out_path)


def _handle_errors(fn: Callable[[], int]) -> int:
    try:
        return fn()
    except ClipError as exc:
        print(f"tstudio: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"tstudio: {exc}", file=sys.stderr)
        return 1


def run_editor(video: Path, start: str = "", end: str = "",
               out: Path | None = None) -> int:
    """Probe the file, build the editor app and run it, returning the exit code.

    Called by `cli.py --tui`; the same error handling `main` applies is used
    here, so callers never see a traceback.
    """
    def _go() -> int:
        info = probe(video)
        app = _build_app(info, start, end, out)
        app.run()
        return 0

    return _handle_errors(_go)


def main(argv: list[str] | None = None) -> int:
    return _handle_errors(lambda: _run(argv))


if __name__ == "__main__":
    sys.exit(main())
