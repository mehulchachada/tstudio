"""Tests for tstudio.cut: command shape, snapping, and real cuts."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import fixture
from tstudio import ClipError
from tstudio.cut import CutPlan, plan_cut, run_cut
from tstudio.media import encoders_available, probe


@unittest.skipUnless(fixture.ffmpeg_available(), "ffmpeg not on PATH")
class CommandShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.info = probe(fixture.ensure_sample("dense"))

    def test_lossless_command_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_cut(self.info, Path(tmp) / "out.mp4", 2.0, 4.0)
            cmd = plan.command
            self.assertIn("-c", cmd)
            self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
            for forbidden in ("-crf", "-b:v", "-vf", "-s", "-r", "hevc_nvenc"):
                self.assertNotIn(forbidden, cmd)
            self.assertLess(cmd.index("-ss"), cmd.index("-i"))
            self.assertIn("-t", cmd)
            self.assertIn("+faststart", cmd)
            self.assertIn("-avoid_negative_ts", cmd)
            self.assertEqual(cmd[cmd.index("-avoid_negative_ts") + 1], "make_zero")

    def test_no_end_no_t(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_cut(self.info, Path(tmp) / "out.mp4", 2.0)
            self.assertNotIn("-t", plan.command)
            self.assertIsNone(plan.end)
            self.assertIsNone(plan.length)

    def test_mkv_has_no_faststart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_cut(self.info, Path(tmp) / "out.mkv", 2.0, 4.0)
            self.assertNotIn("+faststart", plan.command)

    def test_snap_reflected_in_plan(self) -> None:
        info = probe(fixture.ensure_sample("sparse"))
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_cut(info, Path(tmp) / "out.mp4", 3.0, 3.5)
            self.assertAlmostEqual(plan.start, 2.0, delta=0.01)
            self.assertAlmostEqual(plan.drift, 1.0, delta=0.01)
            self.assertAlmostEqual(plan.length, 1.5, delta=0.01)

    def test_backwards_range_is_rejected_even_when_snapping_would_hide_it(self) -> None:
        info = probe(fixture.ensure_sample("sparse"))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ClipError) as ctx:
                plan_cut(info, Path(tmp) / "out.mp4", 3.0, 2.5)
            message = str(ctx.exception)
            self.assertIn("0:03.000", message)
            self.assertIn("0:02.500", message)

    def test_end_just_after_asked_start_survives_snapping(self) -> None:
        info = probe(fixture.ensure_sample("sparse"))
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_cut(info, Path(tmp) / "out.mp4", 3.0, 3.1)
            self.assertAlmostEqual(plan.start, 2.0, delta=0.01)
            self.assertAlmostEqual(plan.length, 1.1, delta=0.01)

    def test_start_past_duration_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for bad_start in (10.0, 99.0):
                with self.assertRaises(ClipError):
                    plan_cut(self.info, Path(tmp) / "out.mp4", bad_start)

    def test_end_before_start_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ClipError):
                plan_cut(self.info, Path(tmp) / "out.mp4", 5.0, 2.0)

    def test_unknown_encoder_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ClipError):
                plan_cut(self.info, Path(tmp) / "out.mp4", 1.0, 2.0,
                         lossless=False, encoder="nosuchencoder")

    def test_end_past_file_is_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_cut(self.info, Path(tmp) / "out.mp4", 2.0, 99.0)
            self.assertAlmostEqual(plan.end, self.info.duration, delta=0.05)


@unittest.skipUnless(fixture.ffmpeg_available(), "ffmpeg not on PATH")
class RealCutTests(unittest.TestCase):
    def test_lossless_cut_dense(self) -> None:
        info = probe(fixture.ensure_sample("dense"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clip.mp4"
            plan = plan_cut(info, out, 2.0, 4.0)
            elapsed = run_cut(plan)
            self.assertIsInstance(elapsed, float)
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)
            self.assertAlmostEqual(fixture.probe_duration(out), 2.0, delta=0.15)
            self.assertEqual(fixture.probe_video_codec(out), "h264")

    def test_lossless_cut_sparse_snaps(self) -> None:
        info = probe(fixture.ensure_sample("sparse"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clip.mp4"
            plan = plan_cut(info, out, 3.0)
            self.assertAlmostEqual(plan.start, 2.0, delta=0.01)
            run_cut(plan)
            self.assertAlmostEqual(fixture.probe_duration(out), 8.0, delta=0.2)

    def test_lossless_cut_silent(self) -> None:
        info = probe(fixture.ensure_sample("silent"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clip.mp4"
            run_cut(plan_cut(info, out, 1.0, 3.0))
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)

    def test_mute_drops_audio(self) -> None:
        info = probe(fixture.ensure_sample("dense"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clip.mp4"
            run_cut(plan_cut(info, out, 1.0, 3.0, mute=True))
            self.assertEqual(probe(out).audio, ())

    def test_path_with_spaces(self) -> None:
        src = fixture.ensure_sample("dense")
        info = probe(src)
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "my gameplay"
            sub.mkdir()
            spaced = sub / "raid clip.mp4"
            shutil.copy2(src, spaced)
            info = probe(spaced)
            out = Path(tmp) / "out dir" / "clip.mp4"
            run_cut(plan_cut(info, out, 1.0, 3.0))
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 0)

    @unittest.skipUnless("libx264" in encoders_available(), "libx264 not available")
    def test_reencode_libx264(self) -> None:
        info = probe(fixture.ensure_sample("dense"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clip.mp4"
            plan = plan_cut(info, out, 1.0, 3.0,
                            lossless=False, encoder="libx264", quality=30)
            self.assertEqual(plan.drift, 0.0)
            run_cut(plan)
            self.assertTrue(out.exists())
            self.assertAlmostEqual(fixture.probe_duration(out), 2.0, delta=0.15)
            self.assertEqual(fixture.probe_video_codec(out), "h264")

    @unittest.skipUnless("libx264" in encoders_available(), "libx264 not available")
    def test_exact_cut_does_not_include_the_previous_keyframe(self) -> None:
        info = probe(fixture.ensure_sample("sparse"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clip.mp4"
            plan = plan_cut(info, out, 3.0, 4.0,
                            lossless=False, encoder="libx264", quality=30)
            self.assertNotIn("-avoid_negative_ts", plan.command)
            run_cut(plan)
            self.assertAlmostEqual(fixture.probe_duration(out), 1.0, delta=0.1)

    @unittest.skipUnless("libx264" in encoders_available(), "libx264 not available")
    def test_progress_callback_fires_and_reaches_done(self) -> None:
        info = probe(fixture.ensure_sample("dense"))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "clip.mp4"
            plan = plan_cut(info, out, 1.0, 3.0,
                            lossless=False, encoder="libx264", quality=30)
            frames: list[dict] = []
            elapsed = run_cut(plan, on_progress=frames.append)
            self.assertGreater(elapsed, 0.0)
            self.assertTrue(out.exists())
            self.assertTrue(frames, "expected at least one progress frame")
            self.assertTrue(
                any(f.get("done") for f in frames),
                "expected a terminal done frame",
            )
            # every frame carries the out_seconds key once ffmpeg reports progress
            self.assertTrue(
                all("out_seconds" in f or f.get("done") for f in frames),
                frames,
            )

    def test_run_cut_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad_src = Path(tmp) / "not a video.mp4"
            bad_src.write_text("hello, this is a text file")
            out = Path(tmp) / "out.mp4"
            plan = CutPlan(
                src=bad_src, dst=out, asked_start=0.0, start=0.0, end=1.0,
                drift=0.0, lossless=True, encoder=None,
                command=["ffmpeg", "-hide_banner", "-loglevel", "error",
                         "-nostdin", "-y", "-ss", "0.000", "-i", str(bad_src),
                         "-t", "1.000", "-map", "0", "-c", "copy",
                         "-avoid_negative_ts", "make_zero", str(out)],
            )
            with self.assertRaises(ClipError) as ctx:
                run_cut(plan)
            self.assertTrue(str(ctx.exception).startswith("ffmpeg failed:"))


if __name__ == "__main__":
    unittest.main()
