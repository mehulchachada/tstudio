"""Command-shape and real-conversion tests for lightbox.convert."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fixture import ffmpeg_available, ensure_sample, probe_duration, probe_video_codec

from lightbox import ClipError
from lightbox.convert import TARGETS, plan_convert, run_convert
from lightbox.media import MediaInfo, VideoStream, encoders_available, probe


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
            width=640,
            height=360,
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


# Command-shape tests: no ffmpeg needed. _pick_encoder is stubbed so the
# encoder-availability probe never runs.
class PlanShapeTests(unittest.TestCase):
    def _plan(self, info, **kwargs):
        with mock.patch("lightbox.convert._pick_encoder", return_value="hevc_nvenc"):
            return plan_convert(info, Path("out.mp4"), **kwargs)

    def test_hdr_auto_keeps_hdr(self):
        plan = self._plan(_hdr_info(Path("in.mp4")), hdr="auto")
        self.assertTrue(plan.hdr_out)
        self.assertFalse(plan.tonemapped)
        self.assertIsNone(plan.tonemapper)
        command = " ".join(plan.command)
        self.assertIn("p010le", command)
        self.assertIn("smpte2084", command)
        self.assertIn("main10", command)
        self.assertNotIn("libplacebo", command)
        self.assertNotIn("tonemap", command)

    def test_tonemapped_output_is_not_tagged_hdr(self):
        with mock.patch(
            "lightbox.convert.filters_available",
            return_value=frozenset({"libplacebo", "zscale"}),
        ):
            plan = self._plan(_hdr_info(Path("in.mp4")), hdr="sdr")
        self.assertTrue(plan.tonemapped)
        self.assertEqual(plan.tonemapper, "libplacebo")
        command = " ".join(plan.command)
        self.assertIn("yuv420p", command)
        self.assertIn("bt709", command)
        self.assertNotIn("smpte2084", command)

    def test_zscale_fallback(self):
        with mock.patch(
            "lightbox.convert.filters_available",
            return_value=frozenset({"zscale"}),
        ):
            plan = self._plan(_hdr_info(Path("in.mp4")), hdr="sdr")
        self.assertEqual(plan.tonemapper, "zscale")
        command = " ".join(plan.command)
        self.assertIn("tonemap=hable", command)
        self.assertIn("zscale", command)
        self.assertNotIn("smpte2084", command)

    def test_libplacebo_does_its_own_scaling(self):
        with mock.patch(
            "lightbox.convert.filters_available",
            return_value=frozenset({"libplacebo"}),
        ):
            plan = self._plan(_hdr_info(Path("in.mp4")), hdr="sdr")
        self.assertEqual(plan.tonemapper, "libplacebo")
        self.assertFalse(any(arg.startswith("scale=") for arg in plan.command))

    def test_sdr_keep_raises(self):
        with self.assertRaises(ClipError):
            self._plan(_sdr_info(Path("in.mp4")), hdr="keep")

    def test_sdr_auto_is_plain_scale(self):
        plan = self._plan(_sdr_info(Path("in.mp4")), target="360p", hdr="auto")
        self.assertFalse(plan.hdr_out)
        self.assertFalse(plan.tonemapped)
        command = " ".join(plan.command)
        self.assertNotIn("tonemap", command)
        self.assertNotIn("libplacebo", command)
        self.assertNotIn("zscale", command)

    def test_hdr_out_with_h264_raises(self):
        with mock.patch("lightbox.convert._pick_encoder", return_value="libx264"):
            with self.assertRaises(ClipError):
                plan_convert(
                    _hdr_info(Path("in.mp4")), Path("out.mp4"),
                    hdr="auto", encoder="libx264",
                )

    def test_bad_target_and_upscale(self):
        with self.assertRaises(ClipError) as ctx:
            plan_convert(_hdr_info(Path("in.mp4")), Path("out.mp4"), target="4320p")
        for key in TARGETS:
            self.assertIn(key, str(ctx.exception))

        with self.assertRaises(ClipError) as ctx:
            plan_convert(_sdr_info(Path("in.mp4")), Path("out.mp4"), target="1080p")
        self.assertIn("360", str(ctx.exception))
        self.assertIn("1080", str(ctx.exception))

        # Equal height is allowed: a format conversion with no resize.
        plan = plan_convert(
            _sdr_info(Path("in.mp4")), Path("out.mp4"), target="360p",
            encoder="libx264",
        )
        self.assertEqual((plan.width, plan.height), (640, 360))

    def test_width_follows_the_source_aspect_ratio(self):
        info = MediaInfo(
            path=Path("in.mp4"),
            duration=10.0,
            size=1_000_000,
            bit_rate=None,
            video=VideoStream(
                index=0, codec="h264", width=2560, height=1080, fps=30.0,
                pix_fmt="yuv420p", bit_rate=None,
            ),
            audio=(),
            stream_count=1,
        )
        plan = plan_convert(info, Path("out.mp4"), target="720p", encoder="libx264")
        self.assertEqual(plan.height, 720)
        self.assertEqual(plan.width, 1706)
        self.assertEqual(plan.width % 2, 0)

    def test_fps_flag(self):
        plan = self._plan(_sdr_info(Path("in.mp4")), target="360p", fps=60)
        command = plan.command
        self.assertIn("-r", command)
        self.assertEqual(command[command.index("-r") + 1], "60")

        plan = self._plan(_sdr_info(Path("in.mp4")), target="360p")
        self.assertNotIn("-r", plan.command)

    def test_no_avoid_negative_ts(self):
        plan = self._plan(_hdr_info(Path("in.mp4")), hdr="sdr")
        self.assertNotIn("-avoid_negative_ts", plan.command)

    def _long_info(self) -> MediaInfo:
        """The 4K HDR source with a 60 s duration, so 10-15 s is in range."""
        info = _hdr_info(Path("in.mp4"))
        return MediaInfo(
            path=info.path,
            duration=60.0,
            size=info.size,
            bit_rate=info.bit_rate,
            video=info.video,
            audio=info.audio,
            stream_count=info.stream_count,
        )

    def test_start_puts_ss_before_i(self):
        plan = self._plan(self._long_info(), hdr="sdr", start=10.0)
        command = plan.command
        self.assertIn("-ss", command)
        self.assertEqual(command[command.index("-ss") + 1], "10.000")
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertEqual(plan.start, 10.0)
        self.assertIsNone(plan.end)

    def test_start_and_end_give_t_of_the_length(self):
        plan = self._plan(self._long_info(), hdr="sdr", start=10.0, end=15.0)
        command = plan.command
        self.assertIn("-t", command)
        self.assertEqual(command[command.index("-t") + 1], "5.000")
        self.assertGreater(command.index("-t"), command.index("-i"))
        self.assertEqual(plan.start, 10.0)
        self.assertEqual(plan.end, 15.0)

    def test_end_without_start(self):
        plan = self._plan(self._long_info(), hdr="sdr", end=5.0)
        command = plan.command
        self.assertNotIn("-ss", command)
        self.assertIn("-t", command)
        self.assertEqual(command[command.index("-t") + 1], "5.000")
        self.assertIsNone(plan.start)
        self.assertEqual(plan.end, 5.0)

    def test_no_range_leaves_the_command_unchanged(self):
        with mock.patch("lightbox.convert._pick_encoder", return_value="hevc_nvenc"):
            plan = plan_convert(self._long_info(), Path("out.mp4"), hdr="sdr")
        self.assertNotIn("-ss", plan.command)
        self.assertNotIn("-t", plan.command)
        self.assertIsNone(plan.start)
        self.assertIsNone(plan.end)

    def test_bad_ranges_raise(self):
        info = self._long_info()
        with self.assertRaises(ClipError) as ctx:
            self._plan(info, start=5.0, end=3.0)
        message = str(ctx.exception)
        self.assertIn("0:05.000", message)
        self.assertIn("0:03.000", message)

        with self.assertRaises(ClipError):
            self._plan(info, start=-1)

        with self.assertRaises(ClipError):
            self._plan(info, end=-1)

        with self.assertRaises(ClipError) as ctx:
            self._plan(info, start=60.0)
        self.assertIn("1:00.000", str(ctx.exception))

    def test_end_past_duration_is_clamped(self):
        info = self._long_info()
        plan = self._plan(info, start=2.0, end=99.0)
        self.assertEqual(plan.end, 60.0)
        command = plan.command
        self.assertEqual(command[command.index("-t") + 1], "58.000")

    def test_range_command_has_no_avoid_negative_ts(self):
        plan = self._plan(self._long_info(), hdr="sdr", start=1.0, end=3.0)
        self.assertNotIn("-avoid_negative_ts", plan.command)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class RealConversionTests(unittest.TestCase):
    def _convert(self, src: Path, dst: Path, **kwargs) -> None:
        info = probe(src)
        plan = plan_convert(info, dst, **kwargs)
        run_convert(plan)

    def test_dense_to_240p(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("dense")
            dst = Path(tmp) / "out.mp4"
            self._convert(src, dst, target="240p", quality=35, encoder="libx264")
            self.assertTrue(dst.exists())
            out = probe(dst)
            self.assertEqual(out.video.height, 240)
            self.assertEqual(out.video.width, 426)
            self.assertAlmostEqual(probe_duration(dst), 10.0, delta=0.2)
            self.assertIn(probe_video_codec(dst), ("h264", "hevc"))

    def test_equal_height_no_resize(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("dense")
            dst = Path(tmp) / "out.mp4"
            self._convert(src, dst, target="360p", quality=35, encoder="libx264")
            out = probe(dst)
            self.assertEqual((out.video.width, out.video.height), (640, 360))

    def test_mute_drops_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("dense")
            dst = Path(tmp) / "out.mp4"
            self._convert(src, dst, target="240p", quality=35,
                          encoder="libx264", mute=True)
            self.assertEqual(probe(dst).audio, ())

    def test_path_with_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "my clips"
            folder.mkdir()
            src = folder / "my clip.mp4"
            shutil.copyfile(ensure_sample("dense"), src)
            dst = folder / "out.mp4"
            self._convert(src, dst, target="240p", quality=35, encoder="libx264")
            self.assertTrue(dst.exists())
            self.assertEqual(probe(dst).video.height, 240)

    def test_bad_source_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "not_a_video.mp4"
            src.write_text("this is not a video")
            dst = Path(tmp) / "out.mp4"
            info = MediaInfo(
                path=src, duration=10.0, size=src.stat().st_size, bit_rate=None,
                video=VideoStream(index=0, codec="h264", width=640, height=360,
                                 fps=30.0, pix_fmt="yuv420p", bit_rate=None),
                audio=(), stream_count=1,
            )
            with mock.patch("lightbox.convert._pick_encoder", return_value="libx264"):
                plan = plan_convert(info, dst, target="240p",
                                    encoder="libx264", quality=35)
            with self.assertRaises(ClipError) as ctx:
                run_convert(plan)
            self.assertTrue(str(ctx.exception).startswith("ffmpeg failed:"))

    def test_hdr_fixture_converts_to_sdr_for_real(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("hdr")
            dst = Path(tmp) / "out.mp4"
            info = probe(src)
            plan = plan_convert(info, dst, "360p", hdr="sdr",
                                encoder="libx264", quality=35)
            self.assertTrue(plan.tonemapped)
            run_convert(plan)
            self.assertTrue(dst.exists())
            out = probe(dst)
            self.assertEqual((out.video.width, out.video.height), (640, 360))
            self.assertEqual(out.video.pix_fmt, "yuv420p")
            self.assertNotEqual(out.video.color_transfer, "smpte2084")
            self.assertAlmostEqual(probe_duration(dst), 4.0, delta=0.3)

    def test_convert_a_range_of_the_hdr_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("hdr")
            dst = Path(tmp) / "out.mp4"
            info = probe(src)
            plan = plan_convert(info, dst, "360p", hdr="sdr",
                                encoder="libx264", quality=35,
                                start=1.0, end=3.0)
            self.assertEqual(plan.start, 1.0)
            self.assertEqual(plan.end, 3.0)
            run_convert(plan)
            self.assertTrue(dst.exists())
            out = probe(dst)
            self.assertEqual(out.video.pix_fmt, "yuv420p")
            self.assertAlmostEqual(probe_duration(dst), 2.0, delta=0.3)

    def test_convert_from_a_start_without_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("dense")
            dst = Path(tmp) / "out.mp4"
            info = probe(src)
            plan = plan_convert(info, dst, "240p",
                                encoder="libx264", quality=35, start=2.0)
            self.assertEqual(plan.start, 2.0)
            self.assertIsNone(plan.end)
            run_convert(plan)
            self.assertTrue(dst.exists())
            self.assertAlmostEqual(probe_duration(dst), 8.0, delta=0.3)

    @unittest.skipUnless(
        "libx265" in encoders_available(), "libx265 not available")
    def test_hdr_fixture_stays_hdr_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = ensure_sample("hdr")
            dst = Path(tmp) / "out.mp4"
            info = probe(src)
            plan = plan_convert(info, dst, "360p", hdr="keep",
                                encoder="libx265", quality=35)
            self.assertFalse(plan.tonemapped)
            run_convert(plan)
            self.assertTrue(dst.exists())
            out = probe(dst)
            self.assertIn(out.video.pix_fmt, ("yuv420p10le", "p010le"))
            self.assertEqual(out.video.color_transfer, "smpte2084")


if __name__ == "__main__":
    unittest.main()
