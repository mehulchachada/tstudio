"""Headless tests for the optional Textual TUI, driven via run_test().

Textual is an optional dependency: these tests skip if it is not installed or
if ffmpeg/ffprobe is unavailable. They exercise the TUI over the shared
zero-dependency backend (probe / plan_from_recipe / run_cut) and do not require
a display or a real terminal.

The editor has one vocabulary: CLIP, PRESET, SIZE, SHAPE, COLOUR, FPS, AUDIO.
There is no "task" and no "mode"; "lossless" is what is left when nothing about
the picture or the sound changes, and `recipe.is_lossless` decides it. Several
tests below exist specifically to stop the old shape coming back.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

try:  # Textual is optional for the CLI; skip (rather than fail) if it is absent.
    import textual  # noqa: F401
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False

from fixture import ensure_sample, ffmpeg_available
from tstudio.media import probe
from tstudio.tui import BUTTON_ICONS, PRESET_ICONS, initial_clip_text


def _text(pilot, selector: str) -> str:
    """Rendered text of a widget by selector, tolerant across Textual versions."""
    w = pilot.app.query_one(selector)
    render = w.render
    if callable(render):
        render = render()
    return getattr(render, "plain", render if isinstance(render, str) else str(render))


def _app(start: str = "", end: str = "", out: Path | None = None,
         kind: str = "dense"):
    from tstudio.tui import _build_app
    info = probe(ensure_sample(kind))
    return _build_app(info, start, end, out)


def _value(pilot, selector: str):
    return pilot.app.query_one(selector).value


async def _settle(pilot, frames: int = 4) -> None:
    """Let a Select change propagate through _sync_controls and _refresh."""
    for _ in range(frames):
        await pilot.pause()


class TestClipText(unittest.TestCase):
    """initial_clip_text is pure, so it needs no app and no ffmpeg."""

    def test_both_given_become_a_range(self) -> None:
        self.assertEqual(initial_clip_text("3:49", "4:19"), "3:49-4:19")

    def test_start_only_means_to_the_end(self) -> None:
        self.assertEqual(initial_clip_text("3:49", ""), "3:49")

    def test_end_only_still_needs_a_start(self) -> None:
        # parse_range refuses a range with no start, so one is supplied.
        self.assertEqual(initial_clip_text("", "4:19"), "0-4:19")

    def test_neither_is_the_whole_file(self) -> None:
        self.assertEqual(initial_clip_text("", ""), "")
        self.assertEqual(initial_clip_text("  ", " "), "")


@unittest.skipUnless(_HAS_TEXTUAL and ffmpeg_available(),
                     "Textual and ffmpeg/ffprobe both required")
class TestTui(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    # -- the one clip field ------------------------------------------------
    def test_mount_folds_start_and_end_into_one_clip_field(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                self.assertEqual(_value(pilot, "#clip"), "2-4")
                # The field beside ⓘ is the clip's LENGTH, not its end time.
                self.assertEqual(_text(pilot, "#clip_len").strip(), "0:02.000")

        self._run(scenario())

    def test_a_relative_range_resolves_in_the_same_field(self) -> None:
        async def scenario():
            async with _app().run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#clip").value = "first 2s"
                await _settle(pilot)
                self.assertEqual(_text(pilot, "#clip_len").strip(), "0:02.000")

        self._run(scenario())

    def test_an_empty_clip_field_is_the_whole_file(self) -> None:
        async def scenario():
            async with _app().run_test() as pilot:
                await pilot.pause()
                self.assertEqual(_value(pilot, "#clip"), "")
                self.assertEqual(_text(pilot, "#clip_len").strip(), "0:10.000")

        self._run(scenario())

    def test_a_bad_range_shows_in_the_readout_not_a_crash(self) -> None:
        async def scenario():
            async with _app().run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#clip").value = "banana"
                await _settle(pilot)
                self.assertIn("range", _text(pilot, "#readout").lower())
                self.assertTrue(
                    pilot.app.query_one("#readout_panel").has_class("error"))

        self._run(scenario())

    # -- no preset is the default -----------------------------------------
    def test_no_preset_is_the_default_and_is_lossless(self) -> None:
        """Most clips are not going to a social platform. That is the default."""
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                self.assertEqual(_value(pilot, "#preset"), "none")
                self.assertIn("lossless", _text(pilot, "#readout"))
                self.assertIn("nothing about the picture changes",
                              _text(pilot, "#why"))
                self.assertIn("Export", str(pilot.app.query_one("#btn_export").label))

        self._run(scenario())

    def test_the_readout_shows_the_keyframe_drift_for_a_lossless_cut(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                self.assertIn("keyframe", _text(pilot, "#readout"))

        self._run(scenario())

    # -- a preset fills in, it does not lock out ---------------------------
    def test_preset_fills_the_controls_in_and_leaves_them_editable(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#preset").value = "shorts"
                await _settle(pilot)

                self.assertEqual(_value(pilot, "#size"), "1080p")
                self.assertEqual(_value(pilot, "#shape"), "9:16|pad")
                # The fixture is 30fps, so a 60fps cap is not on offer and the
                # preset leaves FPS alone rather than inventing an option.
                self.assertEqual(_value(pilot, "#fps"), "source")

                # The whole point: every one of them is still usable.
                for selector in ("#size", "#shape", "#colour", "#fps", "#audio"):
                    self.assertFalse(
                        pilot.app.query_one(selector).disabled,
                        f"{selector} was locked by the preset")

        self._run(scenario())

    def test_editing_a_field_marks_the_preset_modified(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#preset").value = "shorts"
                await _settle(pilot)
                self.assertEqual(_text(pilot, "#preset_note").strip(), "")

                pilot.app.query_one("#size").value = "720p"
                await _settle(pilot)
                note = _text(pilot, "#preset_note")
                self.assertIn("modified", note)
                self.assertIn("size", note)
                # ...and the preset itself is NOT silently cleared.
                self.assertEqual(_value(pilot, "#preset"), "shorts")

        self._run(scenario())

    def test_shorts_but_keep_hdr_stays_vertical(self) -> None:
        """The regression this whole rebuild exists for.

        Choosing a vertical preset and then asking to keep HDR used to drop the
        reframe on the floor and hand back a landscape clip. Keeping the colour
        must change the colour handling and nothing else.
        """
        async def scenario():
            async with _app("1", "3", kind="hdr").run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#preset").value = "shorts"
                await _settle(pilot)
                self.assertEqual(_value(pilot, "#colour"), "sdr")

                pilot.app.query_one("#colour").value = "keep"
                await _settle(pilot)

                self.assertEqual(_value(pilot, "#shape"), "9:16|pad",
                                 "keeping HDR cancelled the reframe")
                command = " ".join(pilot.app._last_plan.command)
                self.assertIn("1080:1920", command,
                              f"not reframed to 9:16: {command}")
                self.assertNotIn("tonemap", command,
                                 "still tone-mapping although HDR is kept")
                self.assertIn("bt2020", command,
                              "HDR kept but no bt2020 colour flags")

        self._run(scenario())

    def test_an_sdr_source_is_not_offered_a_colour_choice(self) -> None:
        """A choice that cannot do anything is not a choice; it is clutter."""
        async def scenario():
            async with _app().run_test() as pilot:
                await pilot.pause()
                options = [v for _, v in pilot.app.query_one("#colour")._options]
                self.assertEqual(options, ["keep"])

        self._run(scenario())

    def test_colour_is_named_by_what_comes_out(self) -> None:
        """SDR and HDR10 are one choice, so they must read as one pair.

        They used to be labelled "keep HDR10" and "SDR  (tone-map)", which
        names the operation rather than the result and makes the two sound
        like different kinds of thing. The value behind each is unchanged.
        """
        async def scenario():
            async with _app(kind="hdr").run_test() as pilot:
                await pilot.pause()
                labels = [str(p) for p, _ in pilot.app.query_one("#colour")._options]
                self.assertEqual(labels, ["HDR10", "SDR"])
                for label in labels:
                    self.assertNotIn("keep", label.lower())
                    self.assertNotIn("tone", label.lower())

        self._run(scenario())

    def test_an_sdr_source_still_says_what_it_will_produce(self) -> None:
        """Its one option reads "SDR", not "keep" - a colour, like the pair."""
        async def scenario():
            async with _app().run_test() as pilot:
                await pilot.pause()
                labels = [str(p) for p, _ in pilot.app.query_one("#colour")._options]
                self.assertEqual(labels, ["SDR"])

        self._run(scenario())

    # -- one button, and it does not move about ---------------------------
    def test_the_action_button_keeps_one_label(self) -> None:
        """It used to read Cut / Convert / Export depending on the recipe.

        That renamed the one fixed thing on the screen while saying nothing
        the readout does not already say outright, so the label is now
        constant and the readout carries the meaning.
        """
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                button = pilot.app.query_one("#btn_export")
                self.assertIn("Export", str(button.label))

                # 240p, not 720p: the fixture is 360p and the planner refuses
                # to upscale, which would leave the readout showing that
                # refusal instead of a plan.
                pilot.app.query_one("#size").value = "240p"
                await _settle(pilot)
                self.assertIn("Export", str(button.label))
                self.assertIn("re-encode", _text(pilot, "#readout"))

                pilot.app.query_one("#preset").value = "shorts"
                await _settle(pilot)
                self.assertIn("Export", str(button.label))

        self._run(scenario())

    def test_play_appears_only_once_there_is_something_to_play(self) -> None:
        """Nothing has been written at mount, so there is nothing to offer."""
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "played.mp4"

        async def scenario():
            async with _app("2", "4", out).run_test() as pilot:
                await pilot.pause()
                play = pilot.app.query_one("#btn_play")
                self.assertFalse(play.display,
                                 "Play is offered before anything is written")

                await pilot.press("f5")
                for _ in range(200):
                    await pilot.pause(0.05)
                    if out.exists() and out.stat().st_size > 0:
                        break
                for _ in range(40):
                    await pilot.pause(0.05)
                    if play.display:
                        break
                self.assertTrue(play.display,
                                f"Play never appeared: {_text(pilot, '#status')}")

        self._run(scenario())

    def test_the_button_row_is_only_the_two(self) -> None:
        """Preview and Copy are keyboard-only; the footer lists them."""
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                from textual.widgets import Button
                ids = [b.id for b in pilot.app.query(Button)]
                self.assertEqual(sorted(ids), ["btn_export", "btn_play"])
                bindings = pilot.app.active_bindings
                self.assertIn("f2", bindings)
                self.assertIn("f4", bindings)

        self._run(scenario())

    def test_encoder_and_quality_are_live_only_when_re_encoding(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                self.assertTrue(pilot.app.query_one("#encoder").disabled)
                self.assertTrue(pilot.app.query_one("#quality").disabled)

                pilot.app.query_one("#shape").value = "1:1|crop"
                await _settle(pilot)
                self.assertFalse(pilot.app.query_one("#encoder").disabled)
                self.assertFalse(pilot.app.query_one("#quality").disabled)

        self._run(scenario())

    def test_the_output_name_carries_the_preset(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#preset").value = "tiktok"
                await _settle(pilot)
                self.assertIn("tiktok", _text(pilot, "#readout"))

        self._run(scenario())

    # -- the command preview ------------------------------------------------
    def test_command_panel_starts_hidden_and_toggles(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                panel = pilot.app.query_one("#preview_panel")
                self.assertFalse(panel.display)
                await pilot.press("f7")
                await pilot.pause()
                self.assertTrue(panel.display)
                self.assertIn("ffmpeg", _text(pilot, "#preview"))

        self._run(scenario())

    def test_the_command_preview_is_one_unwrapped_line(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                await pilot.press("f7")
                await pilot.pause()
                body = _text(pilot, "#preview").strip()
                self.assertEqual(body.count("ffmpeg"), 1)
                self.assertNotIn("\n", body)

        self._run(scenario())

    # -- it actually writes files ------------------------------------------
    def test_a_lossless_cut_writes_a_file(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "out.mp4"

        async def scenario():
            async with _app("2", "4", out).run_test() as pilot:
                await pilot.pause()
                await pilot.press("f5")
                for _ in range(200):
                    await pilot.pause(0.05)
                    if out.exists() and out.stat().st_size > 0:
                        break
                self.assertTrue(out.exists(), _text(pilot, "#status"))
                self.assertGreater(out.stat().st_size, 0)

        self._run(scenario())

    def test_a_reframe_writes_a_file(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "square.mp4"

        async def scenario():
            async with _app("2", "4", out).run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#shape").value = "1:1|crop"
                await _settle(pilot)
                await pilot.press("f5")
                for _ in range(400):
                    await pilot.pause(0.05)
                    if out.exists() and out.stat().st_size > 0:
                        break
                self.assertTrue(out.exists(), _text(pilot, "#status"))
                self.assertGreater(out.stat().st_size, 0)

        self._run(scenario())

    def test_the_progress_block_animates_while_encoding(self) -> None:
        """Two lines from tstudio.gallery.progress, and they move."""
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "anim.mp4"

        async def scenario():
            async with _app("2", "4", out).run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#shape").value = "1:1|crop"
                await _settle(pilot)
                await pilot.press("f5")

                seen: set[str] = set()
                for _ in range(120):
                    await pilot.pause(0.05)
                    body = _text(pilot, "#bar")
                    if body.strip():
                        seen.add(body)
                    if len(seen) > 1:
                        break
                self.assertTrue(seen, "the progress block never drew anything")
                sample = next(iter(seen))
                self.assertEqual(len(sample.rstrip("\n").split("\n")), 2,
                                 f"expected two progress lines, got {sample!r}")
                self.assertGreater(len(seen), 1,
                                   "the progress block drew but never changed")

        self._run(scenario())

    def test_a_job_reporting_back_to_no_screen_does_not_crash(self) -> None:
        """Quitting mid-encode is ordinary, and the worker still reports back.

        call_from_thread carries whatever the callback raises back into the
        worker, so NoMatches here does not cost one status line - it fails
        the worker and takes the app down with it. That is what made the
        animation test above fail about one run in three: it stops watching
        as soon as it has seen two frames, while ffmpeg is still going.
        """
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "gone.mp4"

        async def scenario():
            async with _app("2", "4", out).run_test() as pilot:
                await pilot.pause()
                await pilot.app.query_one("#status").remove()
                await pilot.app.query_one("#btn_play").remove()
                await pilot.pause()

                pilot.app._ui_done(str(out), "1.2 MB")
                pilot.app._ui_fail("ffmpeg said no")
                await pilot.pause()

                self.assertTrue(pilot.app.is_running)
                # The file was written before any of this; losing the screen
                # must not lose the path to it.
                self.assertEqual(pilot.app._last_output, out)

        self._run(scenario())

    def test_a_broken_progress_frame_does_not_kill_the_encode(self) -> None:
        """The bar is decoration; ffmpeg is the work.

        A real IndexError in progress.render came out of this timer callback
        and took the whole app down mid-export. The bug itself is fixed and
        swept in test_progress, but the blast radius was the worse half: an
        encode that had been running for minutes died for a drawing glitch.
        """
        from unittest import mock

        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                pilot.app._start_animation()
                await pilot.pause()
                # the editor aliases this module, so patching it here is
                # what the editor will call
                with mock.patch("tstudio.gallery.progress.render",
                                side_effect=IndexError("string index out of range")):
                    pilot.app._draw_progress()
                    await pilot.pause()

                self.assertTrue(pilot.app.is_running, "the app went down")
                # it stops animating rather than raising on every tick
                self.assertIsNone(pilot.app._progress_timer)
                self.assertIn("progress display stopped", _text(pilot, "#status"))

        self._run(scenario())

    # -- shape of the screen -------------------------------------------------
    def test_everything_fits_in_a_100x34_terminal(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test(size=(100, 34)) as pilot:
                await pilot.pause()
                offenders: list[str] = []
                for node in pilot.app.screen.walk_children():
                    if type(node).__name__ == "Tooltip":
                        continue  # Textual parks tooltips off-screen deliberately
                    region = node.region
                    if region.width == 0 and region.height == 0:
                        continue
                    if region.y + region.height > 34 or region.x + region.width > 100:
                        offenders.append(f"{type(node).__name__}#{node.id} {region}")
                self.assertEqual(
                    offenders, [],
                    "widgets off-screen at 100x34: " + "; ".join(offenders))

        self._run(scenario())

    def test_everything_still_fits_with_a_preset_chosen(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test(size=(100, 34)) as pilot:
                await pilot.pause()
                pilot.app.query_one("#preset").value = "shorts"
                await _settle(pilot)
                offenders: list[str] = []
                for node in pilot.app.screen.walk_children():
                    if type(node).__name__ == "Tooltip":
                        continue
                    region = node.region
                    if region.width == 0 and region.height == 0:
                        continue
                    if region.y + region.height > 34 or region.x + region.width > 100:
                        offenders.append(f"{type(node).__name__}#{node.id} {region}")
                self.assertEqual(
                    offenders, [],
                    "widgets off-screen at 100x34: " + "; ".join(offenders))

        self._run(scenario())

    def test_labels_line_up(self) -> None:
        """Every field starts in one column, or the screen reads as a jumble."""
        from tstudio.tui import LABEL_WIDTH

        async def scenario():
            async with _app("2", "4").run_test(size=(110, 40)) as pilot:
                await pilot.pause()
                heads = [n for n in pilot.app.screen.query(".row_head")
                         if n.region.width > 0]
                self.assertGreaterEqual(len(heads), 6)
                xs = {n.region.x for n in heads}
                widths = {n.region.width for n in heads}
                self.assertEqual(len(xs), 1, f"row labels start at {sorted(xs)}")
                self.assertEqual(widths, {LABEL_WIDTH})

                field_x = xs.pop() + LABEL_WIDTH
                for selector in ("#clip", "#preset", "#size", "#colour",
                                 "#audio", "#quality"):
                    self.assertEqual(
                        pilot.app.query_one(selector).region.x, field_x,
                        f"{selector} does not start at the field column")

        self._run(scenario())

    def test_shortcuts_are_live_with_an_input_focused(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#clip").focus()
                await pilot.pause()
                bindings = pilot.app.active_bindings
                for key in ("f5", "f2", "f3", "f4", "f6"):
                    self.assertIn(
                        key, bindings,
                        f"{key} not in active bindings: {sorted(bindings)}")

        self._run(scenario())

    # -- the small visual contracts ----------------------------------------
    def test_button_icons_are_single_cell(self) -> None:
        """A two-cell glyph would shift the label and break the button row."""
        from rich.cells import cell_len

        for name, icon in BUTTON_ICONS.items():
            with self.subTest(button=name):
                self.assertEqual(cell_len(icon), 1, f"{name}={icon!r}")

        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                for selector in ("#btn_export", "#btn_play"):
                    label = str(pilot.app.query_one(selector).label)
                    self.assertIn(
                        label[0], BUTTON_ICONS.values(),
                        f"{selector} starts with {label[0]!r}, not an icon")
                    self.assertEqual(label[1], " ")

        self._run(scenario())

    def test_preset_icons_are_single_cell(self) -> None:
        from rich.cells import cell_len

        for name, icon in PRESET_ICONS.items():
            with self.subTest(preset=name):
                self.assertEqual(cell_len(icon), 1, f"{name}={icon!r}")

    def test_every_preset_has_an_icon(self) -> None:
        from tstudio.presets import PRESETS

        for key in PRESETS:
            self.assertIn(key, PRESET_ICONS, f"no icon for preset {key!r}")
        self.assertIn("none", PRESET_ICONS, "the 'no preset' entry needs one too")

    def test_no_preset_leads_the_list(self) -> None:
        async def scenario():
            async with _app().run_test() as pilot:
                await pilot.pause()
                options = [v for _, v in pilot.app.query_one("#preset")._options]
                self.assertEqual(options[0], "none")

        self._run(scenario())

    def test_the_uncapped_fps_option_is_not_an_empty_string(self) -> None:
        """Textual's Select has its own blank sentinel; do not shadow it."""
        async def scenario():
            async with _app().run_test() as pilot:
                await pilot.pause()
                options = [v for _, v in pilot.app.query_one("#fps")._options]
                self.assertNotIn("", options)
                self.assertEqual(options[0], "source")

        self._run(scenario())

    def test_focus_changes_the_input_style(self) -> None:
        """Focus has to be visible: the field's own style changes, not a border.

        `border` is not in Styles.ANIMATABLE (textual 8.2.8), so the focus look
        is carried by background/background-tint/color - which is what this
        asserts. The background is animated (transition: background 140ms), so
        give the animation a few frames to land before reading it.
        """
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                field = pilot.app.query_one("#clip")
                # The clip field is focused at mount; park focus elsewhere so
                # the "before" reading really is the unfocused look.
                pilot.app.query_one("#btn_export").focus()
                for _ in range(10):
                    await pilot.pause(0.05)
                self.assertFalse(field.has_focus)
                before_bg = field.styles.background
                before_colour = field.styles.color
                before_style = field.rich_style

                field.focus()
                for _ in range(20):
                    await pilot.pause(0.05)
                    if field.styles.background != before_bg:
                        break

                self.assertTrue(field.has_focus)
                self.assertNotEqual(
                    before_bg, field.styles.background,
                    f"focused background unchanged: {before_bg}")
                self.assertNotEqual(
                    before_colour, field.styles.color,
                    f"focused colour unchanged: {before_colour}")
                self.assertNotEqual(
                    str(before_style), str(field.rich_style),
                    "focused field renders identically to an unfocused one")

                # A Select is a field too, and gets the same treatment.
                current = pilot.app.query_one("#preset").query_one("SelectCurrent")
                select_bg = current.styles.background
                pilot.app.query_one("#preset").focus()
                for _ in range(20):
                    await pilot.pause(0.05)
                    if current.styles.background != select_bg:
                        break
                self.assertNotEqual(
                    select_bg, current.styles.background,
                    f"focused select background unchanged: {select_bg}")

        self._run(scenario())

    def test_the_editor_uses_the_tstudio_theme(self) -> None:
        """Textual's stock primary is a blue that belongs to nothing here.

        The editor asks tstudio.theme for the brand palette at mount. That is
        what keeps the selection block, the block cursor and the focus border
        off #0178D4, so it is worth a test rather than a hope.
        """
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                self.assertEqual(pilot.app.theme, "tstudio")
                variables = pilot.app.get_css_variables()
                blue = [key for key, value in variables.items()
                        if "#0178d4" in str(value).lower()]
                self.assertEqual(blue, [],
                                 f"textual blue still in: {blue}")

        self._run(scenario())

    def test_reset_puts_every_control_back(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#preset").value = "shorts"
                await _settle(pilot)
                self.assertNotEqual(_value(pilot, "#size"), "keep")

                await pilot.press("f3")
                await _settle(pilot)
                self.assertEqual(_value(pilot, "#clip"), "2-4")
                self.assertEqual(_value(pilot, "#preset"), "none")
                self.assertEqual(_value(pilot, "#size"), "keep")
                self.assertEqual(_value(pilot, "#shape"), "keep")
                self.assertEqual(_value(pilot, "#fps"), "source")
                self.assertIn("lossless", _text(pilot, "#readout"))

        self._run(scenario())


if __name__ == "__main__":
    unittest.main()
