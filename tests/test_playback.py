"""Tests for tstudio.gallery.playback: mpv detection, its argv, and the scrub fallback.

Nothing here launches mpv, runs ffmpeg, or needs a real video: every external
call (shutil.which, subprocess.run, probe, art_for) is patched.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tstudio import ClipError
from tstudio.gallery import playback


class MpvAvailableTests(unittest.TestCase):
    def setUp(self):
        playback.mpv_available.cache_clear()
        self.addCleanup(playback.mpv_available.cache_clear)

    def test_found_on_path(self):
        with mock.patch("tstudio.gallery.playback.shutil.which",
                        return_value="C:\\mpv\\mpv.exe"):
            self.assertIs(playback.mpv_available(), True)

    def test_not_found_on_path(self):
        with mock.patch("tstudio.gallery.playback.shutil.which", return_value=None):
            self.assertIs(playback.mpv_available(), False)

    def test_result_is_cached(self):
        with mock.patch("tstudio.gallery.playback.shutil.which",
                        return_value=None) as which:
            playback.mpv_available()
            playback.mpv_available()
            playback.mpv_available()
        self.assertEqual(which.call_count, 1)


class MpvCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip = Path(self._tmp.name) / "clip.mp4"
        self.clip.write_bytes(b"not a real mp4; mpv_command never opens it")

    def test_argv_is_exactly_right(self):
        self.assertEqual(
            playback.mpv_command(self.clip),
            ["mpv", "--vo=tct", "--really-quiet", str(self.clip)],
        )

    def test_missing_path_raises_clip_error(self):
        missing = Path(self._tmp.name) / "gone.mp4"
        with self.assertRaises(ClipError) as ctx:
            playback.mpv_command(missing)
        self.assertIn("gone.mp4", str(ctx.exception))


class PlayWithMpvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip = Path(self._tmp.name) / "clip.mp4"
        self.clip.write_bytes(b"not a real mp4; subprocess.run is patched")

    def test_missing_mpv_tells_you_how_to_install_it(self):
        with mock.patch("tstudio.gallery.playback.mpv_available", return_value=False):
            with mock.patch("tstudio.gallery.playback.subprocess.run") as run:
                with self.assertRaises(ClipError) as ctx:
                    playback.play_with_mpv(self.clip)
        self.assertIn("winget install shinchiro.mpv", str(ctx.exception))
        run.assert_not_called()

    def test_returns_the_exit_code_without_launching_anything(self):
        fake = SimpleNamespace(returncode=7)
        with mock.patch("tstudio.gallery.playback.mpv_available", return_value=True):
            with mock.patch("tstudio.gallery.playback.subprocess.run",
                            return_value=fake) as run:
                code = playback.play_with_mpv(self.clip)
        self.assertEqual(code, 7)
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["mpv", "--vo=tct", "--really-quiet", str(self.clip)])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_missing_file_raises_clip_error(self):
        missing = Path(self._tmp.name) / "gone.mp4"
        with mock.patch("tstudio.gallery.playback.mpv_available", return_value=True):
            with mock.patch("tstudio.gallery.playback.subprocess.run") as run:
                with self.assertRaises(ClipError):
                    playback.play_with_mpv(missing)
        run.assert_not_called()

    def test_unstartable_mpv_raises_clip_error(self):
        with mock.patch("tstudio.gallery.playback.mpv_available", return_value=True):
            with mock.patch("tstudio.gallery.playback.subprocess.run",
                            side_effect=OSError("no exec")):
                with self.assertRaises(ClipError) as ctx:
                    playback.play_with_mpv(self.clip)
        self.assertIn("mpv", str(ctx.exception))


class ScrubTimestampTests(unittest.TestCase):
    def test_ten_steps_through_ten_seconds(self):
        times = playback.scrub_timestamps(10.0, 10)
        self.assertEqual(len(times), 10)
        for i, got in enumerate(times):
            self.assertAlmostEqual(got, float(i))

    def test_zero_duration_gives_one_timestamp(self):
        self.assertEqual(playback.scrub_timestamps(0, 10), [0.0])

    def test_negative_duration_gives_one_timestamp(self):
        self.assertEqual(playback.scrub_timestamps(-5.0, 10), [0.0])

    def test_one_step_starts_at_zero(self):
        self.assertEqual(playback.scrub_timestamps(5.0, 1), [0.0])

    def test_zero_steps_raises_clip_error(self):
        with self.assertRaises(ClipError):
            playback.scrub_timestamps(10.0, 0)

    def test_negative_steps_raises_clip_error(self):
        with self.assertRaises(ClipError):
            playback.scrub_timestamps(10.0, -3)

    def test_scrub_timestamps_never_reaches_the_end(self):
        # Seeking to exactly the end yields no frame, so the last stop must be inside.
        for duration in (0.04, 1.0, 3.0, 7.5, 10.0, 59.94, 3600.0):
            for steps in (1, 2, 3, 10, 24):
                times = playback.scrub_timestamps(duration, steps)
                self.assertEqual(len(times), steps)
                self.assertAlmostEqual(times[0], 0.0)
                for t in times:
                    self.assertGreaterEqual(t, 0.0)
                    self.assertLess(t, duration, f"{duration=} {steps=} {t=}")

    def test_timestamps_increase(self):
        times = playback.scrub_timestamps(7.5, 10)
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(set(times)), len(times))


class ScrubFrameTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip = Path(self._tmp.name) / "clip.mp4"
        self.clip.write_bytes(b"not a real mp4; probe and art_for are patched")
        self.seen: list[float | None] = []

        def fake_art_for(path, cols, rows, *, at=None):
            self.seen.append(at)
            return [f"frame at {at}"]

        probe_patch = mock.patch(
            "tstudio.gallery.playback.probe",
            return_value=SimpleNamespace(duration=10.0),
        )
        art_patch = mock.patch(
            "tstudio.gallery.playback.art_for", side_effect=fake_art_for
        )
        self.probe = probe_patch.start()
        self.art_for = art_patch.start()
        self.addCleanup(probe_patch.stop)
        self.addCleanup(art_patch.stop)

    def test_index_picks_its_timestamp(self):
        art = playback.scrub_frame(self.clip, 3, 40, 12)
        self.assertEqual(art, ["frame at 3.0"])
        self.assertEqual(len(self.seen), 1)
        self.assertAlmostEqual(self.seen[0], 3.0)

    def test_index_wraps_past_the_last_step(self):
        playback.scrub_frame(self.clip, 13, 40, 12)
        self.assertAlmostEqual(self.seen[0], 3.0)

    def test_negative_index_wraps_to_the_end(self):
        playback.scrub_frame(self.clip, -1, 40, 12)
        self.assertAlmostEqual(self.seen[0], 9.0)

    def test_size_is_passed_through(self):
        playback.scrub_frame(self.clip, 0, 40, 12)
        self.art_for.assert_called_once_with(self.clip, 40, 12, at=0.0)

    def test_steps_changes_the_spacing(self):
        playback.scrub_frame(self.clip, 1, 40, 12, steps=4)
        self.assertAlmostEqual(self.seen[0], 2.5)

    def test_bad_steps_raises_clip_error(self):
        with self.assertRaises(ClipError):
            playback.scrub_frame(self.clip, 0, 40, 12, steps=0)
        self.art_for.assert_not_called()

    def test_missing_file_raises_clip_error(self):
        missing = Path(self._tmp.name) / "gone.mp4"
        with self.assertRaises(ClipError):
            playback.scrub_frame(missing, 0, 40, 12)
        self.probe.assert_not_called()
        self.art_for.assert_not_called()

    def test_zero_length_clip_still_renders_one_frame(self):
        self.probe.return_value = SimpleNamespace(duration=0.0)
        playback.scrub_frame(self.clip, 5, 40, 12)
        self.assertAlmostEqual(self.seen[0], 0.0)


class HintTests(unittest.TestCase):
    def test_hint_is_the_exact_one_liner(self):
        self.assertEqual(
            playback.MPV_HINT,
            "install mpv for in-terminal playback: winget install shinchiro.mpv",
        )
        self.assertNotIn("\n", playback.MPV_HINT)


if __name__ == "__main__":
    unittest.main()
