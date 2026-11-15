"""Tests for lightbox.gallery.entries: classify, list_folder, summary, filter."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lightbox import ClipError
from lightbox.gallery.entries import (
    classify,
    filter_entries,
    folder_summary,
    list_folder,
)


class ClassifyTest(unittest.TestCase):
    def test_classify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sub = root / "sub"
            sub.mkdir()
            mp4 = root / "a.MP4"
            mp4.touch()
            png = root / "b.Png"
            png.touch()
            txt = root / "c.txt"
            txt.touch()
            nosuffix = root / "README"
            nosuffix.touch()
            self.assertEqual(classify(sub), "folder")
            self.assertEqual(classify(mp4), "video")
            self.assertEqual(classify(png), "image")
            self.assertEqual(classify(txt), "other")
            self.assertEqual(classify(nosuffix), "other")


class ListFolderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "sub").mkdir()
        for name in ("clip2.mp4", "clip10.mp4", "Clip1.mp4", "notes.txt", ".hidden.mp4"):
            (self.root / name).touch()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_parent_entry_first(self) -> None:
        entries = list_folder(self.root)
        self.assertTrue(entries[0].is_parent)
        self.assertEqual(entries[0].name, "..")
        self.assertEqual(entries[0].kind, "folder")

    def test_no_parent_at_root(self) -> None:
        root = Path(Path(self.root).anchor)
        if not root.is_dir():
            self.skipTest(f"{root} is not a listable directory")
        entries = list_folder(root)
        self.assertFalse(any(e.is_parent for e in entries))

    def test_folders_before_files(self) -> None:
        entries = list_folder(self.root)
        kinds = [e.kind for e in entries[1:]]
        self.assertEqual(kinds, sorted(kinds, key=lambda k: k != "folder"))

    def test_natural_sort_puts_clip2_before_clip10(self) -> None:
        entries = list_folder(self.root)
        names = [e.name for e in entries if e.name in ("Clip1.mp4", "clip2.mp4", "clip10.mp4")]
        self.assertEqual(names, ["Clip1.mp4", "clip2.mp4", "clip10.mp4"])

    def test_hidden_default_and_shown(self) -> None:
        self.assertNotIn(".hidden.mp4", [e.name for e in list_folder(self.root)])
        self.assertIn(".hidden.mp4", [e.name for e in list_folder(self.root, show_hidden=True)])

    def test_kinds_keeps_folders_and_parent(self) -> None:
        entries = list_folder(self.root, kinds=frozenset({"video"}))
        names = [e.name for e in entries]
        self.assertNotIn("notes.txt", names)
        self.assertIn("sub", names)
        self.assertTrue(entries[0].is_parent)


class SummaryFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "sub").mkdir()
        (self.root / "a.mp4").write_bytes(b"x" * 100)
        (self.root / "b.txt").write_bytes(b"y" * 50)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_folder_summary(self) -> None:
        entries = list_folder(self.root)
        self.assertEqual(folder_summary(entries), (3, 150))

    def test_filter_entries(self) -> None:
        entries = list_folder(self.root)
        self.assertEqual([e.name for e in filter_entries(entries, "MP4")], ["..", "a.mp4"])
        self.assertEqual([e.name for e in filter_entries(entries, "zzz")], [".."])
        self.assertEqual(filter_entries(entries, "   "), entries)
        self.assertEqual(filter_entries(entries, ""), entries)


class ErrorsTest(unittest.TestCase):
    def test_missing_folder_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ClipError):
                list_folder(Path(tmp) / "nope")

    def test_file_not_folder_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.mp4"
            f.touch()
            with self.assertRaises(ClipError):
                list_folder(f)


if __name__ == "__main__":
    unittest.main()
