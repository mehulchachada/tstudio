"""Tests for vidclip.media against the real fixture files."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import fixture
from vidclip import ClipError
from vidclip.media import (
    VideoStream,
    encoders_available,
    filters_available,
    is_hdr,
    keyframe_at_or_before,
    keyframes_in,
    probe,
)


@unittest.skipUnless(fixture.ffmpeg_available(), "ffmpeg not on PATH")
class ProbeTests(unittest.TestCase):
    def test_dense_probe(self) -> None:
        path = fixture.ensure_sample("dense")
        info = probe(path)
        self.assertAlmostEqual(info.duration, 10.0, delta=0.05)
        self.assertIsNotNone(info.video)
        self.assertEqual(info.video.codec, "h264")
        self.assertEqual(info.video.width, 640)
        self.assertEqual(info.video.height, 360)
        self.assertAlmostEqual(info.video.fps, 30.0, delta=0.01)
        self.assertEqual(len(info.audio), 1)
        self.assertEqual(info.audio[0].codec, "aac")
        self.assertEqual(info.size, path.stat().st_size)

    def test_silent_probe_has_no_audio(self) -> None:
        path = fixture.ensure_sample("silent")
        info = probe(path)
        self.assertEqual(info.audio, ())
        self.assertIsNotNone(info.video)

    def test_missing_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.mp4"
            with self.assertRaises(ClipError):
                probe(missing)

    def test_directory_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ClipError):
                probe(Path(tmp))


@unittest.skipUnless(fixture.ffmpeg_available(), "ffmpeg not on PATH")
class KeyframeTests(unittest.TestCase):
    def test_keyframes_in_dense(self) -> None:
        path = fixture.ensure_sample("dense")
        times = keyframes_in(path, 0, 10)
        expected = [round(0.5 * i, 3) for i in range(20)]
        self.assertEqual(len(times), 20)
        for got, want in zip(times, expected):
            self.assertAlmostEqual(got, want, delta=0.01)

    def test_keyframes_in_sparse(self) -> None:
        path = fixture.ensure_sample("sparse")
        times = keyframes_in(path, 0, 10)
        for got, want in zip(times, [0.0, 2.0, 4.0, 6.0, 8.0]):
            self.assertAlmostEqual(got, want, delta=0.01)
        self.assertEqual(len(times), 5)

    def test_a_keyframe_before_the_window_is_not_returned(self) -> None:
        sparse = fixture.ensure_sample("sparse")
        self.assertEqual(keyframes_in(sparse, 1.0, 2.0), [])
        dense = fixture.ensure_sample("dense")
        self.assertEqual(len(keyframes_in(dense, 0.0, 10.0)), 20)
        self.assertEqual(len(keyframes_in(sparse, 0.0, 10.0)), 5)

    def test_snap_sparse(self) -> None:
        path = fixture.ensure_sample("sparse")
        self.assertAlmostEqual(keyframe_at_or_before(path, 3.0), 2.0, delta=0.01)
        self.assertAlmostEqual(keyframe_at_or_before(path, 1.9), 0.0, delta=0.01)
        self.assertAlmostEqual(keyframe_at_or_before(path, 8.5), 8.0, delta=0.01)

    def test_sparse_needs_a_wider_window_than_the_first_rung(self) -> None:
        sparse = fixture.ensure_sample("sparse")
        self.assertAlmostEqual(keyframe_at_or_before(sparse, 1.9), 0.0, delta=0.01)

    def test_a_time_already_on_a_keyframe_does_not_move(self) -> None:
        sparse = fixture.ensure_sample("sparse")
        self.assertAlmostEqual(keyframe_at_or_before(sparse, 4.0), 4.0, delta=0.01)
        dense = fixture.ensure_sample("dense")
        self.assertAlmostEqual(keyframe_at_or_before(dense, 3.0), 3.0, delta=0.01)

    def test_snap_dense(self) -> None:
        path = fixture.ensure_sample("dense")
        self.assertAlmostEqual(keyframe_at_or_before(path, 3.1), 3.0, delta=0.01)
        self.assertAlmostEqual(keyframe_at_or_before(path, 0.2), 0.0, delta=0.01)
        self.assertAlmostEqual(keyframe_at_or_before(path, 0.0), 0.0, delta=0.01)
        self.assertAlmostEqual(keyframe_at_or_before(path, -5), 0.0, delta=0.01)


@unittest.skipUnless(fixture.ffmpeg_available(), "ffmpeg not on PATH")
class EncoderTests(unittest.TestCase):
    def test_encoders_available(self) -> None:
        names = encoders_available()
        self.assertIn("libx264", names)
        self.assertIn("aac", names)


@unittest.skipUnless(fixture.ffmpeg_available(), "ffmpeg not on PATH")
class ColorProbeTests(unittest.TestCase):
    def test_probe_exposes_color_attributes(self) -> None:
        info = probe(fixture.ensure_sample("dense"))
        self.assertIsNotNone(info.video)
        for name in ("color_primaries", "color_transfer", "color_space", "color_range"):
            getattr(info.video, name)

    def test_fixture_is_not_hdr(self) -> None:
        info = probe(fixture.ensure_sample("dense"))
        self.assertFalse(is_hdr(info.video))


class IsHdrTests(unittest.TestCase):
    def _stream(self, **kwargs) -> VideoStream:
        base = dict(index=0, codec="hevc", width=3840, height=2160, fps=120.0,
                    pix_fmt="yuv420p10le", bit_rate=105_000_000)
        base.update(kwargs)
        return VideoStream(**base)

    def test_pq_is_hdr(self) -> None:
        self.assertTrue(is_hdr(self._stream(color_transfer="smpte2084")))

    def test_hlg_is_hdr(self) -> None:
        self.assertTrue(is_hdr(self._stream(color_transfer="arib-std-b67")))

    def test_case_and_whitespace_insensitive(self) -> None:
        self.assertTrue(is_hdr(self._stream(color_transfer="SMPTE2084")))
        self.assertTrue(is_hdr(self._stream(color_transfer=" smpte2084 ")))

    def test_sdr_transfer_is_not_hdr(self) -> None:
        self.assertFalse(is_hdr(self._stream(color_transfer="bt709")))
        self.assertFalse(is_hdr(self._stream(color_transfer=None)))
        self.assertFalse(is_hdr(self._stream(color_transfer="")))

    def test_none_stream_is_not_hdr(self) -> None:
        self.assertFalse(is_hdr(None))

    def test_wide_gamut_alone_is_not_hdr(self) -> None:
        stream = self._stream(color_primaries="bt2020", color_space="bt2020nc",
                              color_transfer="bt709", pix_fmt="yuv420p10le")
        self.assertFalse(is_hdr(stream))

    def test_the_real_capture_shape_is_hdr(self) -> None:
        stream = self._stream(color_primaries="bt2020", color_transfer="smpte2084",
                              color_space="bt2020nc", color_range="pc")
        self.assertTrue(is_hdr(stream))


@unittest.skipUnless(fixture.ffmpeg_available(), "ffmpeg not on PATH")
class FilterTests(unittest.TestCase):
    def test_filters_available(self) -> None:
        names = filters_available()
        self.assertIn("scale", names)
        self.assertTrue({"zscale", "libplacebo"} & names)

    def test_filters_available_is_cached(self) -> None:
        self.assertIs(filters_available(), filters_available())


if __name__ == "__main__":
    unittest.main()
