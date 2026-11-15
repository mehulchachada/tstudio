"""Tests for lightbox.gallery.favourites: persistence, toggle, recent, prune."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lightbox.gallery.favourites import Favourites


class FavouritesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "favourites.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_round_trip(self) -> None:
        sub = self.root / "sub"
        sub.mkdir()
        f = self.root / "a.mp4"
        f.touch()
        favs = Favourites(self.path)
        favs.toggle(sub)
        favs.toggle(f)
        favs.save()
        fresh = Favourites(self.path)
        fresh.load()
        self.assertTrue(fresh.is_favourite(sub))
        self.assertTrue(fresh.is_favourite(f))

    def test_toggle_is_case_insensitive_on_windows(self) -> None:
        if not self.root.anchor.lower().startswith(("c:", "d:", "e:")):
            self.skipTest("not on a Windows drive")
        favs = Favourites(self.path)
        favs.toggle(Path("C:\\A\\b.mp4"))
        self.assertTrue(favs.is_favourite(Path("c:\\a\\B.MP4")))
        self.assertFalse(favs.toggle(Path("c:\\a\\B.MP4")))
        self.assertEqual(favs.files, [])
        self.assertEqual(favs.folders, [])

    def test_toggle_state_and_buckets(self) -> None:
        sub = self.root / "sub"
        sub.mkdir()
        f = self.root / "a.mp4"
        f.touch()
        favs = Favourites(self.path)
        self.assertTrue(favs.toggle(sub))
        self.assertEqual([e.path for e in favs.folders], [sub])
        self.assertFalse(favs.toggle(sub))
        self.assertEqual(favs.folders, [])
        self.assertTrue(favs.toggle(f))
        self.assertEqual([e.path for e in favs.files], [f])

    def test_a_corrupt_file_does_not_raise(self) -> None:
        self.path.write_text("{", encoding="utf-8")
        favs = Favourites(self.path)
        favs.load()
        self.assertEqual(favs.folders, [])
        self.assertEqual(favs.files, [])
        self.assertEqual(favs.recent, [])
        self.assertTrue(favs.toggle(self.root))

        self.path.write_text('{"version": 99, "folders": []}', encoding="utf-8")
        favs2 = Favourites(self.path)
        favs2.load()
        self.assertEqual(favs2.folders, [])
        self.assertEqual(favs2.files, [])

    def test_add_recent_dedupes_and_caps(self) -> None:
        favs = Favourites(self.path)
        for i in range(60):
            favs.add_recent(Path(f"clip{i}.mp4"))
        favs.add_recent(Path("clip59.mp4"))
        self.assertEqual(len(favs.recent), 50)
        self.assertEqual(favs.recent[0].path, Path("clip59.mp4"))
        self.assertEqual(len({str(e.path) for e in favs.recent}), 50)

    def test_save_is_atomic(self) -> None:
        favs = Favourites(self.path)
        favs.toggle(self.root)
        favs.save()
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_prune_missing_reports_only(self) -> None:
        f = self.root / "gone.mp4"
        f.touch()
        favs = Favourites(self.path)
        favs.toggle(f)
        f.unlink()
        missing = favs.prune_missing()
        self.assertEqual(missing, [f])
        self.assertEqual([e.path for e in favs.files], [f])


if __name__ == "__main__":
    unittest.main()
