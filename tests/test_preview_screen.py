"""Headless tests for the clip preview dialog, driven via run_test().

Nothing here is allowed to shell out: `art_for`, `probe`, `mpv_available` and
`play_with_mpv` are all patched at the names the dialog imported them under,
so ffmpeg, ImageMagick and mpv are never touched. Textual is an optional
dependency, so the whole module skips (rather than fails) without it.

Every scenario runs through asyncio.run and waits for the render worker to
finish before leaving `run_test()`, so no thread or task outlives its app and
the output stays free of "Task was destroyed" warnings.
"""

from __future__ import annotations

import asyncio
import re
import unittest
from pathlib import Path
from unittest import mock

try:  # Textual is optional for the CLI; skip (rather than fail) if it is absent.
    import textual  # noqa: F401
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False

from tstudio import ClipError
from tstudio.gallery import branding
from tstudio.gallery.playback import MPV_HINT

MODULE = "tstudio.gallery.preview_screen"

CLIP = Path("C:/clips/a-rather-long-holiday-clip-from-a-phone.mp4")

_UNSET = object()


def _host(factory):
    """A one-screen App that pushes `factory()` and records what it dismisses."""
    from textual.app import App

    class Host(App):
        def __init__(self) -> None:
            super().__init__()
            self.result = _UNSET

        def on_mount(self) -> None:
            self.push_screen(factory(), self._record)

        def _record(self, value) -> None:
            self.result = value

    return Host()


def _text_of(node) -> str:
    """The plain text a Static is showing, however Textual wraps it."""
    render = node.render
    if callable(render):
        render = render()
    return render if isinstance(render, str) else str(render)


def _screen_text(pilot) -> str:
    """Every Static line on the current screen, joined with newlines."""
    from textual.widgets import Static

    return "\n".join(_text_of(node)
                     for node in pilot.app.screen.query(Static))


def _offenders(pilot, width: int, height: int) -> list[str]:
    """Widgets whose region pokes outside a width x height terminal."""
    out: list[str] = []
    for node in pilot.app.screen.walk_children():
        if type(node).__name__ == "Tooltip":
            continue  # Textual parks tooltips off-screen deliberately
        region = node.region
        if region.width == 0 and region.height == 0:
            continue
        if region.y + region.height > height or region.x + region.width > width:
            out.append(f"{type(node).__name__}#{node.id} {region}")
    return out


class _FakeArt:
    """A stand-in for `art_for` that records its calls and returns instantly."""

    def __init__(self, error: ClipError | None = None) -> None:
        self.calls: list[tuple[Path, int, int, float | None]] = []
        self.error = error

    def __call__(self, path, cols, rows, *, at=None):
        self.calls.append((path, cols, rows, at))
        if self.error is not None:
            raise self.error
        return [f"frame@{at:.3f} row {index}" for index in range(rows)]

    @property
    def stamps(self) -> list[float]:
        return [call[3] for call in self.calls]


class _Suspension:
    """A stand-in for `App.suspend` that records being entered and exited."""

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc_info) -> bool:
        self.exited += 1
        return False


@unittest.skipUnless(_HAS_TEXTUAL, "Textual required")
class PreviewScreenTestCase(unittest.TestCase):
    """Shared plumbing: a patched dialog, and a way to wait for a render."""

    def setUp(self) -> None:
        self.art = _FakeArt()
        self.probe = mock.Mock(
            return_value=mock.Mock(duration=120.0), name="probe")
        self.mpv_available = mock.Mock(return_value=False, name="mpv_available")
        self.play_with_mpv = mock.Mock(return_value=0, name="play_with_mpv")
        for name, double in (("art_for", self.art),
                             ("probe", self.probe),
                             ("mpv_available", self.mpv_available),
                             ("play_with_mpv", self.play_with_mpv)):
            patcher = mock.patch(f"{MODULE}.{name}", double)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, coro):
        return asyncio.run(coro)

    def _host(self, **kwargs):
        from tstudio.gallery.preview_screen import build_preview_screen

        return _host(lambda: build_preview_screen(CLIP, **kwargs))

    async def _settle(self, pilot) -> None:
        """Let the render thread finish and its result land on the screen."""
        for _ in range(40):
            await pilot.pause()
            try:
                await pilot.app.workers.wait_for_complete()
            except Exception:
                pass  # a superseded exclusive render; cancellation is the point
            await pilot.pause()
            if not pilot.app.workers._workers:
                break

    def _expected_size(self, pilot) -> tuple[int, int]:
        """The cols/rows the dialog should have asked `art_for` for.

        The numbers are written out rather than read back off the module, so
        this says what the sizing rule *is*: the dialog's own content box,
        less the three rows the caption and hints take, never below 20x8.
        """
        dialog = pilot.app.screen.query_one("#dialog")
        return (max(20, dialog.size.width), max(8, dialog.size.height - 3))

    def _caption(self, pilot) -> str:
        from textual.widgets import Static

        return _text_of(pilot.app.screen.query_one("#caption", Static))

    def _hints(self, pilot) -> str:
        from textual.widgets import Static

        return _text_of(pilot.app.screen.query_one("#hints", Static))

    def _frame(self, pilot) -> str:
        from textual.widgets import Static

        return _text_of(pilot.app.screen.query_one("#frame", Static))


