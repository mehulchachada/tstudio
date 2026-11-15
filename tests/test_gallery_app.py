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

from tstudio.gallery.favourites import Favourites


def _bare(names: list[str]) -> list[str]:
    """Row names with the leading icon removed."""
    return [n.split(" ", 1)[1] if " " in n else n for n in names]


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
                    self.assertIn("★", _table_names(pilot)[3])
                    await pilot.press("f")
                    await pilot.pause()
                    self.assertFalse(fav.is_favourite(tmp / "shot.png"))
                    self.assertNotIn("★", _table_names(pilot)[3])

            self._run(scenario())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

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
