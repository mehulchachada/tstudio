"""Headless tests for the sixel image widget - no sixel ever reaches a terminal.

``render_sixel``, ``write_sixel``, ``sixel_supported`` and ``art_for`` are all
patched at the names the widget module imported them under, so nothing shells
out to ImageMagick and nothing writes escape bytes to a real screen: every
write lands in a list this suite can read back.

Textual is an optional dependency, so these tests skip (rather than fail) when
it is absent. Each scenario runs through ``asyncio.run`` and waits for its
workers before leaving ``run_test()``, so no thread or task is left behind and
the suite stays free of "Task was destroyed" warnings.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:  # Textual is optional for the CLI; skip (rather than fail) if it is absent.
    import textual  # noqa: F401
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False

from tstudio import ClipError
from tstudio.gallery import sixel_widget
from tstudio.gallery.sixel import SIXEL_HINT

# A screen big enough to be interesting, padded so the widget's region does
# NOT start at 0,0 - an off-by-one in the cursor move would otherwise pass.
SCREEN = (80, 24)
PAD_TOP = 2
PAD_LEFT = 4

#: Stand-in sixel payloads. Real sixel starts with DCS 'q'; these do too, so
#: nothing downstream can tell the difference by sniffing the introducer.
SIXEL_A = b"\x1bPq#0;2;100;0;0#0~~~~\x1b\\"
SIXEL_B = b"\x1bPq#0;2;0;0;100#0!!!!\x1b\\"

#: Half-block art rows: a red upper-half block run, as thumbs.art_for returns.
ART_ROWS = [
    "\x1b[38;2;200;20;20m▀▀▀▀\x1b[0m",
    "\x1b[38;2;20;20;200m▀▀▀▀\x1b[0m",
]


def _host(image_view_cls, **kwargs):
    """A one-widget App holding an ImageView inside a padded screen."""
    from textual.app import App, ComposeResult

    class Host(App):
        CSS = "Screen { padding: %d %d; }" % (PAD_TOP, PAD_LEFT)

        def compose(self) -> ComposeResult:
            yield image_view_cls(id="image", **kwargs)

        @property
        def image(self):
            return self.query_one("#image", image_view_cls)

    return Host()


def _plain(render_result) -> str:
    """The text of a render() result, escape bytes and all markup stripped."""
    plain = getattr(render_result, "plain", None)
    return plain if isinstance(plain, str) else str(render_result)


@unittest.skipUnless(_HAS_TEXTUAL, "Textual required")
class SixelWidgetTestCase(unittest.TestCase):
    """Shared plumbing: a temp directory, recorded writes, patched renderers."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.image_a = root / "sunset.png"
        self.image_b = root / "sunrise.png"
        for path in (self.image_a, self.image_b):
            path.write_bytes(b"not really a png - rendering is patched")
        #: Every chunk handed to write_sixel, in order.
        self.writes: list[bytes] = []
        #: Every (path, width_px, height_px) render_sixel was asked for.
        self.rendered: list[tuple[Path, int, int]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- patching ------------------------------------------------------------

    def _record_write(self, data: bytes, stream=None) -> None:
        self.writes.append(bytes(data))

    def _fake_render(self, payloads: dict[Path, bytes], *, delays=None):
        """A render_sixel double: look the payload up by path, optionally slowly."""
        delays = delays or {}

        def render(path, width, height, *, colors=256):
            self.rendered.append((Path(path), width, height))
            delay = delays.get(Path(path))
            if delay:
                time.sleep(delay)
            return payloads[Path(path)]

        return render

    def _patch(self, *, supported: bool, render=None, art=None):
        """Patch the four names the widget module imported, as a single context."""
        patches = [
            mock.patch.object(sixel_widget, "sixel_supported",
                              return_value=supported),
            mock.patch.object(sixel_widget, "write_sixel",
                              side_effect=self._record_write),
            mock.patch.object(
                sixel_widget, "render_sixel",
                side_effect=render or self._fake_render({self.image_a: SIXEL_A,
                                                         self.image_b: SIXEL_B})),
            mock.patch.object(sixel_widget, "art_for",
                              side_effect=art or (lambda *a, **k: list(ART_ROWS))),
        ]
        supported, write, render_mock, art_mock = [p.start() for p in patches]
        for patch in patches:
            self.addCleanup(patch.stop)
        return SimpleNamespace(supported=supported, write=write,
                               render=render_mock, art=art_mock)

    # -- driving -------------------------------------------------------------

    def _run(self, coro):
        return asyncio.run(coro)

    def _joined(self) -> bytes:
        return b"".join(self.writes)


class TestSixelPath(SixelWidgetTestCase):
    """The terminal can do sixel: bytes are rendered off-thread and painted."""

    def test_setting_a_source_paints_the_rendered_bytes(self) -> None:
        doubles = self._patch(supported=True)
        view = sixel_widget.build_image_view()

        async def scenario():
            app = _host(view)
            async with app.run_test(size=SCREEN) as pilot:
                await pilot.pause()
                app.image.source = self.image_a
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()

                self.assertEqual(
                    [call[0] for call in self.rendered], [self.image_a],
                    "render_sixel was not asked for the new source exactly once")
                self.assertIn(SIXEL_A, self.writes,
                              "the rendered bytes never reached write_sixel")
                # The pixel size came from the region, through sixel_size_for.
                _path, width_px, height_px = self.rendered[0]
                region = app.image.region
                self.assertGreaterEqual(width_px, region.width)
                self.assertGreaterEqual(height_px, region.height)
                self.assertFalse(doubles.art.called,
                                 "half-block art was rendered even though "
                                 "sixel is available")

        self._run(scenario())

    def test_image_is_painted_at_the_widget_position(self) -> None:
        self._patch(supported=True)
        view = sixel_widget.build_image_view()

        async def scenario():
            app = _host(view)
            async with app.run_test(size=SCREEN) as pilot:
                await pilot.pause()
                app.image.source = self.image_a
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()

                region = app.image.region
                # The padded screen must actually have moved the widget, or
                # this test would pass on a widget pinned at the origin.
                self.assertEqual((region.x, region.y), (PAD_LEFT, PAD_TOP))

                row = region.y + 1
                col = region.x + 1
                cup = f"\x1b[{row};{col}H".encode("ascii")
                self.assertEqual(cup, sixel_widget.move_to(row, col))
                self.assertIn(cup, self._joined(),
                              "no ESC[row;colH for the widget's top-left cell")

                # The move is immediately before the image, and the cursor is
                # saved before it and restored after it.
                index = self.writes.index(SIXEL_A)
                self.assertGreater(index, 0)
                self.assertEqual(self.writes[index - 1],
                                 sixel_widget.SAVE_CURSOR + cup)
                self.assertEqual(self.writes[index + 1],
                                 sixel_widget.RESTORE_CURSOR)

        self._run(scenario())

    def test_erase_covers_the_whole_region(self) -> None:
        self._patch(supported=True)
        view = sixel_widget.build_image_view()

        async def scenario():
            app = _host(view)
            async with app.run_test(size=SCREEN) as pilot:
                await pilot.pause()
                image = app.image
                region = image.region
                self.assertGreater(region.width, 0)
                self.assertGreater(region.height, 0)

                self.writes.clear()
                image._erase()
                await pilot.pause()

                blanks = b" " * region.width
                rows = [chunk for chunk in self.writes if chunk.endswith(blanks)]
                self.assertEqual(
                    len(rows), region.height,
                    f"erased {len(rows)} rows, not the region's {region.height}")
                for offset, chunk in enumerate(rows):
                    expected = sixel_widget.move_to(region.y + 1 + offset,
                                                    region.x + 1) + blanks
                    self.assertEqual(chunk, expected)
                    # Exactly region.width spaces, no more and no fewer.
                    self.assertEqual(chunk.count(b" "), region.width)
                self.assertEqual(self.writes[0], sixel_widget.SAVE_CURSOR)
                self.assertEqual(self.writes[-1], sixel_widget.RESTORE_CURSOR)

        self._run(scenario())

    def test_a_new_image_erases_the_old_one_first(self) -> None:
        self._patch(supported=True)
        view = sixel_widget.build_image_view()

        async def scenario():
            app = _host(view)
            async with app.run_test(size=SCREEN) as pilot:
                await pilot.pause()
                app.image.source = self.image_a
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()

                region = app.image.region
                blanks = b" " * region.width
                self.writes.clear()
                app.image.repaint()
                await pilot.pause()

                erased = [c for c in self.writes if c.endswith(blanks)]
                self.assertEqual(len(erased), region.height)
                self.assertIn(SIXEL_A, self.writes, "repaint drew no image")
                self.assertLess(self.writes.index(erased[-1]),
                                self.writes.index(SIXEL_A),
                                "the image was drawn before the erase")

        self._run(scenario())

    def test_a_second_source_wins_over_the_first(self) -> None:
        # The first render is deliberately slow, so the second request lands
        # while it is still inside "ImageMagick".
        self._patch(
            supported=True,
            render=self._fake_render({self.image_a: SIXEL_A,
                                      self.image_b: SIXEL_B},
                                     delays={self.image_a: 0.4}),
        )
        view = sixel_widget.build_image_view()

        async def scenario():
            app = _host(view)
            async with app.run_test(size=SCREEN) as pilot:
                await pilot.pause()
                app.image.source = self.image_a
                await asyncio.sleep(0.05)   # let the slow worker get going
                app.image.source = self.image_b
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()

                self.assertIn(SIXEL_B, self.writes,
                              "the last image never reached the terminal")
                self.assertNotIn(SIXEL_A, self.writes,
                                 "the superseded image was painted anyway")
                images = [c for c in self.writes if c in (SIXEL_A, SIXEL_B)]
                self.assertEqual(images[-1], SIXEL_B)
                self.assertEqual(app.image._data, SIXEL_B)

        self._run(scenario())

    def test_a_resize_re_renders_at_the_new_pixel_size(self) -> None:
        self._patch(supported=True)
        view = sixel_widget.build_image_view()

        async def scenario():
            app = _host(view)
            async with app.run_test(size=SCREEN) as pilot:
                await pilot.pause()
                app.image.source = self.image_a
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                first = self.rendered[-1]

                await pilot.resize_terminal(SCREEN[0] // 2, SCREEN[1])
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()

                self.assertGreater(len(self.rendered), 1,
                                   "a resize did not re-render the image")
                last = self.rendered[-1]
                self.assertEqual(last[0], self.image_a)
                self.assertLess(last[1], first[1],
                                "the narrower region asked for the same pixels")

        self._run(scenario())

    def test_hiding_the_widget_erases_the_image(self) -> None:
        self._patch(supported=True)
        view = sixel_widget.build_image_view()

        async def scenario():
            app = _host(view)
            async with app.run_test(size=SCREEN) as pilot:
                await pilot.pause()
                image = app.image
                image.source = self.image_a
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()
                region = image.region
                blanks = b" " * region.width

                self.writes.clear()
                image.display = False
                await pilot.pause()

                erased = [c for c in self.writes if c.endswith(blanks)]
                self.assertEqual(len(erased), region.height,
                                 "hiding the widget left the image on screen")
                self.assertNotIn(SIXEL_A, self.writes)
                self.assertFalse(image._painted)

        self._run(scenario())

    def test_a_cliperror_is_shown_in_the_widget_and_the_app_survives(self) -> None:
        message = "could not render sunset.png - ImageMagick is not installed"

        def explode(path, width, height, *, colors=256):
            self.rendered.append((Path(path), width, height))
            raise ClipError(message)

        self._patch(supported=True, render=explode)
        view = sixel_widget.build_image_view()

        async def scenario():
            app = _host(view)
            async with app.run_test(size=SCREEN) as pilot:
                await pilot.pause()
                app.image.source = self.image_a
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()

                self.assertTrue(self.rendered, "render_sixel was never called")
                self.assertEqual(app.image._error, message)
                self.assertIn(message, _plain(app.image.render()))
                self.assertIsNone(app.image._data)
                # Still alive and still driveable.
                self.assertTrue(app.is_running)
                await pilot.pause()
                self.assertIs(app.image.source, self.image_a)

        self._run(scenario())

    def test_blank_padding_reserves_the_widgets_own_cells(self) -> None:
        self._patch(supported=True)
        view = sixel_widget.build_image_view()

        async def scenario():
            app = _host(view)
            async with app.run_test(size=SCREEN) as pilot:
                await pilot.pause()
                image = app.image
                image.source = self.image_a
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()

                text = _plain(image.render())
                lines = text.split("\n")
                self.assertEqual(len(lines), image.size.height)
                self.assertEqual(set(lines), {" " * image.size.width})

        self._run(scenario())


class TestArtFallback(SixelWidgetTestCase):
    """The terminal cannot do sixel: there is still a picture, made of text."""

    def test_falls_back_to_half_block_art(self) -> None:
        doubles = self._patch(supported=False)
        view = sixel_widget.build_image_view()

        async def scenario():
            app = _host(view)
            async with app.run_test(size=SCREEN) as pilot:
                await pilot.pause()
                app.image.source = self.image_a
                await pilot.pause()
                await app.workers.wait_for_complete()
                await pilot.pause()

                rendered = app.image.render()
                self.assertEqual(
                    self.rendered, [],
                    "render_sixel ran on a terminal that cannot show sixel")
                self.assertFalse(doubles.render.called)
                self.assertTrue(doubles.art.called,
                                "art_for was never asked for the fallback")
                args, kwargs = doubles.art.call_args
                self.assertEqual(args[0], self.image_a)
                self.assertEqual(args[1], app.image.size.width)
                self.assertEqual(args[2], app.image.size.height)
                self.assertIn("at", kwargs)

                text = _plain(rendered)
                self.assertIn("▀▀▀▀", text)
                self.assertNotIn("\x1b", text,
                                 "the ANSI bytes were not parsed into styles")

        self._run(scenario())

    def test_the_art_is_cached_until_something_changes(self) -> None:
        doubles = self._patch(supported=False)
        view = sixel_widget.build_image_view()

        async def scenario():
            app = _host(view)
            async with app.run_test(size=SCREEN) as pilot:
                await pilot.pause()
                image = app.image
                image.source = self.image_a
                await pilot.pause()
                image.render()
                image.render()
                first = doubles.art.call_count
                self.assertEqual(first, 1, "the art was recomputed per render")

                image.at = 12.5
                await pilot.pause()
                image.render()
                self.assertEqual(doubles.art.call_count, 2,
                                 "a new timestamp did not re-render the art")
                self.assertEqual(doubles.art.call_args[1]["at"], 12.5)

        self._run(scenario())

    def test_a_cliperror_from_the_art_is_shown_as_text(self) -> None:
        def explode(*args, **kwargs):
            raise ClipError("could not read sunset.png - not an image")

        self._patch(supported=False, art=explode)
        view = sixel_widget.build_image_view()

        async def scenario():
            app = _host(view)
            async with app.run_test(size=SCREEN) as pilot:
                await pilot.pause()
                app.image.source = self.image_a
                await pilot.pause()
                self.assertIn("not an image", _plain(app.image.render()))
                self.assertTrue(app.is_running)

        self._run(scenario())


@unittest.skipUnless(_HAS_TEXTUAL, "Textual required")
class TestEscapeSequences(unittest.TestCase):
    """The small pure helpers, without an app in the way."""

    def test_move_to_is_one_based_cup(self) -> None:
        self.assertEqual(sixel_widget.move_to(3, 5), b"\x1b[3;5H")
        self.assertEqual(sixel_widget.move_to(1, 1), b"\x1b[1;1H")

    def test_move_to_rejects_zero_based_coordinates(self) -> None:
        with self.assertRaises(ClipError):
            sixel_widget.move_to(0, 1)
        with self.assertRaises(ClipError):
            sixel_widget.move_to(1, 0)

    def test_save_and_restore_are_decsc_and_decrc(self) -> None:
        self.assertEqual(sixel_widget.SAVE_CURSOR, b"\x1b7")
        self.assertEqual(sixel_widget.RESTORE_CURSOR, b"\x1b8")

    def test_background_sgr_is_a_24_bit_colour(self) -> None:
        self.assertEqual(sixel_widget.background_sgr(18, 18, 20),
                         b"\x1b[48;2;18;18;20m")
        with self.assertRaises(ClipError):
            sixel_widget.background_sgr(0, 0, 256)


class TestReason(unittest.TestCase):
    """`sixel_or_art_reason` explains which of the two the user is looking at."""

    def test_reason_says_sixel_when_supported(self) -> None:
        with mock.patch.object(sixel_widget, "sixel_supported",
                               return_value=True):
            reason = sixel_widget.sixel_or_art_reason()
        self.assertIsInstance(reason, str)
        self.assertIn("sixel", reason)
        self.assertNotIn(SIXEL_HINT, reason)

    def test_reason_gives_the_hint_when_not_supported(self) -> None:
        with mock.patch.object(sixel_widget, "sixel_supported",
                               return_value=False):
            reason = sixel_widget.sixel_or_art_reason()
        self.assertIsInstance(reason, str)
        self.assertIn("half-block art", reason)
        self.assertIn(SIXEL_HINT, reason)


class TestModuleHygiene(unittest.TestCase):
    """The module must not drag textual in at import time."""

    def test_nothing_at_module_level_imports_textual(self) -> None:
        source = Path(sixel_widget.__file__).read_text(encoding="utf-8")
        top_level = [line for line in source.splitlines()
                     if line.startswith(("import ", "from "))]
        for line in top_level:
            self.assertNotIn("textual", line)
            self.assertNotIn("rich", line)

    def test_the_builder_returns_a_class_not_an_instance(self) -> None:
        if not _HAS_TEXTUAL:
            self.skipTest("Textual required")
        from textual.widget import Widget

        cls = sixel_widget.build_image_view()
        self.assertIsInstance(cls, type)
        self.assertTrue(issubclass(cls, Widget))
        self.assertEqual(cls.__name__, "ImageView")


if __name__ == "__main__":
    unittest.main()
