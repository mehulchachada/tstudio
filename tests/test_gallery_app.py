"""Headless tests for the gallery screen, driven via run_test().

Textual is an optional dependency: these tests skip if it is absent. They
build a temp folder tree and a Favourites pointed at a temp path, so no test
touches the real favourites file.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:  # Textual is optional for the CLI; skip (rather than fail) if it is absent.
    import textual  # noqa: F401
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False

from tstudio import ClipError
from tstudio.gallery.entries import list_folder
from tstudio.gallery.favourites import Favourites
#: The favourite marker, named once so a glyph swap is a one-line
#: change here rather than a hunt through assertions.
FAV_MARK = "◆"

from tstudio.media import MediaInfo, VideoStream


def _bare(names: list[str]) -> list[str]:
    """Row names with the leading icon removed."""
    return [n.split(" ", 1)[1].strip() if " " in n else n for n in names]


def _text(pilot, selector: str) -> str:
    """Rendered text of a widget by selector, tolerant across Textual versions."""
    w = pilot.app.query_one(selector)
    render = w.render
    if callable(render):
        render = render()
    return render if isinstance(render, str) else str(render)


def _tree(root: Path) -> None:
    """A subfolder, a video, an image, a text file and a dot-file."""
    (root / "originals").mkdir()
    (root / "originals" / "raid.mp4").write_bytes(b"\x00" * 4096)
    (root / "shot.png").write_bytes(b"\x01" * 2048)
    (root / "notes.txt").write_text("hello")
    (root / ".hidden").write_text("secret")


def _app(folder: Path, fav: Favourites):
    from tstudio.gallery_app import build_gallery
    return build_gallery(folder, fav)


async def _settle(pilot) -> None:
    """Let the event loop and any thread workers (the preview) finish.

    One pass is not enough, and the failure it leaves is a confusing one.
    Writing the preview's "loading…" line and starting the worker that
    replaces it both happen in the ``selected`` watcher, so a highlight that
    arrives after ``wait_for_complete`` has returned puts "loading…" back and
    starts a worker nobody is waiting on - leaving a pane that looks settled
    and still reads "loading…". Under the full suite's load that happened
    roughly one run in three.

    So wait repeatedly, until a whole pass starts nothing new. The cap is
    there to keep a genuinely stuck worker from hanging the suite: it hands
    the problem to the assertion that follows instead.
    """
    for _ in range(50):
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()
        if not len(pilot.app.workers):
            return


def _fake_info(path: Path, *, hdr: bool) -> MediaInfo:
    """A probe() result with no ffprobe behind it."""
    video = VideoStream(
        index=0,
        codec="hevc",
        width=1920,
        height=1080,
        fps=30.0,
        pix_fmt="yuv420p10le",
        bit_rate=12_000_000,
        color_primaries="bt2020" if hdr else "bt709",
        color_transfer="smpte2084" if hdr else "bt709",
        color_space="bt2020nc" if hdr else "bt709",
        color_range="tv",
    )
    return MediaInfo(
        path=path,
        duration=229.0,
        size=3_900_000,
        bit_rate=12_000_000,
        video=video,
        audio=(),
        stream_count=2,
    )


def _fake_art(path: Path, cols: int, rows: int, *, at: float | None = None) -> list[str]:
    """Coloured blocks, one ANSI string per row, exactly as art_for returns them."""
    return ["\x1b[38;2;255;170;0m" + "█" * max(1, cols) + "\x1b[0m" for _ in range(max(1, rows))]


def _table_names(pilot) -> list[str]:
    """The Name cell of every row, in order."""
    table = pilot.app.query_one("#files")
    names = []
    for index in range(table.row_count):
        cell = table.get_row_at(index)[0]
        names.append(cell.plain if hasattr(cell, "plain") else str(cell))
    return names


@unittest.skipUnless(_HAS_TEXTUAL, "Textual required")
class TestGalleryApp(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def _setup(self):
        tmp = Path(tempfile.mkdtemp())
        _tree(tmp)
        fav = Favourites(path=tmp / "favourites.json")
        fav.load()
        return tmp, fav

    def test_mounts_and_lists(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    names = _table_names(pilot)
                    self.assertEqual(names[0], "📁 ..")
                    # parent + originals + shot.png + notes.txt (dot-file hidden)
                    self.assertEqual(len(names), 4)
                    self.assertIn("originals", names[1])
                    self.assertIn("notes.txt", names[2])
                    self.assertIn("shot.png", names[3])

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gallery_uses_the_tstudio_theme(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    self.assertEqual(pilot.app.theme, "tstudio")

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_folders_sort_before_files(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    names = _table_names(pilot)
                    self.assertIn("originals", names[1])
                    self.assertNotIn("originals", names[2:])

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_enter_opens_a_folder(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    table = pilot.app.query_one("#files")
                    table.move_cursor(row=1)  # the originals row
                    await pilot.press("enter")
                    await pilot.pause()
                    status = _text(pilot, "#status")
                    self.assertIn("originals", status)
                    names = _table_names(pilot)
                    self.assertEqual(names[0], "📁 ..")
                    self.assertIn("raid.mp4", names[1])

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_backspace_goes_up(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    table = pilot.app.query_one("#files")
                    table.move_cursor(row=1)
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIn("originals", _text(pilot, "#status"))
                    await pilot.press("backspace")
                    await pilot.pause()
                    names = _table_names(pilot)
                    self.assertIn("shot.png", _bare(names))
                    self.assertIn("notes.txt", _bare(names))

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_f_toggles_a_favourite(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    table = pilot.app.query_one("#files")
                    table.move_cursor(row=3)  # shot.png
                    await pilot.press("f")
                    await pilot.pause()
                    self.assertTrue(fav.is_favourite(tmp / "shot.png"))
                    self.assertIn(FAV_MARK, _table_names(pilot)[3])
                    await pilot.press("f")
                    await pilot.pause()
                    self.assertFalse(fav.is_favourite(tmp / "shot.png"))
                    self.assertNotIn(FAV_MARK, _table_names(pilot)[3])

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_enter_on_a_video_opens_the_editor(self) -> None:
        """Enter on a video must hand off to the editor, not sit there."""
        tmp, fav = self._setup()
        try:
            async def scenario():
                (tmp / "clip.mp4").write_bytes(bytes(2048))
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    app = pilot.app
                    table = app.query_one("#files")
                    names = _table_names(pilot)
                    row = next(i for i, n in enumerate(names) if "clip.mp4" in n)
                    table.move_cursor(row=row, column=0)
                    await pilot.pause()
                    with mock.patch("tstudio.gallery_app.subprocess.run") as run,                          mock.patch.object(app, "suspend"):
                        await pilot.press("enter")
                        await pilot.pause()
                    self.assertTrue(run.called)
                    # call_args is only the LAST call, and this patch is not
                    # the only thing reaching subprocess.run: the preview
                    # worker is a thread, and its ffprobe lands here too
                    # whenever it finishes after the Enter handler - which it
                    # does under the load of the full suite, but not when this
                    # test runs alone. Look through every call instead.
                    calls = [c[0][0] for c in run.call_args_list if c[0]]
                    launches = [argv for argv in calls if "--tui" in argv]
                    self.assertEqual(
                        len(launches), 1,
                        f"expected one editor launch among {calls}")
                    argv = launches[0]
                    # a separate process, on the real module entry point
                    self.assertIn("-m", argv)
                    self.assertIn("tstudio", argv)
                    self.assertTrue(argv[-2].endswith(".mp4"))

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_preview_shows_the_kind_not_a_frame(self) -> None:
        """An image gets the picture frame, a video gets the film strip.

        The pane used to try to draw the actual frame. That needs sixel, which
        plenty of terminals do not have, and an ImageMagick convert of a 4K
        source, which takes seconds - so it showed nothing at all for long
        stretches and had no way to say why. It now shows what the file is,
        instantly, in any terminal.
        """
        from tstudio.gallery import icons

        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    app = pilot.app
                    await pilot.pause()
                    table = app.query_one("#files")
                    names = _table_names(pilot)

                    row = next(i for i, n in enumerate(names) if "shot.png" in n)
                    table.move_cursor(row=row, column=0)
                    await _settle(pilot)
                    art = _text(pilot, "#preview-icon")
                    self.assertIn(icons.PICTURE[0].strip(), art)
                    self.assertEqual(_text(pilot, "#preview-action").strip(),
                                     icons.action_for_kind("image"))

                    # The videos live in originals/, not at the top level, and
                    # the fixture is a stub rather than a real mp4 - so probe
                    # is stood in for, as the neighbouring tests do.
                    with mock.patch("tstudio.gallery_app.probe",
                                    side_effect=lambda p: _fake_info(p, hdr=False)):
                        app.folder = tmp / "originals"
                        await pilot.pause()
                        names = _table_names(pilot)
                        row = next(i for i, n in enumerate(names)
                                   if n.endswith(".mp4"))
                        app.query_one("#files").move_cursor(row=row, column=0)
                        await _settle(pilot)
                        art = _text(pilot, "#preview-icon")
                        self.assertIn("▶", art)
                        self.assertEqual(
                            _text(pilot, "#preview-action").strip(),
                            icons.action_for_kind("video"))

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_preview_never_renders_a_frame(self) -> None:
        """A guard, not a preference: frame rendering is what made it blank."""
        import inspect
        from tstudio import gallery_app

        src = inspect.getsource(gallery_app)
        for banned in ("art_for(", "render_sixel", "build_image_view"):
            self.assertNotIn(
                banned, src,
                f"the gallery is rendering frames again: {banned}")

    def test_hidden_toggle(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    names = _table_names(pilot)
                    self.assertNotIn(".hidden", _bare(names))
                    await pilot.press(".")
                    await pilot.pause()
                    self.assertIn(".hidden", _bare(_table_names(pilot)))
                    await pilot.press(".")
                    await pilot.pause()
                    self.assertNotIn(".hidden", _bare(_table_names(pilot)))

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_filter_narrows_the_table(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    await pilot.press("/")
                    await pilot.pause()
                    await pilot.press("s", "h", "o", "t")
                    await pilot.pause()
                    names = _table_names(pilot)
                    self.assertEqual(names[0], "📁 ..")
                    self.assertIn("shot.png", names[1])
                    self.assertNotIn("notes.txt", _bare(names))
                    await pilot.press("escape")
                    await pilot.pause()
                    names = _table_names(pilot)
                    self.assertIn("notes.txt", _bare(names))
                    self.assertIn("shot.png", _bare(names))

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_bad_folder_does_not_exit(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    shutil.rmtree(tmp)
                    await pilot.press("f5")
                    await pilot.pause()
                    self.assertTrue(pilot.app.is_running)
                    self.assertIn("not a folder", _text(pilot, "#status"))

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_ffprobe_in_this_screen(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                def boom(*a, **k):
                    raise AssertionError("subprocess.run must not be called")
                with mock.patch("subprocess.run", side_effect=boom):
                    async with _app(tmp, fav).run_test() as pilot:
                        await pilot.pause()
                        table = pilot.app.query_one("#files")
                        table.move_cursor(row=1)
                        await pilot.press("enter")
                        await pilot.pause()
                        await pilot.press("backspace")
                        await pilot.pause()
                        self.assertTrue(pilot.app.is_running)

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_favourite_raises_a_toast(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                app = _app(tmp, fav)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    pilot.app.query_one("#files").move_cursor(row=3)  # shot.png
                    await pilot.pause()
                    with mock.patch.object(app, "notify") as notify:
                        await pilot.press("f")
                        await pilot.pause()
                        self.assertIn("shot.png", notify.call_args.args[0])
                        self.assertIn("added", notify.call_args.args[0])
                        await pilot.press("f")
                        await pilot.pause()
                        self.assertIn("removed", notify.call_args.args[0])

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_an_unreadable_folder_raises_a_toast(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                app = _app(tmp, fav)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    shutil.rmtree(tmp)
                    with mock.patch.object(app, "notify") as notify:
                        await pilot.press("f5")
                        await pilot.pause()
                        self.assertTrue(notify.called)
                        self.assertEqual(notify.call_args.kwargs["severity"], "error")
                    self.assertIn("not a folder", _text(pilot, "#status"))

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- reactive state --------------------------------------------------

    def test_assigning_folder_reloads_the_listing(self) -> None:
        """No loader is called by hand: setting the reactive is the whole API."""
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    pilot.app.folder = tmp / "originals"
                    await pilot.pause()
                    names = _table_names(pilot)
                    self.assertEqual(names[0], "📁 ..")
                    self.assertIn("raid.mp4", names[1])
                    self.assertIn("originals", _text(pilot, "#status"))
                    pilot.app.folder = tmp
                    await pilot.pause()
                    self.assertIn("shot.png", _bare(_table_names(pilot)))

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_assigning_show_hidden_reloads_the_listing(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    self.assertNotIn(".hidden", _bare(_table_names(pilot)))
                    pilot.app.show_hidden = True
                    await pilot.pause()
                    self.assertIn(".hidden", _bare(_table_names(pilot)))

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_query_text_refilters_without_touching_the_disk(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                with mock.patch("tstudio.gallery_app.list_folder", wraps=list_folder) as lister:
                    async with _app(tmp, fav).run_test() as pilot:
                        await pilot.pause()
                        self.assertTrue(lister.called)  # the initial listing
                        lister.reset_mock()
                        pilot.app.query_text = "shot"
                        await pilot.pause()
                        names = _table_names(pilot)
                        self.assertIn("shot.png", names[1])
                        self.assertNotIn("notes.txt", _bare(names))
                        lister.assert_not_called()
                        pilot.app.query_text = ""
                        await pilot.pause()
                        self.assertIn("notes.txt", _bare(_table_names(pilot)))
                        lister.assert_not_called()

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_highlight_with_no_table_left_does_not_take_the_app_down(self) -> None:
        """NoMatches raised inside a message handler kills the whole app.

        The listing is rebuilt on every folder, filter and show_hidden
        change, and the app can be shutting down, so a queued
        RowHighlighted can land with no #files to look at. About one full
        suite run in three used to die exactly here, which is why this
        asks for None rather than for an exception.
        """
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    await pilot.app.query_one("#files").remove()
                    await pilot.pause()
                    self.assertIsNone(pilot.app._selected_entry())
                    pilot.app.on_data_table_row_highlighted(None)
                    await pilot.pause()
                    self.assertTrue(pilot.app.is_running)

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- preview ---------------------------------------------------------

    def test_preview_shows_the_picture_and_metadata(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                with mock.patch("tstudio.gallery_app.probe",
                                side_effect=lambda p: _fake_info(p, hdr=True)) as probe:
                    async with _app(tmp, fav).run_test() as pilot:
                        await pilot.pause()
                        pilot.app.folder = tmp / "originals"
                        await pilot.pause()
                        table = pilot.app.query_one("#files")
                        table.move_cursor(row=1)  # raid.mp4
                        # The worker is a thread: the facts fill in later.
                        await _settle(pilot)
                        meta = _text(pilot, "#preview-meta")
                        self.assertIn("3:49.000", meta)
                        self.assertIn("1920x1080", meta)
                        self.assertIn("@30fps", meta)
                        self.assertIn("hevc", meta)
                        self.assertIn("HDR10", meta)
                        self.assertIn("3.7 MB", meta)
                        picture = _text(pilot, "#preview-icon")
                        self.assertIn("▶", picture)
                        self.assertNotIn("loading", picture)
                        self.assertIn("raid.mp4", pilot.app.query_one("#preview").border_title)
                        self.assertTrue(probe.called)

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_preview_of_an_image_does_not_probe(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                def boom(*a, **k):
                    raise AssertionError("an image must not be probed")
                with mock.patch("tstudio.gallery_app.probe", side_effect=boom):
                    async with _app(tmp, fav).run_test() as pilot:
                        await pilot.pause()
                        table = pilot.app.query_one("#files")
                        table.move_cursor(row=3)  # shot.png
                        await _settle(pilot)
                        self.assertIn("◉", _text(pilot, "#preview-icon"))
                        self.assertIn("2.0 KB", _text(pilot, "#preview-meta"))

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_preview_shows_a_clip_error_instead_of_crashing(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                with mock.patch("tstudio.gallery_app.probe",
                                side_effect=ClipError("no video stream")):
                    async with _app(tmp, fav).run_test() as pilot:
                        await pilot.pause()
                        pilot.app.folder = tmp / "originals"
                        await pilot.pause()
                        pilot.app.query_one("#files").move_cursor(row=1)
                        await _settle(pilot)
                        self.assertTrue(pilot.app.is_running)
                        self.assertIn("no video stream", _text(pilot, "#preview-icon"))

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_preview_is_empty_for_a_folder(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test() as pilot:
                    await pilot.pause()
                    pilot.app.query_one("#files").move_cursor(row=1)  # originals
                    await _settle(pilot)
                    self.assertIn("select a file", _text(pilot, "#preview-icon"))
                    self.assertEqual(_text(pilot, "#preview-meta").strip(), "")
                    self.assertEqual(pilot.app.query_one("#preview").border_title, "preview")

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_raw_threads_in_the_module(self) -> None:
        """Previews run on Textual's worker API, not on hand-rolled threads."""
        import tstudio.gallery_app as module
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("threading", source)
        self.assertIn('@work(thread=True, exclusive=True, group="preview")', source)

    def test_everything_fits_in_a_100x34_terminal(self) -> None:
        tmp, fav = self._setup()
        try:
            async def scenario():
                async with _app(tmp, fav).run_test(size=(100, 34)) as pilot:
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
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
