"""Headless tests for the optional Textual TUI, driven via run_test().

Textual is an optional dependency: these tests skip if it is not installed or
if ffmpeg/ffprobe is unavailable. They exercise the TUI over the shared
zero-dependency backend (probe / plan_cut / run_cut) and do not require a
display or a real terminal.
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


def _text(pilot, selector: str) -> str:
    """Rendered text of a widget by selector, tolerant across Textual versions."""
    w = pilot.app.query_one(selector)
    render = w.render
    if callable(render):
        render = render()
    return render if isinstance(render, str) else str(render)


def _app(start: str, end: str, out: Path | None = None):
    from tstudio.tui import _build_app
    info = probe(ensure_sample("dense"))
    return _build_app(info, start, end, out)


@unittest.skipUnless(_HAS_TEXTUAL and ffmpeg_available(),
                      "Textual and ffmpeg/ffprobe both required")
class TestTui(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_mount_prefills_and_shows_header(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                start = pilot.app.query_one("#start_input").value
                end = pilot.app.query_one("#end_input").value
                self.assertEqual(start, "2")
                self.assertEqual(end, "4")
                readout = _text(pilot, "#readout")
                self.assertIn("output:", readout)
                self.assertIn("length:", readout)
                self.assertIn("keyframe", readout)

        self._run(scenario())

    def test_lossless_preview_is_a_copy(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                # The preview panel starts hidden; toggle it on first.
                # Focus a non-input widget so the key reaches the app binding.
                pilot.app.query_one("#btn_cut").focus()
                await pilot.press("f2")
                await pilot.pause()
                preview = _text(pilot, "#preview")
                self.assertIn("ffmpeg", preview)
                self.assertIn("-c", preview)
                self.assertIn("copy", preview)

        self._run(scenario())

    def test_switching_to_exact_enables_encoder(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                select = pilot.app.query_one("#mode")
                select.value = "exact"
                await pilot.pause()
                encoder = pilot.app.query_one("#encoder")
                self.assertFalse(encoder.disabled)
                readout = _text(pilot, "#readout")
                self.assertIn("re-encoding with", readout)

        self._run(scenario())

    def test_file_panel_shows_the_duration_and_size(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                panel = _text(pilot, "#file_panel Static")
                self.assertIn("10.000", panel)  # duration
                self.assertRegex(panel, r"\d+(\.\d+)?\s*(B|KB|MB|GB|TB)")  # size

        self._run(scenario())

    def test_command_preview_is_one_block(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#btn_cut").focus()
                await pilot.press("f2")
                await pilot.pause()
                preview = _text(pilot, "#preview")
                self.assertIn("ffmpeg", preview)
                self.assertLessEqual(len(preview.splitlines()), 3)

        self._run(scenario())

    def test_preview_starts_hidden_and_toggles(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                panel = pilot.app.query_one("#preview_panel")
                self.assertFalse(panel.display)
                pilot.app.query_one("#btn_cut").focus()
                await pilot.press("f2")
                await pilot.pause()
                self.assertTrue(panel.display)

        self._run(scenario())

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

    def test_shortcuts_are_live_with_an_input_focused(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#start_input").focus()
                await pilot.pause()
                bindings = pilot.app.active_bindings
                for key in ("f5", "f2", "f3"):
                    self.assertIn(
                        key, bindings,
                        f"{key} not in active bindings: {sorted(bindings)}")

        self._run(scenario())

    def test_cut_writes_a_file(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "out.mp4"
        try:
            async def scenario():
                async with _app("2", "4", out).run_test() as pilot:
                    await pilot.pause()
                    pilot.app.query_one("#btn_cut").press()
                    await pilot.pause()
                    for _ in range(200):
                        if "done" in _text(pilot, "#status"):
                            break
                        await pilot.pause(0.05)
                    status = _text(pilot, "#status")
                    self.assertIn("done", status, status)

            self._run(scenario())
            self.assertTrue(out.exists(), "expected the cut to be written")
            self.assertGreater(out.stat().st_size, 0)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


    def test_task_defaults_to_cut(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                task = pilot.app.query_one("#task").value
                self.assertEqual(task, "cut")
                self.assertTrue(pilot.app.query_one("#mode").display)
                for hid in ("#target", "#preset", "#hdr", "#fit"):
                    self.assertFalse(pilot.app.query_one(hid).display, hid)

        self._run(scenario())

    def test_switching_to_convert_shows_target_and_hdr(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#task").value = "convert"
                await pilot.pause()
                self.assertTrue(pilot.app.query_one("#target").display)
                self.assertTrue(pilot.app.query_one("#hdr").display)
                for hid in ("#mode", "#preset", "#fit"):
                    self.assertFalse(pilot.app.query_one(hid).display, hid)

        self._run(scenario())

    def test_switching_to_social_shows_preset_and_fit(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#task").value = "social"
                await pilot.pause()
                self.assertTrue(pilot.app.query_one("#preset").display)
                self.assertTrue(pilot.app.query_one("#fit").display)
                for hid in ("#mode", "#target", "#hdr"):
                    self.assertFalse(pilot.app.query_one(hid).display, hid)

        self._run(scenario())

    def test_convert_readout_names_the_resolution(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#task").value = "convert"
                pilot.app.query_one("#target").value = "240p"
                await pilot.pause()
                readout = _text(pilot, "#readout")
                self.assertIn("240p", readout)
                self.assertIn("426x240", readout)

        self._run(scenario())

    def test_social_readout_names_the_preset(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#task").value = "social"
                pilot.app.query_one("#preset").value = "shorts"
                await pilot.pause()
                readout = _text(pilot, "#readout")
                self.assertIn("YouTube Shorts", readout)
                self.assertIn("1080x1920", readout)

        self._run(scenario())

    def test_hdr_fixture_readout_says_tonemapped(self) -> None:
        async def scenario():
            from tstudio.tui import _build_app
            info = probe(ensure_sample("hdr"))
            async with _build_app(info, "0", "2", None).run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#task").value = "social"
                await pilot.pause()
                readout = _text(pilot, "#readout")
                self.assertIn("tone-mapped", readout)

        self._run(scenario())

    def test_button_label_follows_the_task(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                btn = pilot.app.query_one("#btn_cut")
                self.assertIn("Cut", str(btn.label))
                pilot.app.query_one("#task").value = "convert"
                await pilot.pause()
                self.assertIn("Convert", str(btn.label))
                pilot.app.query_one("#task").value = "social"
                await pilot.pause()
                self.assertIn("Export", str(btn.label))

        self._run(scenario())

    def test_convert_writes_a_file(self) -> None:
        from tstudio.media import encoders_available
        if "libx264" not in encoders_available():
            self.skipTest("libx264 not available")
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "out.mp4"
        try:
            async def scenario():
                async with _app("2", "4", out).run_test() as pilot:
                    await pilot.pause()
                    pilot.app.query_one("#task").value = "convert"
                    pilot.app.query_one("#target").value = "240p"
                    pilot.app.query_one("#encoder").value = "libx264"
                    pilot.app.query_one("#quality").value = "35"
                    await pilot.pause()
                    pilot.app.query_one("#btn_cut").press()
                    await pilot.pause()
                    for _ in range(400):
                        if "done" in _text(pilot, "#status"):
                            break
                        await pilot.pause(0.05)
                    status = _text(pilot, "#status")
                    self.assertIn("done", status, status)

            self._run(scenario())
            self.assertTrue(out.exists(), "expected the convert to be written")
            self.assertGreater(out.stat().st_size, 0)
            self.assertEqual(probe(out).video.height, 240)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_plan_error_shows_in_the_readout_not_a_crash(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test() as pilot:
                await pilot.pause()
                pilot.app.query_one("#task").value = "convert"
                pilot.app.query_one("#target").value = "2160p"
                await pilot.pause()
                readout = _text(pilot, "#readout")
                self.assertIn("error", readout)
                self.assertFalse(pilot.app._exit)

        self._run(scenario())

    def test_everything_fits_in_a_100x34_terminal_all_tasks(self) -> None:
        async def scenario():
            async with _app("2", "4").run_test(size=(100, 34)) as pilot:
                await pilot.pause()
                for task in ("cut", "convert", "social"):
                    pilot.app.query_one("#task").value = task
                    await pilot.pause()
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
                        f"widgets off-screen at 100x34 (task={task}): " + "; ".join(offenders))

        self._run(scenario())


if __name__ == "__main__":
    unittest.main()