class TestPreviewMount(PreviewScreenTestCase):
    def test_the_dialog_mounts_and_shows_the_first_frame(self) -> None:
        async def scenario():
            async with self._host().run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                screen = pilot.app.screen
                self.assertTrue(self.art.calls, "art_for was never called")
                path, cols, rows, at = self.art.calls[-1]
                self.assertEqual(path, CLIP)
                self.assertEqual((cols, rows), self._expected_size(pilot))
                # The frame it asked for is the frame it has room for: the
                # caption and hints really do get their three rows back.
                frame = screen.query_one("#frame")
                dialog = screen.query_one("#dialog")
                self.assertEqual(frame.region.height, rows)
                self.assertEqual(frame.region.width, cols)
                self.assertEqual(dialog.size.height - frame.region.height, 3)
                self.assertEqual(at, screen.timestamps[0])
                self.assertEqual(at, 0.0)
                # The frame rendered, so the loading line is gone.
                shown = self._frame(pilot)
                self.assertIn("frame@0.000", shown)
                self.assertNotIn("loading", shown)

        self._run(scenario())

    def test_the_frame_is_never_smaller_than_the_floor(self) -> None:
        async def scenario():
            async with self._host().run_test(size=(24, 10)) as pilot:
                await self._settle(pilot)
                _path, cols, rows, _at = self.art.calls[-1]
                self.assertGreaterEqual(cols, 20)
                self.assertGreaterEqual(rows, 8)

        self._run(scenario())

    def test_the_border_title_is_the_file_name_cut_to_fit(self) -> None:
        async def scenario():
            async with self._host().run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                dialog = pilot.app.screen.query_one("#dialog")
                title = str(dialog.border_title)
                self.assertLessEqual(len(title), dialog.size.width)
                self.assertTrue(CLIP.name.startswith(title.rstrip("…")),
                                f"unexpected border title {title!r}")
                kind, colour = dialog.styles.border_top
                self.assertEqual(kind, "round")
                self.assertEqual(colour.hex.lower(), branding.AMBER.lower())

        self._run(scenario())

    def test_the_dialog_is_large_and_centred_over_a_dimmed_screen(self) -> None:
        async def scenario():
            async with self._host().run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                screen = pilot.app.screen
                dialog = screen.query_one("#dialog")
                self.assertEqual(dialog.region.width, 80)   # 80% of 100
                self.assertEqual(dialog.region.height, 27)  # 80% of 34
                self.assertGreater(dialog.region.x, 0, "not centred")
                self.assertLess(screen.styles.background.a, 1.0,
                                "the modal screen is not translucent")
                self.assertEqual(screen.styles.align_horizontal, "center")
                self.assertEqual(screen.styles.align_vertical, "middle")

        self._run(scenario())

    def test_textual_is_not_imported_at_module_level(self) -> None:
        from tstudio.gallery import preview_screen as ps

        source = Path(ps.__file__).read_text(encoding="utf-8").splitlines()
        offenders = [line for line in source
                     if re.match(r"(import|from)\s+(textual|rich)\b", line)]
        self.assertEqual(offenders, [],
                         f"textual/rich imported at module level: {offenders}")


