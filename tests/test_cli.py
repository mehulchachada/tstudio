"""End-to-end tests for the clip command, calling main() in-process."""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from tstudio.cli import main
from fixture import ensure_sample, ffmpeg_available, probe_duration
from tstudio.media import encoders_available


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    """Call main(), capturing stdout and stderr; return (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


@unittest.skipUnless(ffmpeg_available(), "ffmpeg/ffprobe not on PATH")
class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def test_basic_cut(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "-O", str(self.tmp)])
        self.assertEqual(code, 0, err)
        files = list(self.tmp.iterdir())
        self.assertEqual(len(files), 1)
        self.assertGreater(files[0].stat().st_size, 0)
        self.assertAlmostEqual(probe_duration(files[0]), 2.0, delta=0.15)

    def test_two_ranges(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "1-2", "3-4", "-O", str(self.tmp)])
        self.assertEqual(code, 0, err)
        names = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(len(names), 2)
        for p in self.tmp.iterdir():
            self.assertGreater(p.stat().st_size, 0)

    def test_dry_run(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "-O", str(self.tmp), "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertIn("-c", out)
        self.assertIn("copy", out)
        self.assertIn(str(dense), out)

    def test_dry_run_to_end_shows_real_end_on_cutting_line(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-end", "-O", str(self.tmp), "--dry-run"])
        self.assertEqual(code, 0, err)
        cutting = [l for l in out.splitlines() if l.startswith("cutting")]
        self.assertEqual(len(cutting), 1)
        self.assertIn("0:10.000", cutting[0])
        self.assertNotIn("end", cutting[0])
        asked = [l for l in out.splitlines() if l.startswith("asked")]
        self.assertEqual(len(asked), 1)
        self.assertIn("end", asked[0])

    def test_relative_first_percent(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "first 50%", "-O", str(self.tmp)])
        self.assertEqual(code, 0, err)
        files = list(self.tmp.iterdir())
        self.assertEqual(len(files), 1)
        self.assertGreater(files[0].stat().st_size, 0)
        self.assertAlmostEqual(probe_duration(files[0]), 5.0, delta=0.15)

    def test_relative_last_seconds(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "last 3s", "-O", str(self.tmp)])
        self.assertEqual(code, 0, err)
        files = list(self.tmp.iterdir())
        self.assertEqual(len(files), 1)
        self.assertAlmostEqual(probe_duration(files[0]), 3.0, delta=0.2)

    def test_relative_around(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "around 3s", "-O", str(self.tmp)])
        self.assertEqual(code, 0, err)
        files = list(self.tmp.iterdir())
        self.assertEqual(len(files), 1)
        # default half-window is 5s: [3-5, 3+5] clamped to [0,10] => 0..8 => 8s
        self.assertAlmostEqual(probe_duration(files[0]), 8.0, delta=0.3)

    def test_relative_dry_run_shows_resolved_end(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "first 50%", "-O", str(self.tmp), "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertEqual(list(self.tmp.iterdir()), [])
        cutting = [l for l in out.splitlines() if l.startswith("cutting")]
        self.assertEqual(len(cutting), 1)
        self.assertIn("0:05.000", cutting[0])

    def test_out_with_two_ranges(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "1-2", "3-4", "-o", str(self.tmp / "x.mp4")])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_bad_time(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "banana"])
        self.assertEqual(code, 2)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertNotIn("Traceback", err)

    def test_missing_input(self) -> None:
        code, out, err = _run_main([str(self.tmp / "nope.mp4"), "1-2"])
        self.assertEqual(code, 2)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertNotIn("Traceback", err)

    def test_help_mentions_relative_ranges(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        text = out.getvalue()
        for word in ("first", "last", "around"):
            self.assertIn(word, text)
        self.assertIn("'", text)

    def test_no_range(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense)])
        self.assertEqual(code, 2)
        self.assertIn("clip video.mp4 3:49", err)

    def test_info(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "--info"])
        self.assertEqual(code, 0, err)
        self.assertIn("640x360", out)
        self.assertIn("h264", out)
        self.assertIn("keyframes", out)

    def test_keyframes(self) -> None:
        sparse = ensure_sample("sparse")
        code, out, err = _run_main([str(sparse), "--keyframes", "3"])
        self.assertEqual(code, 0, err)
        self.assertIn("2.000", out)
        lines = [l for l in out.splitlines() if "<-- nearest" in l]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("0:02.000"), lines[0])

    def test_unique_name(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "-O", str(self.tmp)])
        self.assertEqual(code, 0, err)
        first = next(self.tmp.iterdir())
        size_before = first.stat().st_size
        code, out, err = _run_main([str(dense), "2-4", "-O", str(self.tmp)])
        self.assertEqual(code, 0, err)
        files = sorted(self.tmp.iterdir(), key=lambda p: p.name)
        self.assertEqual(len(files), 2)
        self.assertTrue(any(p.name.endswith("_2.mp4") for p in files))
        self.assertEqual(first.stat().st_size, size_before)

    def test_overwrite(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "-O", str(self.tmp)])
        self.assertEqual(code, 0, err)
        code, out, err = _run_main([str(dense), "2-4", "-O", str(self.tmp), "--overwrite"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(list(self.tmp.iterdir())), 1)

    def test_encoder_without_exact(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--encoder", "libx264"])
        self.assertEqual(code, 2)
        self.assertIn("--encoder only applies with --exact", err)

    def test_quiet(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "-O", str(self.tmp), "--quiet"])
        self.assertEqual(code, 0, err)
        files = list(self.tmp.iterdir())
        self.assertEqual(out, f"{files[0]}\n")

    @unittest.skipUnless("libx264" in encoders_available(), "libx264 not available")
    def test_exact(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main(
            [str(dense), "2-4", "-O", str(self.tmp), "--exact", "--encoder", "libx264", "-q", "30"]
        )
        self.assertEqual(code, 0, err)
        files = list(self.tmp.iterdir())
        self.assertEqual(len(files), 1)
        self.assertGreater(files[0].stat().st_size, 0)
        self.assertAlmostEqual(probe_duration(files[0]), 2.0, delta=0.15)
        self.assertIn("re-encoded", out)

    def test_space_in_dir(self) -> None:
        dense = ensure_sample("dense")
        self._src = tempfile.TemporaryDirectory()
        src_dir = Path(self._src.name) / "my clips"
        src_dir.mkdir()
        dest = src_dir / dense.name
        shutil.copy(dense, dest)
        code, out, err = _run_main([str(dest), "2-4", "-O", str(self.tmp)])
        self.assertEqual(code, 0, err)
        files = list(self.tmp.iterdir())
        self.assertEqual(len(files), 1)
        self.assertGreater(files[0].stat().st_size, 0)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        if getattr(self, "_src", None) is not None:
            self._src.cleanup()


@unittest.skipUnless(ffmpeg_available(), "ffmpeg/ffprobe not on PATH")
class TestTuiFlag(unittest.TestCase):
    """`--tui` must delegate to clip_tui.run_editor without starting a real app.

    We patch clip_tui.run_editor (the module the lazy import inside clip.py
    resolves at call time), not a copy on clip, so the stub is what runs.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stub(self) -> "unittest.mock.MagicMock":
        from unittest import mock
        patcher = mock.patch("tstudio.tui.run_editor", return_value=0)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_tui_flag_rejects_dry_run(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--tui", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertNotIn("Traceback", err)

    def test_tui_flag_rejects_info(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--tui", "--info"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertNotIn("Traceback", err)

    def test_tui_flag_rejects_keyframes(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--tui", "--keyframes", "2"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertNotIn("Traceback", err)

    def test_tui_flag_rejects_quiet(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--tui", "--quiet"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertNotIn("Traceback", err)

    def test_tui_flag_rejects_two_ranges(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "1-2", "3-4", "--tui"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertNotIn("Traceback", err)

    def test_tui_flag_splits_a_range(self) -> None:
        stub = self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "3:49-4:30", "--tui"])
        self.assertEqual(code, 0, err)
        stub.assert_called_once()
        self.assertEqual(stub.call_args.args[1], "3:49")
        self.assertEqual(stub.call_args.args[2], "4:30")

    def test_tui_flag_with_bare_start(self) -> None:
        stub = self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "3:49", "--tui"])
        self.assertEqual(code, 0, err)
        self.assertEqual(stub.call_args.args[1], "3:49")
        self.assertEqual(stub.call_args.args[2], "")

    def test_tui_flag_with_no_range(self) -> None:
        stub = self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "--tui"])
        self.assertEqual(code, 0, err)
        self.assertEqual(stub.call_args.args[1], "")
        self.assertEqual(stub.call_args.args[2], "")

    def test_tui_flag_passes_relative_range_unparsed(self) -> None:
        stub = self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "last 45s", "--tui"])
        self.assertEqual(code, 0, err)
        self.assertEqual(stub.call_args.args[1], "last 45s")
        self.assertEqual(stub.call_args.args[2], "")

    def test_tui_flag_passes_out_path(self) -> None:
        stub = self._stub()
        dense = ensure_sample("dense")
        dest = self.tmp / "x.mp4"
        code, out, err = _run_main([str(dense), "2-4", "--tui", "-o", str(dest)])
        self.assertEqual(code, 0, err)
        self.assertEqual(stub.call_args.args[3], dest)

    def test_cli_still_works_without_textual(self) -> None:
        import sys
        from unittest import mock
        dense = ensure_sample("dense")
        with mock.patch.dict(sys.modules, {"textual": None}):
            code, out, err = _run_main([str(dense), "2-4", "-O", str(self.tmp), "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertIn("copy", out)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg/ffprobe not on PATH")
class TestConversionFlags(unittest.TestCase):
    """`--to` / `--for` flag logic, with clip.run_convert stubbed out."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stub(self) -> "unittest.mock.MagicMock":
        from unittest import mock
        patcher = mock.patch("tstudio.cli.run_convert", return_value=0.5)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_to_and_for_together_refused(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "--to", "240p", "--for", "shorts"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertIn("--to", err)
        self.assertIn("--for", err)
        self.assertNotIn("Traceback", err)

    def test_fit_without_for_refused(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "--fit", "crop"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertIn("--fit", err)
        self.assertIn("--for", err)
        self.assertNotIn("Traceback", err)

    def test_hdr_without_to_refused(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "--hdr", "sdr"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertIn("--hdr", err)
        self.assertIn("--to", err)
        self.assertNotIn("Traceback", err)

    def test_fps_without_to_refused(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "--fps", "30"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertIn("--fps", err)
        self.assertIn("--to", err)
        self.assertNotIn("Traceback", err)

    def test_exact_with_to_refused(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--exact", "--to", "240p"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertIn("--exact", err)
        self.assertNotIn("Traceback", err)

    def test_exact_with_for_refused(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--exact", "--for", "shorts"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertIn("--exact", err)
        self.assertNotIn("Traceback", err)

    def test_tui_with_to_refused(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--tui", "--to", "240p"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertIn("--tui", err)
        self.assertNotIn("Traceback", err)

    def test_tui_with_for_refused(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--tui", "--for", "shorts"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertIn("--tui", err)
        self.assertNotIn("Traceback", err)

    def test_encoder_with_to_accepted(self) -> None:
        stub = self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "--to", "240p", "--encoder", "libx264", "-q", "35"])
        self.assertEqual(code, 0, err)
        self.assertEqual(stub.call_count, 1)
        self.assertEqual(stub.call_args.args[0].encoder, "libx264")

    def test_encoder_with_none_refused(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--encoder", "libx264"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        self.assertIn("--encoder", err)
        self.assertNotIn("Traceback", err)

    def test_to_whole_file_has_no_range(self) -> None:
        stub = self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "--to", "240p"])
        self.assertEqual(code, 0, err)
        plan = stub.call_args.args[0]
        self.assertIsNone(plan.start)
        self.assertIsNone(plan.end)

    def test_to_with_range_passes_the_range(self) -> None:
        stub = self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--to", "240p"])
        self.assertEqual(code, 0, err)
        plan = stub.call_args.args[0]
        self.assertEqual(plan.start, 2.0)
        self.assertEqual(plan.end, 4.0)

    def test_for_uses_the_preset_size(self) -> None:
        stub = self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "--for", "shorts"])
        self.assertEqual(code, 0, err)
        plan = stub.call_args.args[0]
        self.assertEqual((plan.width, plan.height), (1080, 1920))

    def test_for_accepts_an_alias(self) -> None:
        stub = self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "--for", "youtube-shorts"])
        self.assertEqual(code, 0, err)
        plan = stub.call_args.args[0]
        self.assertEqual((plan.width, plan.height), (1080, 1920))

    def test_two_ranges_make_two_outputs(self) -> None:
        stub = self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "1-2", "3-4", "--to", "240p"])
        self.assertEqual(code, 0, err)
        self.assertEqual(stub.call_count, 2)
        dests = [c.args[0].dst for c in stub.call_args_list]
        self.assertNotEqual(dests[0], dests[1])

    def test_out_tag_is_in_the_name(self) -> None:
        stub = self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--to", "240p"])
        self.assertEqual(code, 0, err)
        self.assertTrue(stub.call_args.args[0].dst.stem.endswith("_240p"))
        code, out, err = _run_main([str(dense), "2-4", "--for", "shorts"])
        self.assertEqual(code, 0, err)
        self.assertTrue(stub.call_args.args[0].dst.stem.endswith("_shorts"))

    def test_unknown_target_is_refused(self) -> None:
        self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "--to", "999p"])
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("tstudio: "), err)
        for target in ("2160p", "1440p", "1080p", "720p", "480p", "360p", "240p"):
            self.assertIn(target, err)
        self.assertNotIn("Traceback", err)

    def test_duration_warning_is_printed_not_fatal(self) -> None:
        from unittest import mock
        self._stub()
        patcher = mock.patch("tstudio.cli.duration_warning", return_value="this preset is capped at 60s")
        self.addCleanup(patcher.stop)
        patcher.start()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--for", "shorts"])
        self.assertEqual(code, 0, err)
        self.assertIn("this preset is capped at 60s", out)

    def test_dry_run_conversion_writes_nothing(self) -> None:
        stub = self._stub()
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--to", "240p", "-O", str(self.tmp), "-n"])
        self.assertEqual(code, 0, err)
        self.assertIn("-vf", out)
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertFalse(stub.called)

    def test_real_conversion_end_to_end(self) -> None:
        dense = ensure_sample("dense")
        code, out, err = _run_main([str(dense), "2-4", "--to", "240p", "-O", str(self.tmp),
                                    "--encoder", "libx264", "-q", "35"])
        self.assertEqual(code, 0, err)
        files = list(self.tmp.iterdir())
        self.assertEqual(len(files), 1)
        from tstudio.media import probe
        info = probe(files[0])
        self.assertEqual(info.video.height, 240)
        self.assertAlmostEqual(info.duration, 2.0, delta=0.3)

    def test_real_social_export_end_to_end(self) -> None:
        hdr = ensure_sample("hdr")
        code, out, err = _run_main([str(hdr), "--for", "shorts", "--fit", "crop",
                                    "-O", str(self.tmp), "--encoder", "libx264", "-q", "35"])
        self.assertEqual(code, 0, err)
        files = list(self.tmp.iterdir())
        self.assertEqual(len(files), 1)
        from tstudio.media import probe
        info = probe(files[0])
        self.assertEqual((info.video.width, info.video.height), (1080, 1920))
        self.assertNotEqual(info.video.color_transfer, "smpte2084")


if __name__ == "__main__":
    unittest.main()
