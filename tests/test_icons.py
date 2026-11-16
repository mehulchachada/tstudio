"""The preview pane's block art: pure, so these are fast and need no terminal."""

from __future__ import annotations

import unittest

from rich.cells import cell_len

from tstudio import ClipError
from tstudio.gallery import icons


class TestArt(unittest.TestCase):
    def test_a_video_gets_the_film_strip(self) -> None:
        self.assertEqual(icons.art_for_kind("video"), list(icons.FILM))

    def test_an_image_gets_the_picture_frame(self) -> None:
        self.assertEqual(icons.art_for_kind("image"), list(icons.PICTURE))

    def test_an_unknown_kind_gets_a_frame_rather_than_an_exception(self) -> None:
        """The listing grows kinds before this module hears about them."""
        self.assertEqual(icons.art_for_kind("audio"), list(icons.UNKNOWN))
        self.assertEqual(icons.art_for_kind(""), list(icons.UNKNOWN))

    def test_every_row_of_every_art_is_the_same_cell_width(self) -> None:
        """A ragged row would make the frame lean; a wide glyph would too."""
        for name, art in (("FILM", icons.FILM),
                          ("PICTURE", icons.PICTURE),
                          ("UNKNOWN", icons.UNKNOWN)):
            with self.subTest(art=name):
                widths = {cell_len(row) for row in art}
                self.assertEqual(
                    len(widths), 1,
                    f"{name} rows measure {sorted(widths)} cells")
                # len() and cell_len() agreeing means no double-width glyph
                # slipped in, which is what would shear the right-hand edge.
                self.assertEqual(widths.pop(), len(art[0]))

    def test_width_rejects_ragged_art(self) -> None:
        with self.assertRaises(ClipError):
            icons.width(["---", "----"])

    def test_width_rejects_empty_art(self) -> None:
        with self.assertRaises(ClipError):
            icons.width([])


class TestActions(unittest.TestCase):
    def test_a_video_offers_mpv(self) -> None:
        self.assertIn("p", icons.action_for_kind("video"))
        self.assertIn("mpv", icons.action_for_kind("video"))

    def test_an_image_offers_opening(self) -> None:
        self.assertIn("p", icons.action_for_kind("image"))

    def test_a_kind_with_nothing_to_do_says_nothing(self) -> None:
        self.assertEqual(icons.action_for_kind("folder"), "")


if __name__ == "__main__":
    unittest.main()