class TestPreviewScrubbing(PreviewScreenTestCase):
    def test_right_advances_and_left_goes_back(self) -> None:
        async def scenario():
            async with self._host().run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                screen = pilot.app.screen
                stamps = list(screen.timestamps)
                self.assertEqual(len(stamps), 10)

                await pilot.press("right")
                await self._settle(pilot)
                self.assertEqual(screen.index, 1)
                self.assertEqual(self.art.stamps[-1], stamps[1])
                self.assertIn(f"frame@{stamps[1]:.3f}", self._frame(pilot))

                await pilot.press("right")
                await self._settle(pilot)
                self.assertEqual(screen.index, 2)
                self.assertEqual(self.art.stamps[-1], stamps[2])

                await pilot.press("left")
                await self._settle(pilot)
                self.assertEqual(screen.index, 1)
                self.assertEqual(self.art.stamps[-1], stamps[1])

        self._run(scenario())

    def test_h_and_l_scrub_like_the_arrows(self) -> None:
        async def scenario():
            async with self._host().run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                screen = pilot.app.screen
                await pilot.press("l")
                await self._settle(pilot)
                self.assertEqual(screen.index, 1)
                await pilot.press("h")
                await self._settle(pilot)
                self.assertEqual(screen.index, 0)

        self._run(scenario())

    def test_home_and_end_jump_to_the_first_and_last(self) -> None:
        async def scenario():
            async with self._host().run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                screen = pilot.app.screen
                stamps = list(screen.timestamps)

                await pilot.press("end")
                await self._settle(pilot)
                self.assertEqual(screen.index, len(stamps) - 1)
                self.assertEqual(self.art.stamps[-1], stamps[-1])

                await pilot.press("home")
                await self._settle(pilot)
                self.assertEqual(screen.index, 0)
                self.assertEqual(self.art.stamps[-1], stamps[0])

        self._run(scenario())

    def test_stepping_past_the_end_is_safe(self) -> None:
        async def scenario():
            async with self._host().run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                screen = pilot.app.screen
                last = len(screen.timestamps) - 1

                for _ in range(len(screen.timestamps) + 5):
                    await pilot.press("right")
                await self._settle(pilot)
                self.assertEqual(screen.index, last)
                self.assertTrue(pilot.app.is_running, "the app fell over")

                for _ in range(len(screen.timestamps) + 5):
                    await pilot.press("left")
                await self._settle(pilot)
                self.assertEqual(screen.index, 0)
                self.assertTrue(pilot.app.is_running, "the app fell over")

                # Nothing was ever asked for outside the list of timestamps.
                for stamp in self.art.stamps:
                    self.assertIn(stamp, screen.timestamps)

        self._run(scenario())

    def test_scrubbing_stays_inside_the_clip(self) -> None:
        async def scenario():
            host = self._host(start=229.0, end=259.0)
            async with host.run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                stamps = list(pilot.app.screen.timestamps)
                self.assertEqual(len(stamps), 10)
                self.assertEqual(stamps[0], 229.0)
                for stamp in stamps:
                    self.assertGreaterEqual(stamp, 229.0)
                    self.assertLess(stamp, 259.0)
                # Both bounds were given, so the duration never had to be probed.
                self.probe.assert_not_called()
                # And every frame actually requested sits in the clip.
                for stamp in self.art.stamps:
                    self.assertGreaterEqual(stamp, 229.0)
                    self.assertLess(stamp, 259.0)

        self._run(scenario())


class TestPreviewCaption(PreviewScreenTestCase):
    def test_the_caption_shows_the_time_and_the_position(self) -> None:
        async def scenario():
            async with self._host().run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                caption = self._caption(pilot)
                self.assertIn("0:00.000", caption)
                self.assertRegex(caption, r"\b1/10\b")

                await pilot.press("right")
                await pilot.press("right")
                await self._settle(pilot)
                caption = self._caption(pilot)
                self.assertRegex(caption, r"\b3/10\b")
                from tstudio.timespec import format_time

                self.assertIn(format_time(pilot.app.screen.timestamp), caption)

        self._run(scenario())

    def test_the_caption_shows_the_clip_bounds(self) -> None:
        async def scenario():
            host = self._host(start=229.0, end=259.0)
            async with host.run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                caption = self._caption(pilot)
                self.assertIn("in 3:49.000", caption)
                self.assertIn("out 4:19.000", caption)
                self.assertIn("3:49.000", caption)
                self.assertRegex(caption, r"\b1/10\b")

        self._run(scenario())


