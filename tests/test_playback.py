"""Tests for tstudio.gallery.playback: mpv detection, its argv, and the scrub fallback.

Nothing here launches mpv, runs ffmpeg, or needs a real video: every external
call (shutil.which, subprocess.run, probe, art_for) is patched.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tstudio import ClipError
from tstudio.gallery import playback


class MpvAvailableTests(unittest.TestCase):
    def setUp(self):
        playback.mpv_path.cache_clear()
        self.addCleanup(playback.mpv_path.cache_clear)

    def test_found_on_path(self):
        with mock.patch("tstudio.gallery.playback.shutil.which",
                        return_value="C:\\mpv\\mpv.exe"):
            self.assertIs(playback.mpv_available(), True)

    def test_not_found_on_path(self):
        """Nothing on PATH and nothing at the known install locations."""
        with mock.patch("tstudio.gallery.playback.shutil.which", return_value=None),              mock.patch.object(playback, "_MPV_FALLBACK_PATHS", ()):
            self.assertIs(playback.mpv_available(), False)

    def test_found_where_the_installer_left_it(self):
        """winget's mpv adds no PATH entry; it must still be found."""
        with mock.patch("tstudio.gallery.playback.shutil.which", return_value=None),              mock.patch.object(playback, "_MPV_FALLBACK_PATHS", (__file__,)):
            self.assertIs(playback.mpv_available(), True)
            self.assertEqual(playback.mpv_path(), __file__)

    def test_result_is_cached(self):
        with mock.patch("tstudio.gallery.playback.shutil.which",
                        return_value=None) as which:
            playback.mpv_available()
            playback.mpv_available()
            playback.mpv_available()
        self.assertEqual(which.call_count, 1)


def _pin_vo(testcase, vo="tct"):
    """Pin the terminal video output so argv assertions are deterministic."""
    patcher = mock.patch.object(playback, "terminal_vo", return_value=vo)
    patcher.start()
    testcase.addCleanup(patcher.stop)


def _pin_mpv(testcase, where="mpv"):
    """Pin where mpv is, so argv assertions do not depend on this machine."""
    patcher = mock.patch.object(playback, "mpv_path", return_value=where)
    patcher.start()
    testcase.addCleanup(patcher.stop)


class MpvCommandTests(unittest.TestCase):

    def setUp(self):
        _pin_mpv(self)
        _pin_vo(self)
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
        _pin_mpv(self)
        _pin_vo(self)
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


class OpenInPlayerTests(unittest.TestCase):
    """Watching the finished clip: mpv in its own window, else the OS player."""

    def setUp(self) -> None:
        _pin_mpv(self)

    def _file(self, tmp: str) -> Path:
        f = Path(tmp) / "clip.mp4"
        f.write_bytes(bytes(16))
        return f

    def test_windowed_command_has_no_terminal_video_output(self) -> None:
        """Reviewing a result wants a real window, not terminal art."""
        with tempfile.TemporaryDirectory() as tmp:
            argv = playback.player_command(self._file(tmp))
            self.assertEqual(argv[0], "mpv")
            self.assertNotIn("--vo=tct", argv)

    def test_in_terminal_command_asks_for_a_terminal_vo(self) -> None:
        """Whatever terminal_vo answers, and a window when it answers None."""
        with tempfile.TemporaryDirectory() as tmp:
            clip = self._file(tmp)
            with mock.patch.object(playback, "terminal_vo", return_value="sixel"):
                self.assertIn("--vo=sixel", playback.player_command(clip, in_terminal=True))
            with mock.patch.object(playback, "terminal_vo", return_value="tct"):
                self.assertIn("--vo=tct", playback.player_command(clip, in_terminal=True))
            with mock.patch.object(playback, "terminal_vo", return_value=None):
                argv = playback.player_command(clip, in_terminal=True)
                self.assertFalse([a for a in argv if a.startswith("--vo=")], argv)

    def test_sixel_is_the_only_terminal_output_chosen_on_its_own(self) -> None:
        """tct is a guess, and a wrong guess plays audio over a blank screen.

        Measured in the Claude desktop terminal, which answers DA1 without
        attribute 4: `mpv --vo=tct` wrote zero bytes, twice, the second time
        with the size forced. So "no sixel" means a window, not tct.
        """
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(playback.TERMINAL_PLAYBACK_ENV, None)
            with mock.patch("tstudio.gallery.sixel.sixel_supported",
                            return_value=True):
                self.assertEqual(playback.terminal_vo(), "sixel")
                self.assertTrue(playback.plays_in_terminal())
            with mock.patch("tstudio.gallery.sixel.sixel_supported",
                            return_value=False):
                self.assertIsNone(playback.terminal_vo())
                self.assertFalse(playback.plays_in_terminal())

    def test_without_sixel_the_command_opens_a_window_not_tct(self) -> None:
        """The regression: audio played and nothing was drawn."""
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "c.mp4"
            clip.write_bytes(b"x")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(playback.TERMINAL_PLAYBACK_ENV, None)
                with mock.patch("tstudio.gallery.sixel.sixel_supported",
                                return_value=False):
                    argv = playback.mpv_command(clip)
            self.assertNotIn("--vo=tct", argv)
            self.assertFalse([a for a in argv if a.startswith("--vo=")],
                             f"a terminal vo slipped through: {argv}")

    def test_tct_can_still_be_forced_for_terminals_that_do_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "c.mp4"
            clip.write_bytes(b"x")
            with mock.patch.dict(
                    os.environ, {playback.TERMINAL_PLAYBACK_ENV: "tct"}):
                self.assertEqual(playback.terminal_vo(), "tct")
                self.assertIn("--vo=tct", playback.mpv_command(clip))

    def test_terminal_playback_can_be_switched_off_outright(self) -> None:
        with mock.patch.dict(
                os.environ, {playback.TERMINAL_PLAYBACK_ENV: "0"}):
            with mock.patch("tstudio.gallery.sixel.sixel_supported",
                            return_value=True):
                self.assertIsNone(playback.terminal_vo())

    def test_uses_mpv_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = self._file(tmp)
            with mock.patch.object(playback, "mpv_available", return_value=True),                  mock.patch.object(playback.subprocess, "Popen") as popen:
                self.assertEqual(playback.open_in_player(f), "mpv")
            self.assertEqual(popen.call_args[0][0][0], "mpv")

    def test_falls_back_to_the_system_player(self) -> None:
        """A missing mpv is a fallback, not an error."""
        with tempfile.TemporaryDirectory() as tmp:
            f = self._file(tmp)
            with mock.patch.object(playback, "mpv_available", return_value=False),                  mock.patch.object(playback.sys, "platform", "win32"),                  mock.patch.object(playback.os, "startfile", create=True) as start:
                self.assertEqual(playback.open_in_player(f), "the system player")
            self.assertTrue(start.called)

    def test_a_missing_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ClipError):
                playback.open_in_player(Path(tmp) / "nope.mp4")
