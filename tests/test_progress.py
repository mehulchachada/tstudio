"""Tests for tstudio.gallery.progress: pure progress display logic."""

import unittest

from tstudio import ClipError
from tstudio.gallery.progress import (
    LINES,
    PULSE,
    SPINNER,
    bar,
    eta,
    frame_at,
    format_eta,
    line_at,
    render,
)


class TestFrames(unittest.TestCase):
    def test_frames_do_not_change_width(self):
        self.assertEqual(len(set(len(f) for f in SPINNER)), 1)
        self.assertEqual(len(set(len(f) for f in PULSE)), 1)


class TestLines(unittest.TestCase):
    def test_lines_are_short_dry_and_unique(self):
        self.assertGreaterEqual(len(LINES), 12)
        self.assertEqual(len(set(LINES)), len(LINES))
        for line in LINES:
            self.assertLessEqual(len(line), 38)
            self.assertEqual(line, line.lower())
            self.assertNotIn("!", line)
            for ch in line:
                self.assertLessEqual(ord(ch), 0x2FFF)


class TestFrameAt(unittest.TestCase):
    def test_cycles_and_wraps(self):
        self.assertEqual(frame_at(0, SPINNER), SPINNER[0])
        self.assertEqual(frame_at(7, SPINNER), SPINNER[7])
        self.assertEqual(frame_at(8, SPINNER), SPINNER[0])
        self.assertEqual(frame_at(100, SPINNER), SPINNER[100 % 8])


class TestLineAt(unittest.TestCase):
    def test_changes_every_ticks(self):
        self.assertEqual(line_at(0, every=20), line_at(19, every=20))
        self.assertNotEqual(line_at(19, every=20), line_at(20, every=20))

    def test_every_below_one_raises(self):
        with self.assertRaises(ClipError):
            line_at(0, every=0)


class TestBar(unittest.TestCase):
    def test_exact_strings(self):
        self.assertEqual(bar(0.0, 10), " " * 10)
        self.assertEqual(bar(0.5, 10), "█" * 5 + " " * 5)
        self.assertEqual(bar(1.0, 10), "█" * 10)
        for value in (bar(0.0, 10), bar(0.5, 10), bar(1.0, 10)):
            self.assertEqual(len(value), 10)

    def test_clamps(self):
        self.assertEqual(bar(-5, 10), " " * 10)
        self.assertEqual(bar(99, 10), "█" * 10)

    def test_nan_does_not_raise(self):
        self.assertEqual(bar(float("nan"), 10), " " * 10)

    def test_a_partial_cell_is_drawn(self):
        self.assertIn("▋", bar(0.55, 10))

    def test_every_fraction_at_every_width_draws(self):
        """This crash was live, not theoretical, and sampling missed it.

        bar() indexed a seven-glyph string with eighths, which reach 7, so
        any cell seven eighths full or more raised IndexError - one progress
        value in eight. The tests above sample 0.0, 0.5, 0.55 and 1.0, and
        every one of those is outside the bad band. Hence a sweep.
        """
        bad: list[str] = []
        for width in (1, 2, 3, 7, 10, 24, 64, 214):
            for step in range(1001):
                fraction = step / 1000
                try:
                    value = bar(fraction, width)
                except Exception as exc:  # noqa: BLE001 - any raise is the bug
                    bad.append(f"width {width} at {fraction}: {exc!r}")
                    continue
                if len(value) != width:
                    bad.append(
                        f"width {width} at {fraction}: {len(value)} cells")
        self.assertEqual(bad[:5], [], f"{len(bad)} bad (width, fraction) pairs")

    def test_a_nearly_full_cell_uses_the_last_partial_glyph(self):
        """0.09 of ten cells is nine tenths of one cell - the case that crashed."""
        self.assertEqual(bar(0.09, 10), "▉" + " " * 9)

    def test_the_bar_is_only_full_when_the_job_is(self):
        """Never claim finished before finished.

        This is why a nearly-full cell clamps to seven eighths instead of
        carrying into the whole-cell run, which would read as done at 99%.
        """
        for step in range(1000):          # 0.000 .. 0.999
            fraction = step / 1000
            self.assertNotEqual(bar(fraction, 10), "█" * 10,
                                f"bar reads full at {fraction}")
        self.assertEqual(bar(1.0, 10), "█" * 10)


class TestEta(unittest.TestCase):
    def test_half_done(self):
        self.assertAlmostEqual(eta(0.5, 10.0), 10.0)

    def test_none_cases(self):
        self.assertIsNone(eta(0.0, 10.0))
        self.assertIsNone(eta(1.0, 10.0))
        self.assertIsNone(eta(0.5, 0.0))


class TestFormatEta(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(format_eta(None), "--:--")
        self.assertEqual(format_eta(95), "1:35")
        self.assertEqual(format_eta(3725), "1:02:05")


class TestRender(unittest.TestCase):
    def test_two_lines_exact_width(self):
        for width in (24, 40, 80):
            for fraction in (0.0, 0.37, 1.0):
                lines = render(fraction, 12.0, 3, width)
                self.assertEqual(len(lines), 2)
                for line in lines:
                    self.assertEqual(len(line), width)

    def test_render_survives_every_fraction(self):
        """render() is what the editor's timer calls eight times a second.

        The traceback that started this came through here at width 214 and
        fraction 0.0877, so the sweep covers the caller too, not only `bar`.
        """
        bad: list[str] = []
        for width in (24, 40, 80, 214):
            for step in range(501):
                fraction = step / 500
                try:
                    lines = render(fraction, 164.25, 1314, width)
                except Exception as exc:  # noqa: BLE001 - any raise is the bug
                    bad.append(f"width {width} at {fraction}: {exc!r}")
                    continue
                if [len(line) for line in lines] != [width, width]:
                    bad.append(f"width {width} at {fraction}: ragged")
        self.assertEqual(bad[:5], [], f"{len(bad)} bad (width, fraction) pairs")

    def test_label_wins(self):
        lines = render(0.37, 12.0, 3, 40, label="writing out.mp4")
        self.assertIn("writing out.mp4", lines[1])
        for line in LINES:
            self.assertNotIn(line, lines[1])

    def test_width_too_small_raises(self):
        with self.assertRaises(ClipError):
            render(0.37, 12.0, 3, 20)

    def test_the_numbers_do_not_move(self):
        lines = [render(0.37, 12.0, tick, 40)[0] for tick in (0, 1, 2)]
        offsets = [line.index("37%") for line in lines]
        self.assertEqual(len(set(offsets)), 1)


if __name__ == "__main__":
    unittest.main()
