"""Tests for tstudio.gallery.branding: the logo art, palette and lockups."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import tstudio.gallery.branding as branding
from tstudio import ClipError
from tstudio.gallery.branding import (
    AMBER,
    CAMERA,
    COMPACT,
    CURSOR,
    GREY,
    GREEN,
    RED,
    SUBTITLE,
    TAGLINE,
    VIOLET,
    WORDMARK,
    header_lines,
    lens_colour,
    splash_lines,
)


class TestCameraArt(unittest.TestCase):
    def test_camera_is_three_rows_of_five(self):
        self.assertEqual(len(CAMERA), 3)
        for row in CAMERA:
            self.assertEqual(len(row), 5)

    def test_top_row_has_shutter_button(self):
        self.assertIn("▪", CAMERA[0])


class TestLensColour(unittest.TestCase):
    def test_all_four_states(self):
        self.assertEqual(lens_colour("idle"), AMBER)
        self.assertEqual(lens_colour("working"), GREEN)
        self.assertEqual(lens_colour("hdr"), VIOLET)
        self.assertEqual(lens_colour("error"), RED)

    def test_unknown_state_raises_clip_error(self):
        with self.assertRaises(ClipError):
            lens_colour("spinning")


class TestWordmark(unittest.TestCase):
    def test_wordmark_ends_with_the_cursor(self):
        self.assertEqual(CURSOR, "▌")
        self.assertTrue(WORDMARK.endswith(CURSOR))

    def test_cursor_is_defined_once(self):
        source = Path(branding.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("▌"), 1)

    def test_compact_has_no_cursor(self):
        self.assertNotIn(CURSOR, COMPACT)


class TestHeaderLines(unittest.TestCase):
    def test_wide(self):
        lines = header_lines(100)
        self.assertEqual(len(lines), 3)
        self.assertIn(WORDMARK, lines[1])
        self.assertIn(SUBTITLE, lines[2])
        for line in lines:
            self.assertLessEqual(len(line), 100)

    def test_narrow(self):
        self.assertEqual(header_lines(60), ["◉ tstudio"])

    def test_header_uses_the_subtitle(self):
        line = header_lines(100)[2]
        self.assertIn("terminal studio", line)
        self.assertNotIn("browse", line)


class TestSplashLines(unittest.TestCase):
    def test_wide(self):
        lines = splash_lines(100)
        for expected in (WORDMARK, TAGLINE, *CAMERA):
            self.assertTrue(any(expected in line for line in lines))
        for line in lines:
            self.assertLessEqual(len(line), 100)
        wordmark_line = next(line for line in lines if WORDMARK in line)
        leading = len(wordmark_line) - len(wordmark_line.lstrip(" "))
        trailing = len(wordmark_line) - len(wordmark_line.rstrip(" "))
        self.assertLessEqual(abs(leading - trailing), 1)

    def test_narrow(self):
        lines = splash_lines(60)
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("◉ tstudio" in line for line in lines))

    def test_splash_uses_the_tagline(self):
        lines = splash_lines(100)
        self.assertTrue(any("browse · cut · send" in line for line in lines))
        self.assertTrue(any(WORDMARK in line for line in lines))
        self.assertTrue(WORDMARK.endswith(CURSOR))


class TestNoAnsi(unittest.TestCase):
    def test_no_ansi_escapes_anywhere(self):
        for lines in (header_lines(100), header_lines(60),
                      splash_lines(100), splash_lines(60)):
            for line in lines:
                self.assertNotIn("\x1b", line)


class TestPalette(unittest.TestCase):
    def test_all_colours_are_hex(self):
        for colour in (AMBER, GREEN, VIOLET, RED, GREY):
            self.assertRegex(colour, r"^#[0-9a-f]{6}$")


if __name__ == "__main__":
    unittest.main()
