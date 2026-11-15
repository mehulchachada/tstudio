"""Tests for vidclip.timespec: time and range parsing, naming (pure logic)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vidclip import ClipError
from vidclip import timespec


class ParseTimeAccepted(unittest.TestCase):
    def test_accepted_examples(self):
        rows = [
            ("90", 90.0),
            ("90.5", 90.5),
            ("0", 0.0),
            (".5", 0.5),
            ("1:30", 90.0),
            ("1:30.25", 90.25),
            ("1:02:03", 3723.0),
            ("1:02:03.5", 3723.5),
            ("0:05", 5.0),
            ("75:00", 4500.0),
            ("90s", 90.0),
            ("1.5s", 1.5),
            ("2m", 120.0),
            ("2m30s", 150.0),
            ("1h2m3s", 3723.0),
            ("1h30m", 5400.0),
            ("500ms", 0.5),
            ("250ms", 0.25),
            ("  3:49  ", 229.0),
            ("2M30S", 150.0),
        ]
        for text, expected in rows:
            with self.subTest(text=text):
                self.assertAlmostEqual(timespec.parse_time(text), expected, places=6)

    def test_colon_minutes_are_not_a_decimal(self):
        self.assertEqual(timespec.parse_time("3:49"), 229.0)
        self.assertNotEqual(timespec.parse_time("3:49"), 3.49)

    def test_ms_beats_minutes(self):
        self.assertAlmostEqual(timespec.parse_time("500ms"), 0.5, places=6)

    def test_bare_number_is_seconds_not_minutes(self):
        self.assertEqual(timespec.parse_time("90"), 90.0)


class ParseTimeRejected(unittest.TestCase):
    def test_rejections(self):
        rows = [
            "",
            "   ",
            "abc",
            "-5",
            "1:-5",
            ":",
            "1:",
            ":30",
            ".",
            "1:2:3:4",
            "1h2x",
            "5m1h",
            "1h1h",
            "3:49-4:30",
            "1.5:30",
            "1:75",
            "1:75:00",
            None,
            90,
            3.5,
        ]
        for text in rows:
            with self.subTest(text=text):
                with self.assertRaises(ClipError):
                    timespec.parse_time(text)


class ParseRange(unittest.TestCase):
    def test_accepted_examples(self):
        rows = [
            ("3:49-4:30", (229.0, 270.0)),
            ("3:49 - 4:30", (229.0, 270.0)),
            ("3:49-", (229.0, None)),
            ("3:49", (229.0, None)),
            ("3:49-end", (229.0, None)),
            ("3:49-EOF", (229.0, None)),
            ("1h2m3s-1h3m", (3723.0, 3780.0)),
        ]
        for text, (start, end) in rows:
            with self.subTest(text=text):
                got_start, got_end = timespec.parse_range(text)
                self.assertAlmostEqual(got_start, start, places=6)
                if end is None:
                    self.assertIsNone(got_end)
                else:
                    self.assertAlmostEqual(got_end, end, places=6)

    def test_rejections(self):
        rows = ["-4:30", "4:30-3:49", "3:49-3:49", ""]
        for text in rows:
            with self.subTest(text=text):
                with self.assertRaises(ClipError):
                    timespec.parse_range(text)

    def test_equal_start_and_end_is_an_error(self):
        with self.assertRaises(ClipError):
            timespec.parse_range("3:49-3:49")


class FormatTime(unittest.TestCase):
    def test_accepted_examples(self):
        rows = [
            (229.0, "3:49.000"),
            (229.5, "3:49.500"),
            (5.25, "0:05.250"),
            (65.0, "1:05.000"),
            (3723.5, "1:02:03.500"),
            (0.0, "0:00.000"),
        ]
        for seconds, expected in rows:
            with self.subTest(seconds=seconds):
                self.assertEqual(timespec.format_time(seconds), expected)

    def test_rollover_rounds_up_cleanly(self):
        self.assertEqual(timespec.format_time(59.9999), "1:00.000")

    def test_rejections(self):
        for text in ["", -5.0, "abc", None]:
            with self.subTest(text=text):
                with self.assertRaises(ClipError):
                    timespec.format_time(text)


class SlugTime(unittest.TestCase):
    def test_accepted_examples(self):
        rows = [
            (229.0, "3-49"),
            (229.5, "3-49.5"),
            (229.25, "3-49.25"),
            (3723.0, "1-02-03"),
            (5.0, "0-05"),
            (229.500, "3-49.5"),
        ]
        for seconds, expected in rows:
            with self.subTest(seconds=seconds):
                self.assertEqual(timespec.slug_time(seconds), expected)


class Naming(unittest.TestCase):
    def test_output_name(self):
        rows = [
            (Path("C:/v/raid.mp4"), 229.0, 270.0, Path("C:/v/raid_3-49_to_4-30.mp4")),
            (Path("C:/v/raid.mp4"), 229.0, None, Path("C:/v/raid_3-49_to_end.mp4")),
            (Path("C:/v/a.b.c.mkv"), 5.0, None, Path("C:/v/a.b.c_0-05_to_end.mkv")),
            (Path("C:/v/notes"), 5.0, 6.0, Path("C:/v/notes_0-05_to_0-06")),
        ]
        for src, start, end, expected in rows:
            with self.subTest(src=src, start=start, end=end):
                self.assertEqual(timespec.output_name(src, start, end), expected)

    def test_output_name_tagged(self):
        rows = [
            (Path("C:/v/raid.mp4"), 229.0, 259.0, "shorts", Path("C:/v/raid_3-49_to_4-19_shorts.mp4")),
            (Path("C:/v/raid.mp4"), 229.0, None, "1080p", Path("C:/v/raid_3-49_to_end_1080p.mp4")),
            (Path("C:/v/raid.mp4"), None, None, "1080p", Path("C:/v/raid_1080p.mp4")),
            (Path("C:/v/a.b.c.mkv"), None, None, "720p", Path("C:/v/a.b.c_720p.mkv")),
        ]
        for src, start, end, tag, expected in rows:
            with self.subTest(src=src, start=start, end=end, tag=tag):
                self.assertEqual(timespec.output_name_tagged(src, start, end, tag), expected)

    def test_output_name_unchanged(self):
        src = Path("C:/v/raid.mp4")
        self.assertEqual(
            timespec.output_name(src, 229.0, 259.0),
            Path("C:/v/raid_3-49_to_4-19.mp4"),
        )
        self.assertEqual(
            timespec.output_name(src, 229.0, None),
            Path("C:/v/raid_3-49_to_end.mp4"),
        )

    def test_unique_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "raid_3-49_to_end.mp4"
            self.assertEqual(timespec.unique_name(base), base)
            base.touch()
            self.assertEqual(timespec.unique_name(base), Path(tmp) / "raid_3-49_to_end_2.mp4")
            (Path(tmp) / "raid_3-49_to_end_2.mp4").touch()
            self.assertEqual(timespec.unique_name(base), Path(tmp) / "raid_3-49_to_end_3.mp4")

    def test_unique_name_exhausts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "clip.mp4"
            base.touch()
            for i in range(2, 1000):
                (Path(tmp) / f"clip_{i}.mp4").touch()
            with self.assertRaises(ClipError):
                timespec.unique_name(base)


class ResolveRelative(unittest.TestCase):
    def _check(self, text, duration, start, end):
        got_start, got_end = timespec.resolve_relative(text, duration)
        self.assertAlmostEqual(got_start, start, places=6, msg=text)
        if end is None:
            self.assertIsNone(got_end)
        else:
            self.assertAlmostEqual(got_end, end, places=6, msg=text)

    def test_first_percent(self):
        self._check("first 10%", 100.0, 0.0, 10.0)
        self._check("first 50%", 60.0, 0.0, 30.0)

    def test_first_seconds(self):
        self._check("first 30s", 100.0, 0.0, 30.0)
        self._check("first 2m", 600.0, 0.0, 120.0)

    def test_last_percent(self):
        self._check("last 20%", 100.0, 80.0, 100.0)
        self._check("last 100%", 100.0, 0.0, 100.0)

    def test_last_seconds(self):
        self._check("last 45s", 100.0, 55.0, 100.0)
        # span longer than duration clamps to the whole file
        self._check("last 300s", 100.0, 0.0, 100.0)

    def test_first_clamps_at_duration(self):
        self._check("first 300s", 100.0, 0.0, 100.0)

    def test_around_default_window(self):
        self._check("around 3:49", 1000.0, 224.0, 234.0)

    def test_around_clamped_to_start(self):
        # window that would start before 0 is clamped
        self._check("around 2s", 100.0, 0.0, 7.0)

    def test_around_clamped_to_end(self):
        # window that would end after duration is clamped
        self._check("around 98s", 100.0, 93.0, 100.0)

    def test_around_custom_window(self):
        self._check("around 100s +20s", 500.0, 80.0, 120.0)
        self._check("around 100s +20s-30s", 500.0, 70.0, 120.0)

    def test_around_empty_window_rejected(self):
        with self.assertRaises(ClipError):
            # 100%+0% gives center=0, before=0, after=0 -> empty
            timespec.resolve_relative("around 0s +0s-0s", 100.0)

    def test_rejects_non_relative(self):
        for text in ["3:49", "3:49-4:30", "middle 10%", "first", "", "first 10% extra"]:
            with self.subTest(text=text):
                with self.assertRaises(ClipError):
                    timespec.resolve_relative(text, 100.0)

    def test_rejects_bad_duration(self):
        with self.assertRaises(ClipError):
            timespec.resolve_relative("first 10%", 0.0)
        with self.assertRaises(ClipError):
            timespec.resolve_relative("first 10%", -5.0)

    def test_rejects_non_string(self):
        with self.assertRaises(ClipError):
            timespec.resolve_relative(None, 100.0)


class RoundTrip(unittest.TestCase):
    def test_parse_format_round_trip(self):
        for t in [0.0, 5.25, 229.0, 3723.5]:
            with self.subTest(t=t):
                self.assertAlmostEqual(timespec.parse_time(timespec.format_time(t)), t, places=3)


if __name__ == "__main__":
    unittest.main()
