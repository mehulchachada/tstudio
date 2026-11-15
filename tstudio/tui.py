"""An optional Textual TUI: a precision single-clip editor over the clip backend.

This is a thin front-end over the same zero-dependency core the CLI uses
(`probe`, `plan_cut`, `run_cut`). It is NOT required for the CLI to work.

Run it with a video file:

    python clip_tui.py video.mp4
    python clip_tui.py video.mp4 0:12 0:45          # pre-fill start/end
    python clip_tui.py video.mp4 --out clip.mp4

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
# the same numbers the user sees from `clip --info`.
from tstudio.cli import _ENCODERS, _fmt_size, _header_lines


def _fmt_command(command: list[str]) -> str:
    """Join an argv list into one copy-pasteable line, quoting space-containing args."""
    parts: list[str] = []
    for arg in command:
        if " " in arg and not arg.startswith('"'):
            parts.append(f'"{arg}"')
        else:
            parts.append(arg)
    return " ".join(parts)


def _usage() -> str:
    return (
        "usage: python clip_tui.py VIDEO [START] [END] [--out PATH]\n"
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
            "The zero-dependency CLI is unchanged:  python clip.py <video> <range>\n"
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
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual import events
    from textual.widgets import (
        Button,
        Checkbox,
        Footer,
        Header,
        Input,
        Label,
        ProgressBar,
        Select,
        Static,
    )

    from tstudio.cut import plan_cut, run_cut
    from tstudio.convert import plan_convert, run_convert, TARGETS
    from tstudio.presets import PRESETS, duration_warning, get_preset, plan_social
    from tstudio.media import encoders_available, keyframe_at_or_before
    from tstudio.timespec import format_time, output_name, parse_time, resolve_relative, unique_name
    from tstudio.gallery import branding

    available = encoders_available()
    encoders = ["auto"] + [e for e in _ENCODERS if e != "auto" and e in available]

    class ClipApp(App):
        TITLE = branding.NAME
        SUB_TITLE = branding.TAGLINE

        CSS = f"""
        Screen {{
            padding: 0 1;
        }}
        Input {{
            height: 1;
            border: none;
            padding: 0 1;
            background: $boost;
        }}
        #quality {{
            width: 12;
            margin-right: 2;
        }}
        #start_input,
        #end_input {{
            width: 1fr;
        }}
        #mute {{
            width: 16;
        }}
        /* No boxes. Groups are separated by space, and the only rule on the
           screen is a single accent bar beside the readout - the one thing
           you must not miss. */
        #file_panel {{
            height: auto;
            padding: 0 2;
            margin-bottom: 1;
        }}
        #range_panel {{
            height: auto;
            padding: 0 2;
            margin-bottom: 1;
        }}
        #output_panel {{
            height: auto;
            padding: 0 2;
            margin-bottom: 1;
        }}
        #readout_panel {{
            height: auto;
            max-height: 5;
            border-left: thick $success;
            padding: 0 2;
            margin-bottom: 1;
        }}
        #readout_panel.error {{
            border-left: thick $error;
        }}
        #readout {{
            height: auto;
            max-height: 3;
            padding: 0 1;
        }}
        #preview_panel {{
            height: auto;
            border: round $accent;
            padding: 0 1;
            margin-bottom: 1;
        }}
        #preview {{
            height: auto;
            padding: 0 1;
        }}
        .ctl_label {{
            width: 10;
            padding: 0 0 0 1;
        }}
        .ctl_row {{
            height: auto;
            align: left middle;
        }}
        #row_buttons {{
            height: 3;
            align: left middle;
            margin-bottom: 1;
        }}
        #btn_cut {{
            margin-right: 1;
        }}
        #bar {{
            height: 1;
            margin: 0 0 1 0;
        }}
        #status {{
            height: 1;
            padding: 0 1;
        }}
        """

        BINDINGS = [
            Binding("f5", "cut", "Cut", priority=True),
            Binding("f2", "preview_clip", "Preview", priority=True),
            Binding("f7", "toggle_preview", "Show cmd", priority=True),
            Binding("f3", "reset_inputs", "Reset", priority=True),
            Binding("f4", "copy_command", "Copy cmd", priority=True),
            Binding("f6", "play_output", "Play result", priority=True),
            Binding("ctrl+q", "quit", "Quit", priority=True),
        ]

        def __init__(self, info: MediaInfo, start_text: str, end_text: str,
                     out_path: Path | None) -> None:
            super().__init__()
            self.info = info
            self.start_text = start_text
            self.end_text = end_text
            self.out_path = out_path
            self._last_plan = None
            self._last_output = None

        def compose(self) -> ComposeResult:
            yield Header()
            with Vertical(id="file_panel"):
                yield Static("\n".join(_header_lines(self.info)) + "\n")
            with Vertical(id="range_panel"):
                with Horizontal(classes="ctl_row"):
                    yield Label("Start", classes="ctl_label")
                    yield Input(value=self.start_text,
                                placeholder="e.g. 0:12 or 'first 10s'",
                                id="start_input")
                with Horizontal(classes="ctl_row"):
                    yield Label("End", classes="ctl_label")
                    yield Input(value=self.end_text,
                                placeholder="blank = to end",
                                id="end_input")
            with Vertical(id="output_panel"):
                with Horizontal(classes="ctl_row"):
                    yield Label("Task", classes="ctl_label")
                    yield Select(
                        [("Cut (lossless)", "cut"),
                         ("Convert", "convert"),
                         ("Social", "social")],
                        value="cut", id="task")
                    yield Label("Mode", classes="ctl_label", id="mode_label")
                    yield Select(
                        [("Lossless copy", "lossless"),
                         ("Re-encode (exact)", "exact")],
                        value="lossless", id="mode")
                    yield Label("Target", classes="ctl_label", id="target_label")
                    yield Select([(t, t) for t in TARGETS], value="1080p",
                                 id="target")
                    yield Label("Preset", classes="ctl_label", id="preset_label")
                    yield Select([(p.name, p.name) for p in PRESETS.values()],
                                 value="shorts", id="preset")
                with Horizontal(classes="ctl_row"):
                    yield Label("Encoder", classes="ctl_label")
                    yield Select([(e, e) for e in encoders], value="auto",
                                 id="encoder", disabled=True)
                    yield Label("HDR", classes="ctl_label", id="hdr_label")
                    yield Select([("auto", "auto"), ("keep HDR", "keep"),
                                  ("force SDR", "sdr")],
                                 value="auto", id="hdr")
                    yield Label("Fit", classes="ctl_label", id="fit_label")
                    yield Select([("pad (blurred bg)", "pad"),
                                  ("crop (fills screen)", "crop")],
                                 value="pad", id="fit")
                with Horizontal(classes="ctl_row"):
                    yield Label("Quality", classes="ctl_label")
                    yield Input(placeholder="crf/cq", id="quality")
                    yield Checkbox("mute", id="mute")
            with Vertical(id="readout_panel"):
                yield Static("", id="readout")
            with Vertical(id="preview_panel"):
                yield Static("", id="preview")
            with Horizontal(id="row_buttons"):
                yield Button("Cut (F5)", variant="success", id="btn_cut")
                yield Button("Preview (F2)", variant="primary", id="btn_preview")
            yield ProgressBar(total=1.0, id="bar")
            yield Static("", id="status")
            yield Footer()

        def on_mount(self) -> None:
            # ProgressBar is already constructed with total=1.0 in compose().
            self._sync_controls()
            self._refresh()
            self._set_border_title()
            self._toggle_preview(False)

        def _sync_controls(self) -> None:
            """Show/hide the task-specific controls and rename the action button."""
            task = self.query_one("#task", Select).value
            lossless = self.query_one("#mode", Select).value == "lossless"

            def _show(widget_id: str, label: str, visible: bool) -> None:
                self.query_one(f"#{widget_id}").display = visible
                self.query_one(f"#{widget_id}_label").display = visible

            _show("mode", "Mode", task == "cut")
            _show("target", "Target", task == "convert")
            _show("preset", "Preset", task == "social")
            _show("hdr", "HDR", task == "convert")
            _show("fit", "Fit", task == "social")

            # Encoder/quality: disabled only for a lossless cut (today's rule).
            disabled = task == "cut" and lossless
            self.query_one("#encoder", Select).disabled = disabled
            self.query_one("#quality", Input).disabled = disabled

            labels = {"cut": "Cut (F5)", "convert": "Convert (F5)",
                      "social": "Export (F5)"}
            self.query_one("#btn_cut", Button).label = labels[task]

        def _set_border_title(self) -> None:
            """Set the file panel's border title to the file name, ellipsised to fit."""
            name = self.info.path.name
            if len(name) > 40:
                name = name[:37] + "..."
            self.query_one("#file_panel").border_title = name
            self.query_one("#range_panel").border_title = "Range"
            self.query_one("#output_panel").border_title = "Output"
            self.query_one("#readout_panel").border_title = "What will happen"
            self.query_one("#preview_panel").border_title = "ffmpeg command"

        def _toggle_preview(self, visible: bool | None = None) -> None:
            """Show/hide the command preview panel (toggle if visible is None)."""
            panel = self.query_one("#preview_panel")
            if visible is None:
                visible = not panel.display
            panel.display = visible

        # -- actions (bound to the footer keys) -------------------------------
        def action_cut(self) -> None:
            self._cut()

        def action_toggle_preview(self) -> None:
            """F7: show or hide the raw ffmpeg command."""
            self._toggle_preview()

        def action_preview_clip(self) -> None:
            """F2: look at the clip, stepping through frames between in and out.

            "Preview" used to reveal an ffmpeg command, which is not a preview
            of anything you can watch.
            """
            try:
                start, end = self._current()
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
            """Restore the start/end fields to what the app opened with."""
            self.query_one("#start_input", Input).value = self.start_text
            self.query_one("#end_input", Input).value = self.end_text
            self.query_one("#task", Select).value = "cut"
            self._sync_controls()
            self._refresh()

        # -- helpers ----------------------------------------------------------
        def _set_controls_enabled(self, enabled: bool) -> None:
            self.query_one("#btn_cut", Button).disabled = not enabled
            self.query_one("#btn_preview", Button).disabled = not enabled

        def _current(self) -> tuple[float, float | None]:
            """Resolve the START/END fields to (start, end|None)."""
            s_text = self.query_one("#start_input", Input).value.strip()
            e_text = self.query_one("#end_input", Input).value.strip()
            try:
                start = parse_time(s_text) if s_text else 0.0
            except ClipError:
                start, rel_end = resolve_relative(s_text, self.info.duration)
                return start, rel_end
            end = parse_time(e_text) if e_text else None
            return start, end

        def _current_task(self) -> str:
            return self.query_one("#task", Select).value

        def _suffix(self) -> Path:
            """Destination name with a task suffix, through unique_name unless fixed."""
            if self.out_path is not None:
                return Path(self.out_path)
            name = self.info.path.name
            stem, dot, suffix = name.rpartition(".")
            if not dot:
                stem, suffix = name, ""
            task = self._current_task()
            if task == "convert":
                tag = self.query_one("#target", Select).value
            elif task == "social":
                tag = self.query_one("#preset", Select).value
            else:
                tag = None
            if tag is not None:
                name = f"{stem}_{tag}{dot}{suffix}"
            return unique_name(self.info.path.parent / name)

        def _make_plan(self, start: float, end: float | None,
                       lossless: bool, encoder: str, quality: int | None,
                       mute: bool) -> tuple[object, Path]:
            task = self._current_task()
            if task == "cut":
                dst = output_name(self.info.path, start, end)
                if self.out_path is not None:
                    dst = Path(self.out_path)
                if not lossless:
                    dst = unique_name(dst)
                plan = plan_cut(self.info, dst, start, end,
                                lossless=lossless, encoder=encoder,
                                quality=quality, mute=mute)
                return plan, dst
            # A blank range means the whole file: pass None, not 0.0.
            c_start = start if start > 0.0 else None
            if task == "convert":
                dst = self._suffix()
                hdr = self.query_one("#hdr", Select).value
                plan = plan_convert(self.info, dst,
                                    self.query_one("#target", Select).value,
                                    hdr=hdr, encoder=encoder, quality=quality,
                                    mute=mute, start=c_start, end=end)
                return plan, dst
            # social
            dst = self._suffix()
            preset = get_preset(self.query_one("#preset", Select).value)
            fit = self.query_one("#fit", Select).value
            kwargs: dict[str, object] = dict(fit=fit, quality=quality, mute=mute,
                                             start=c_start, end=end)
            if encoder != "auto":
                kwargs["encoder"] = encoder
            plan = plan_social(self.info, dst, preset, **kwargs)
            return plan, dst

        def _refresh(self) -> None:
            """Recompute the live readout and command preview from the fields."""
            task = self._current_task()
            lossless = self.query_one("#mode", Select).value == "lossless"
            try:
                start, end = self._current()
            except ClipError as exc:
                self._show_readout(f"range error: {exc}", error=True)
                self.query_one("#preview", Static).update("")
                return

            q_raw = self.query_one("#quality", Input).value.strip()
            quality = int(q_raw) if q_raw else None
            if quality is not None:
                try:
                    int(quality)
                except ValueError:
                    quality = None
            mute = self.query_one("#mute", Checkbox).value
            sel_encoder = self.query_one("#encoder", Select).value
            encoder = "auto" if (task == "cut" and lossless) else sel_encoder

            try:
                plan, dst = self._make_plan(start, end, lossless, encoder, quality, mute)
            except ClipError as exc:
                self._last_plan = None
                self._show_readout(f"plan error: {exc}", error=True)
                self.query_one("#preview", Static).update("")
                return
            self._last_plan = plan

            lines: list[str] = []
            if task == "cut":
                if lossless:
                    kf = keyframe_at_or_before(self.info.path, start)
                    drift = start - kf
                    if drift >= 0.001:
                        lines.append(f"  snaps to keyframe {format_time(kf)}  "
                                     f"({drift:.3f}s before {format_time(start)})")
                    else:
                        lines.append(f"  starts exactly at keyframe {format_time(kf)}")
                else:
                    lines.append(f"  re-encoding with {encoder} (exact start)")
                shown_end = end if end is not None else self.info.duration
                lines.append(f"  end: {format_time(shown_end)}" + ("" if end is not None else " (end of file)"))
                length = (end - start) if end is not None else (self.info.duration - start)
                lines.append(f"  length: {format_time(max(0.0, length))}")
                lines.append(f"  output: {dst.name}")
            else:
                if task == "convert":
                    target_text = self.query_one("#target", Select).value
                    fit = None
                else:
                    preset = get_preset(self.query_one("#preset", Select).value)
                    target_text = preset.label
                    fit = self.query_one("#fit", Select).value
                target = f"{target_text}  {plan.width}x{plan.height}"
                if fit:
                    target += f"  {fit}"
                if plan.tonemapped:
                    colour = (f"HDR10 -> SDR, "
                              f"tone-mapped with {plan.tonemapper}")
                elif plan.hdr_out:
                    colour = "HDR10 kept (bt2020 / PQ, 10-bit)"
                else:
                    colour = "SDR"
                lines.append(f"  {task}: {target}   ·   {colour}")
                length = (end - start) if end is not None else (self.info.duration - start)
                if end is None and start <= 0.0:
                    length_text = f"whole file ({format_time(self.info.duration)})"
                else:
                    length_text = format_time(max(0.0, length))
                if task == "social":
                    warn = duration_warning(preset, length)
                    if warn:
                        length_text = f"{length_text}  ·  {warn}"
                lines.append(f"  length: {length_text}")
                lines.append(f"  output: {dst.name}")
            self._show_readout("\n".join(lines), error=False)
            self.query_one("#preview", Static).update(_fmt_command(plan.command) + "\n")

        def _show_readout(self, text: str, error: bool) -> None:
            """Update the readout and colour its panel border (success vs error)."""
            self.query_one("#readout", Static).update(text)
            panel = self.query_one("#readout_panel")
            panel.set_class(error, "error")

        # -- events -----------------------------------------------------------
        def on_input_changed(self, event: Input.Changed) -> None:
            if event.input.id in ("start_input", "end_input", "quality"):
                self._refresh()

        def on_select_changed(self, event: Select.Changed) -> None:
            if event.select.id == "task":
                self._sync_controls()
                self._refresh()
            elif event.select.id == "mode":
                self._sync_controls()
                self._refresh()
            elif event.select.id in ("target", "preset", "hdr", "fit"):
                self._refresh()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "btn_preview":
                self._toggle_preview()
            elif event.button.id == "btn_cut":
                self._cut()

        def _cut(self) -> None:
            """Run the cut in a background thread; the UI stays responsive."""

            task = self._current_task()
            lossless = self.query_one("#mode", Select).value == "lossless"
            encoder = "auto" if (task == "cut" and lossless) else self.query_one("#encoder", Select).value
            q_raw = self.query_one("#quality", Input).value.strip()
            quality = int(q_raw) if q_raw else None
            if quality is not None:
                try:
                    int(quality)
                except ValueError:
                    quality = None
            mute = self.query_one("#mute", Checkbox).value

            try:
                start, end = self._current()
                plan, dst = self._make_plan(start, end, lossless, encoder, quality, mute)
            except ClipError as exc:
                self.query_one("#status", Static).update(f"error: {exc}")
                return

            on_progress = None
            if task != "cut" or not lossless:
                target = (plan.end - plan.start) if plan.end is not None else self.info.duration - plan.start

                def on_progress(frame: dict) -> None:
                    out = frame.get("out_seconds")
                    if out is None:
                        return
                    pct = min(1.0, max(0.0, out / target))
                    speed = frame.get("speed")
                    eta = (target - out) / speed if speed and speed > 0 else None
                    text = f"{pct * 100:5.1f}%"
                    if speed:
                        text += f"  {speed:.2f}x"
                    if eta is not None and not frame.get("done"):
                        text += f"  eta {eta:.0f}s"
                    if frame.get("done"):
                        text += "  done"
                    self.call_from_thread(self._ui_progress, pct, text)

            self._set_controls_enabled(False)
            self.query_one("#bar", ProgressBar).progress = 0.0
            self.query_one("#status", Static).update("cutting...")

            self._run_job(task, plan, dst, on_progress)

        @work(thread=True, exclusive=True, group="encode")
        def _run_job(self, task: str, plan, dst, on_progress) -> None:
            """Run the ffmpeg job off the event loop.

            exclusive=True means a second press supersedes the first rather
            than starting two encodes over the same destination.
            """
            try:
                if task == "cut":
                    run_cut(plan, on_progress=on_progress)
                else:
                    run_convert(plan, on_progress=on_progress)
                self.call_from_thread(self._ui_done, str(dst), _fmt_size(dst.stat().st_size))
            except ClipError as exc:
                self.call_from_thread(self._ui_fail, str(exc))
            except Exception as exc:  # noqa: BLE001
                self.call_from_thread(self._ui_fail, str(exc))

        # -- UI updates (called on the main thread via call_from_thread) ------
        def _ui_progress(self, pct: float, text: str) -> None:
            self.query_one("#bar", ProgressBar).progress = pct
            self.query_one("#status", Static).update(text)

        def _ui_done(self, dst: str, size: str) -> None:
            self.query_one("#bar", ProgressBar).progress = 1.0
            self._last_output = Path(dst)
            name = Path(dst).name
            self.query_one("#status", Static).update(f"done -> {name}  ({size})")
            self.notify(f"{name} ({size})", title="written", severity="information")
            self._set_controls_enabled(True)

        def _ui_fail(self, message: str) -> None:
            self.query_one("#status", Static).update(f"error: {message}")
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
    except Exception as exc:  # noqa: BLE001 - never show a traceback
        print(f"tstudio: {exc}", file=sys.stderr)
        return 1


def run_editor(video: Path, start: str = "", end: str = "",
               out: Path | None = None) -> int:
    """Probe the file, build the editor app and run it, returning the exit code.

    Called by `clip.py --tui`; the same error handling `main` applies is used
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
