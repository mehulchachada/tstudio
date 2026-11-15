"""Command-shape and real-conversion tests for lightbox.presets."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fixture import ffmpeg_available, ensure_sample, probe_duration

from lightbox import ClipError
from lightbox.convert import run_convert, tonemap_filter
from lightbox.media import MediaInfo, VideoStream, is_hdr, probe
from lightbox.presets import (
    PRESETS,
    Preset,
    duration_warning,
    get_preset,
    plan_social,
)


def _hdr_info(path: Path) -> MediaInfo:
    """A 4K HDR10 source with the owner's real ShadowPlay numbers."""
    return MediaInfo(
        path=path,
        duration=10.0,
        size=10_000_000,
        bit_rate=105_000_000,
        video=VideoStream(
            index=0,
            codec="hevc",
            width=3840,
            height=2160,
            fps=120.0,
            pix_fmt="yuv420p10le",
            bit_rate=105_000_000,
            color_primaries="bt2020",
            color_transfer="smpte2084",
            color_space="bt2020nc",
            color_range="pc",
        ),
        audio=(),
        stream_count=1,
    )


def _sdr_info(path: Path) -> MediaInfo:
    return MediaInfo(
        path=path,
        duration=10.0,
        size=1_000_000,
        bit_rate=8_000_000,
        video=VideoStream(
            index=0,
            codec="h264",
            width=1920,
            height=1080,
            fps=30.0,
            pix_fmt="yuv420p",
            bit_rate=8_000_000,
            color_primaries="bt709",
            color_transfer="bt709",
            color_space="bt709",
            color_range="tv",
        ),
        audio=(),
        stream_count=1,
    )


def _square_info(path: Path) -> MediaInfo:
    return MediaInfo(
        path=path,
        duration=10.0,
        size=1_000_000,
        bit_rate=8_000_000,
        video=VideoStream(
            index=0,
            codec="h264",
            width=1080,
            height=1080,
            fps=30.0,
            pix_fmt="yuv420p",
            bit_rate=8_000_000,
            color_primaries="bt709",
            color_transfer="bt709",
            color_space="bt709",
            color_range="tv",
        ),
        audio=(),
        stream_count=1,
    )


def _plan(info: MediaInfo, preset, **kwargs) -> "ConvertPlan":
    with mock.patch("lightbox.presets.encoders_available",
                    return_value=frozenset({"h264_nvenc", "libx264"})):
        return plan_social(info, Path("out.mp4"), preset, **kwargs)


def _chain(plan) -> str:
    return plan.command[plan.command.index("-vf") + 1]