class TestPreviewPlayback(PreviewScreenTestCase):
    def test_p_plays_with_mpv_inside_a_suspend(self) -> None:
        from textual.app import App

        self.mpv_available.return_value = True
        suspension = _Suspension()
        seen: list[int] = []
        self.play_with_mpv.side_effect = lambda path: seen.append(
            suspension.entered - suspension.exited) or 0

        async def scenario():
            with mock.patch.object(App, "suspend", lambda self: suspension):
                async with self._host().run_test(size=(100, 34)) as pilot:
                    await self._settle(pilot)
                    await pilot.press("p")
                    await self._settle(pilot)
                    self.play_with_mpv.assert_called_once_with(CLIP)
                    self.assertEqual(suspension.entered, 1)
                    self.assertEqual(suspension.exited, 1)
                    self.assertEqual(seen, [1],
                                     "mpv did not run inside the suspension")
                    self.assertTrue(pilot.app.is_running)

        self._run(scenario())

    def test_p_without_mpv_shows_the_hint_instead(self) -> None:
        self.mpv_available.return_value = False

        async def scenario():
            async with self._host().run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                await pilot.press("p")
                await self._settle(pilot)
                self.play_with_mpv.assert_not_called()
                self.assertIn(MPV_HINT, self._hints(pilot))
                self.assertIn(MPV_HINT, _screen_text(pilot))
                self.assertTrue(pilot.app.is_running)

        self._run(scenario())


class TestPreviewDismiss(PreviewScreenTestCase):
    def test_escape_dismisses_with_none(self) -> None:
        async def scenario():
            async with self._host().run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsNone(pilot.app.result)

        self._run(scenario())

    def test_q_dismisses_with_none(self) -> None:
        async def scenario():
            async with self._host().run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                await pilot.press("q")
                await pilot.pause()
                self.assertIsNone(pilot.app.result)

        self._run(scenario())


class TestPreviewErrors(PreviewScreenTestCase):
    def test_a_cliperror_from_art_for_is_shown_in_the_dialog(self) -> None:
        self.art.error = ClipError("ImageMagick is not on PATH")

        async def scenario():
            async with self._host().run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                self.assertIn("ImageMagick is not on PATH", self._frame(pilot))
                self.assertIn("ImageMagick is not on PATH", _screen_text(pilot))
                frame = pilot.app.screen.query_one("#frame")
                self.assertEqual(frame.styles.color.hex.lower(),
                                 branding.RED.lower())
                self.assertTrue(pilot.app.is_running, "the error killed the app")
                # Still usable: scrubbing after an error does not crash either.
                await pilot.press("right")
                await self._settle(pilot)
                self.assertTrue(pilot.app.is_running)

        self._run(scenario())

    def test_a_cliperror_from_probe_is_shown_in_the_dialog(self) -> None:
        self.probe.side_effect = ClipError("not a media file")

        async def scenario():
            async with self._host().run_test(size=(100, 34)) as pilot:
                await self._settle(pilot)
                self.assertIn("not a media file", self._frame(pilot))
                self.art.calls.clear()
                await pilot.press("right")
                await self._settle(pilot)
                self.assertEqual(self.art.calls, [],
                                 "rendered a frame with no timestamps")
                self.assertTrue(pilot.app.is_running, "the error killed the app")

        self._run(scenario())


class TestPreviewLayout(PreviewScreenTestCase):
    def test_the_dialog_fits_in_a_100x34_terminal(self) -> None:
        async def scenario():
            async with self._host(start=229.0, end=259.0).run_test(
                    size=(100, 34)) as pilot:
                await self._settle(pilot)
                offenders = _offenders(pilot, 100, 34)
                self.assertEqual(
                    offenders, [],
                    "widgets off-screen at 100x34: " + "; ".join(offenders))
                dialog = pilot.app.screen.query_one("#dialog")
                self.assertLessEqual(dialog.region.right, 100)
                self.assertLessEqual(dialog.region.bottom, 34)

        self._run(scenario())


if __name__ == "__main__":
    unittest.main()
