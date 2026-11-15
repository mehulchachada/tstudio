"""Tests for vidclip.gallery.thumbs: cache, frame extraction and ANSI art."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fixture import ensure_sample, ffmpeg_available
from vidclip import ClipError
from vidclip.gallery import thumbs

HAVE_FFMPEG = ffmpeg_available()
HAVE_MAGICK = shutil.which("magick") is not None


def _mean_spread(ppm: bytes) -> float:
    """Mean over pixels of max(r,g,b) - min(r,g,b), from a P6 PPM."""
    _, _, raw = thumbs._parse_p6(ppm)
    total = 0
    for i in range(0, len(raw) - 2, 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        total += max(r, g, b) - min(r, g, b)
    return total / max(1, len(raw) // 3)


def _naive_frame_png(path: Path, at: float, out: Path) -> Path:
    """One frame straight from ffmpeg with no -vf, for the differential test."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-ss", f"{at:g}", "-i", str(path), "-frames:v", "1",
         "-f", "image2", str(out)],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode()[:300]}")
    if not out.is_file() or out.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg produced no output at {out}")
    return out


class CacheKeyTests(unittest.TestCase):
    def test_key_changes_when_bytes_change(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.mp4"
            p.write_bytes(b"one")
            first = thumbs.cache_key(p)
            p.write_bytes(b"one-two")
            second = thumbs.cache_key(p)
            self.assertNotEqual(first, second)

    def test_key_is_stable(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.mp4"
            p.write_bytes(b"one")
            self.assertEqual(thumbs.cache_key(p), thumbs.cache_key(p))

    def test_key_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.mp4"
            p.write_bytes(b"one")
            other = p.with_name(p.name.upper())
            self.assertEqual(thumbs.cache_key(p), thumbs.cache_key(other))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not available")
class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._patcher = mock.patch.object(thumbs, "CACHE_DIR", Path(self._tmp.name))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_dense_thumbnail_is_cached(self):
        src = ensure_sample("dense")
        first = thumbs.thumbnail(src)
        self.assertTrue(first.is_file())
        self.assertGreater(first.stat().st_size, 0)
        mtime = first.stat().st_mtime
        second = thumbs.thumbnail(src)
        self.assertEqual(first, second)
        self.assertEqual(mtime, second.stat().st_mtime)

    def test_force_regenerates(self):
        src = ensure_sample("dense")
        first = thumbs.thumbnail(src)
        mtime = first.stat().st_mtime
        second = thumbs.thumbnail(src, force=True)
        self.assertEqual(first, second)
        self.assertNotEqual(mtime, second.stat().st_mtime)

    def test_image_is_its_own_thumbnail(self):
        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "pic.png"
            img.write_bytes(b"not a real png, but the suffix is what matters")
            self.assertEqual(thumbs.thumbnail(img), img)
            self.assertEqual(list(Path(self._tmp.name).iterdir()), [])

    def test_txt_raises_clip_error(self):
        with tempfile.TemporaryDirectory() as d:
            txt = Path(d) / "notes.txt"
            txt.write_text("hello")
            with self.assertRaises(ClipError):
                thumbs.thumbnail(txt)

    def test_unreadable_video_raises_named_error(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "broken.mp4"
            bad.write_bytes(b"this is not an mp4 at all")
            with self.assertRaises(ClipError) as ctx:
                thumbs.thumbnail(bad)
            self.assertTrue(str(ctx.exception).startswith("could not read a frame from"))

    def _capture_frame_argv(self, src: Path) -> list[str]:
        """Run thumbnail() with the frame-extraction ffmpeg call intercepted.

        ffprobe (probe) still runs for real; only the argv of the ffmpeg
        frame-extraction call is recorded.
        """
        captured: list[list[str]] = []
        real_run = subprocess.run  # the mock patches the shared module; keep the original

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "ffmpeg":
                captured.append(list(cmd))
                # Create the output file so os.replace succeeds.
                out_path = Path(cmd[-1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
                return mock.Mock(returncode=0)
            return real_run(cmd, *args, **kwargs)

        with mock.patch.object(thumbs.subprocess, "run", side_effect=fake_run):
            thumbs.thumbnail(src, force=True)
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_the_timestamp_defaults_to_a_quarter_in(self):
        argv = self._capture_frame_argv(ensure_sample("dense"))
        self.assertIn("-ss", argv)
        self.assertAlmostEqual(float(argv[argv.index("-ss") + 1]), 2.5, delta=0.05)

    def test_sdr_thumbnail_has_no_filter(self):
        argv = self._capture_frame_argv(ensure_sample("dense"))
        self.assertNotIn("-vf", argv)

    def test_hdr_thumbnail_is_tonemapped(self):
        src = ensure_sample("hdr")
        at = 4.0 * 0.25  # the same default thumbnail() uses
        thumb = thumbs.thumbnail(src, force=True)

        def spread(png: Path) -> float:
            proc = subprocess.run(
                ["magick", str(png), "-resize", "64x32>",
                 "-background", "black", "-flatten", "ppm:-"],
                capture_output=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"magick failed on {png}: {proc.stderr.decode()[:200]}")
            return _mean_spread(proc.stdout)

        with tempfile.TemporaryDirectory() as d:
            naive = _naive_frame_png(src, at, Path(d) / "naive.png")
            thumb_spread = spread(thumb)
            naive_spread = spread(naive)
        print(f"\n  mean spread: thumbnail={thumb_spread:.2f} naive={naive_spread:.2f}")
        self.assertGreater(thumb_spread, naive_spread)


@unittest.skipUnless(HAVE_FFMPEG and HAVE_MAGICK, "ffmpeg or magick not available")
class ArtTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._patcher = mock.patch.object(thumbs, "CACHE_DIR", Path(self._tmp.name))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_art_fits_within_the_requested_box(self):
        src = ensure_sample("dense")
        art = thumbs.art_for(src, 40, 12)
        self.assertGreaterEqual(len(art), 1)
        got_cols, got_rows = thumbs.art_size(art)
        self.assertGreaterEqual(got_cols, 1)
        self.assertGreaterEqual(got_rows, 1)
        self.assertLessEqual(got_cols, 40)
        self.assertLessEqual(got_rows, 12)
        # -resize fits the box and touches one side; allow 1 of slack on the
        # row count for the odd-height rounding in render_art.
        self.assertTrue(got_cols == 40 or got_rows >= 11)
        for line in art:
            self.assertTrue(line.endswith("\x1b[0m"))

    def test_zero_dimensions_give_nothing(self):
        src = ensure_sample("dense")
        self.assertEqual(thumbs.render_art(src, 0, 12), [])
        self.assertEqual(thumbs.render_art(src, 40, 0), [])

    def test_art_has_many_colours(self):
        src = ensure_sample("dense")
        art = thumbs.art_for(src, 40, 12)
        fg = set()
        for line in art:
            for part in line.split("\x1b[38;2;"):
                if part:
                    fg.add(part.split("m", 1)[0])
        self.assertGreater(len(fg), 20)

    def test_art_has_no_blank_padding_row(self):
        # 60x30 is not 16:9, so filling the box would need padding rows.
        src = ensure_sample("dense")
        art = thumbs.art_for(src, 60, 30)
        self.assertGreaterEqual(len(art), 1)
        row_sets = []
        for line in art:
            fg = set()
            for part in line.split("\x1b[38;2;"):
                if part:
                    fg.add(part.split("m", 1)[0])
            row_sets.append(fg)
        self.assertTrue(any(len(s) > 1 for s in row_sets))
        for fg in row_sets:
            self.assertNotEqual(fg, {"255;255;255"})

    def test_render_art_argv_has_no_extent(self):
        src = ensure_sample("dense")
        captured: list[list[str]] = []
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "magick":
                captured.append(list(cmd))
            return real_run(cmd, *args, **kwargs)

        with mock.patch.object(thumbs.subprocess, "run", side_effect=fake_run):
            thumbs.art_for(src, 60, 30)
        self.assertEqual(len(captured), 1)
        self.assertNotIn("-extent", captured[0])


class PruneTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._patcher = mock.patch.object(thumbs, "CACHE_DIR", Path(self._tmp.name))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_prune_keeps_newest(self):
        import os
        import time
        for i in range(5):
            p = Path(self._tmp.name) / f"thumb{i}.png"
            p.write_bytes(b"x")
            old = time.time() - (100 - i)
            os.utime(p, (old, old))
        deleted = thumbs.prune_cache(limit=3)
        self.assertEqual(deleted, 2)
        remaining = sorted(p.name for p in Path(self._tmp.name).iterdir())
        self.assertEqual(remaining, ["thumb2.png", "thumb3.png", "thumb4.png"])

    def test_prune_missing_directory(self):
        missing = Path(self._tmp.name) / "does-not-exist"
        with mock.patch.object(thumbs, "CACHE_DIR", missing):
            self.assertEqual(thumbs.prune_cache(), 0)


if __name__ == "__main__":
    unittest.main()