# Command-shape tests: no ffmpeg needed.
class PlanShapeTests(unittest.TestCase):
    def test_no_preset_ever_emits_hdr(self):
        info = _hdr_info(Path("in.mp4"))
        for name, preset in PRESETS.items():
            plan = _plan(info, preset)
            self.assertEqual((plan.width, plan.height),
                             (preset.width, preset.height), name)
            command = " ".join(plan.command)
            self.assertIn("yuv420p", command, name)
            self.assertIn("bt709", command, name)
            self.assertNotIn("smpte2084", command, name)

    def test_pad_chain(self):
        plan = _plan(_hdr_info(Path("in.mp4")), "shorts", fit="pad")
        chain = _chain(plan)
        self.assertIn("gblur", chain)
        self.assertIn("overlay", chain)
        self.assertIn("split", chain)

    def test_crop_chain(self):
        plan = _plan(_hdr_info(Path("in.mp4")), "shorts", fit="crop")
        chain = _chain(plan)
        self.assertIn("crop=1080:1920", chain)
        self.assertNotIn("gblur", chain)

    def test_bad_fit_raises(self):
        with self.assertRaises(ClipError):
            _plan(_hdr_info(Path("in.mp4")), "shorts", fit="sideways")

    def test_matching_aspect_ratio_skips_the_blur(self):
        plan = _plan(_sdr_info(Path("in.mp4")), "x")
        chain = _chain(plan)
        self.assertNotIn("gblur", chain)
        self.assertNotIn("overlay", chain)
        self.assertNotIn("split", chain)
        self.assertIn("scale=1920:1080", chain)

    def test_sdr_source_has_no_tonemap(self):
        plan = _plan(_sdr_info(Path("in.mp4")), "shorts")
        chain = _chain(plan)
        self.assertNotIn("libplacebo", chain)
        self.assertNotIn("zscale", chain)
        self.assertNotIn("tonemap", chain)

    def test_hdr_source_tonemaps_first_and_unsized(self):
        plan = _plan(_hdr_info(Path("in.mp4")), "shorts")
        chain = _chain(plan)
        first = chain.split(",")[0]
        self.assertIn("libplacebo", first)
        self.assertNotIn("w=", first)
        self.assertNotIn("h=", first)

    def test_fps_is_capped_for_social(self):
        plan = _plan(_hdr_info(Path("in.mp4")), "shorts")
        self.assertIn("-r", plan.command)
        self.assertEqual(plan.command[plan.command.index("-r") + 1], "60")

        plan = _plan(_sdr_info(Path("in.mp4")), "shorts")
        self.assertNotIn("-r", plan.command)

    def test_aliases(self):
        self.assertIs(get_preset("youtube-shorts"), PRESETS["shorts"])
        self.assertIs(get_preset("yt-shorts"), PRESETS["shorts"])
        self.assertIs(get_preset("instagram"), PRESETS["reels"])
        self.assertIs(get_preset("reels"), PRESETS["reels"])
        self.assertIs(get_preset("twitter"), PRESETS["x"])

        with self.assertRaises(ClipError) as ctx:
            get_preset("vimeo")
        for name in PRESETS:
            self.assertIn(name, str(ctx.exception))

    def test_duration_warning(self):
        warning = duration_warning(PRESETS["shorts"], 200)
        self.assertIsNotNone(warning)
        self.assertIn("180", warning)
        self.assertIsNone(duration_warning(PRESETS["shorts"], 30))

        unlimited = Preset("none", "No limit", 1080, 1920, None, None,
                           "192k", "no limit")
        self.assertIsNone(duration_warning(unlimited, 10_000))

    def test_social_range_validation_is_shared(self):
        with self.assertRaises(ClipError) as ctx:
            _plan(_hdr_info(Path("in.mp4")), "shorts", start=5.0, end=3.0)
        message = str(ctx.exception)
        self.assertIn("0:05.000", message)
        self.assertIn("0:03.000", message)

    def test_social_range_command_shape(self):
        plan = _plan(_hdr_info(Path("in.mp4")), "shorts", start=1.0, end=3.0)
        command = plan.command
        self.assertIn("-ss", command)
        self.assertEqual(command[command.index("-ss") + 1], "1.000")
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertIn("-t", command)
        self.assertEqual(command[command.index("-t") + 1], "2.000")
        self.assertEqual(plan.start, 1.0)
        self.assertEqual(plan.end, 3.0)

    def test_no_avoid_negative_ts_and_faststart(self):
        for info in (_hdr_info(Path("in.mp4")), _sdr_info(Path("in.mp4"))):
            for name in PRESETS:
                plan = _plan(info, name)
                self.assertNotIn("-avoid_negative_ts", plan.command, name)
                self.assertIn("+faststart", plan.command, name)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class RealSocialTests(unittest.TestCase):
    def _social(self, src: Path, dst: Path, preset, **kwargs) -> None:
        info = probe(src)
        plan = plan_social(info, dst, preset, **kwargs)
        run_convert(plan)

    def test_dense_to_shorts_pad(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("dense")
            dst = Path(tmp) / "out.mp4"
            self._social(src, dst, "shorts", fit="pad",
                         encoder="libx264", quality=35)
            self.assertTrue(dst.exists())
            out = probe(dst)
            self.assertEqual((out.video.width, out.video.height), (1080, 1920))
            self.assertAlmostEqual(probe_duration(dst), 10.0, delta=0.3)
            self.assertTrue(out.audio)

    def test_dense_to_shorts_crop(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("dense")
            dst = Path(tmp) / "out.mp4"
            self._social(src, dst, "shorts", fit="crop",
                         encoder="libx264", quality=35)
            out = probe(dst)
            self.assertEqual((out.video.width, out.video.height), (1080, 1920))

    def test_dense_to_x(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("dense")
            dst = Path(tmp) / "out.mp4"
            self._social(src, dst, "x", encoder="libx264", quality=35)
            out = probe(dst)
            self.assertEqual((out.video.width, out.video.height), (1920, 1080))

    def test_silent_to_tiktok(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("silent")
            dst = Path(tmp) / "out.mp4"
            self._social(src, dst, "tiktok", encoder="libx264", quality=35)
            self.assertTrue(dst.exists())

    def test_path_with_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "my clips"
            folder.mkdir()
            src = folder / "my clip.mp4"
            shutil.copyfile(ensure_sample("dense"), src)
            dst = folder / "out.mp4"
            self._social(src, dst, "shorts", encoder="libx264", quality=35)
            self.assertTrue(dst.exists())
            self.assertEqual(probe(dst).video.height, 1920)

    def test_hdr_source_really_exports_to_shorts(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("hdr")
            dst = Path(tmp) / "out.mp4"
            info = probe(src)
            plan = plan_social(info, dst, "shorts", fit="pad",
                               encoder="libx264", quality=35)
            self.assertTrue(plan.tonemapped)
            run_convert(plan)
            self.assertTrue(dst.exists())
            out = probe(dst)
            self.assertEqual((out.video.width, out.video.height), (1080, 1920))
            self.assertNotEqual(out.video.color_transfer, "smpte2084")
            self.assertIn(out.video.color_transfer, ("bt709", None))
            self.assertEqual(out.video.pix_fmt, "yuv420p")

    def test_hdr_source_really_exports_to_x_cropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("hdr")
            dst = Path(tmp) / "out.mp4"
            info = probe(src)
            plan = plan_social(info, dst, "x", fit="crop",
                               encoder="libx264", quality=35)
            self.assertTrue(plan.tonemapped)
            run_convert(plan)
            self.assertTrue(dst.exists())
            out = probe(dst)
            self.assertEqual((out.video.width, out.video.height), (1920, 1080))
            self.assertNotEqual(out.video.color_transfer, "smpte2084")
            self.assertEqual(out.video.pix_fmt, "yuv420p")

    def test_social_export_of_a_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("hdr")
            dst = Path(tmp) / "out.mp4"
            info = probe(src)
            plan = plan_social(info, dst, "shorts", fit="crop",
                               encoder="libx264", quality=35,
                               start=1.0, end=3.0)
            self.assertTrue(plan.tonemapped)
            run_convert(plan)
            self.assertTrue(dst.exists())
            out = probe(dst)
            self.assertEqual((out.video.width, out.video.height), (1080, 1920))
            self.assertAlmostEqual(probe_duration(dst), 2.0, delta=0.3)
            self.assertEqual(out.video.pix_fmt, "yuv420p")
            self.assertNotEqual(out.video.color_transfer, "smpte2084")

    def test_unsized_tonemap_filter_parses(self):
        src = ensure_sample("hdr")
        info = probe(src)
        self.assertTrue(is_hdr(info.video))
        chain = tonemap_filter(info)
        self.assertIsNotNone(chain)
        name, sep, rest = chain.partition("=")
        self.assertIn(name, ("libplacebo", "zscale"))
        self.assertNotEqual(sep, "")
        self.assertNotEqual(chain[len(name)], ":")
        self.assertTrue(rest)


if __name__ == "__main__":
    unittest.main()
