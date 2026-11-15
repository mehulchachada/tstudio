"""Tests for tstudio.gallery.sixel: terminal detection, argv, rendering, sizing."""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tstudio import ClipError
from tstudio.gallery import sixel

HAVE_MAGICK = shutil.which("magick") is not None


class _FakeStream:
    """A stdin/stdout stand-in whose tty-ness and writes we control."""

    def __init__(self, is_tty: bool = True, fd: int = 0) -> None:
        self._is_tty = is_tty
        self._fd = fd
        self.written: list[str] = []
        self.flushes = 0

    def isatty(self) -> bool:
        return self._is_tty

    def fileno(self) -> int:
        return self._fd

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushes += 1


class _NoIsattyStream:
    """A stream missing ``isatty`` entirely, like some test doubles."""

    def __init__(self) -> None:
        self.written: list[str] = []

    def fileno(self) -> int:
        return 0

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        pass


class _FakePosixTerminal:
    """Stands in for termios, tty, select and os.read all at once.

    ``chunks`` is the byte groups stdin will hand back; an empty list means
    the terminal never answers, i.e. a timeout. ``restores`` counts the
    tcsetattr calls, which is how we prove the mode was put back.
    """

    TCSADRAIN = 1

    def __init__(self, chunks: list[bytes] | None = None) -> None:
        self.chunks = list(chunks or [])
        self.raw_calls = 0
        self.restores = 0
        self.saved = ("saved", "attrs")

    # tty
    def setraw(self, fd, when=None):
        self.raw_calls += 1

    # termios
    def tcgetattr(self, fd):
        return self.saved

    def tcsetattr(self, fd, when, attrs):
        assert attrs == self.saved, "restored something other than the saved mode"
        self.restores += 1

    # select
    def select(self, rlist, wlist, xlist, timeout):
        if self.chunks:
            return (list(rlist), [], [])
        return ([], [], [])

    # os.read
    def read(self, fd, size):
        return self.chunks.pop(0)


class _FakeMsvcrt:
    """Stands in for msvcrt: hands back ``text`` one character at a time."""

    def __init__(self, text: str = "") -> None:
        self.chars = list(text)

    def kbhit(self) -> bool:
        return bool(self.chars)

    def getwch(self) -> str:
        return self.chars.pop(0)


class _Da1TestCase(unittest.TestCase):
    """Shared plumbing: nothing here may touch a real terminal."""

    def _use_streams(self, stdin, stdout) -> None:
        self.enterContext(mock.patch.object(sixel.sys, "stdin", stdin))
        self.enterContext(mock.patch.object(sixel.sys, "stdout", stdout))

    def _use_posix(self, terminal: _FakePosixTerminal) -> None:
        self.enterContext(mock.patch.object(sixel, "_IS_WINDOWS", False))
        self.enterContext(
            mock.patch.dict(
                sys.modules,
                {"termios": terminal, "tty": terminal, "select": terminal},
            )
        )
        self.enterContext(mock.patch.object(sixel.os, "read", terminal.read))

    def _use_windows(self, fake: _FakeMsvcrt) -> None:
        self.enterContext(mock.patch.object(sixel, "_IS_WINDOWS", True))
        self.enterContext(mock.patch.dict(sys.modules, {"msvcrt": fake}))


class QueryDa1Tests(_Da1TestCase):
    def test_no_query_without_a_stdin_tty(self):
        stdin = _FakeStream(is_tty=False)
        stdout = _FakeStream(is_tty=True)
        self._use_streams(stdin, stdout)
        self.assertIsNone(sixel.query_da1())
        self.assertEqual(stdout.written, [])

    def test_no_query_without_a_stdout_tty(self):
        stdin = _FakeStream(is_tty=True)
        stdout = _FakeStream(is_tty=False)
        self._use_streams(stdin, stdout)
        self.assertIsNone(sixel.query_da1())
        self.assertEqual(stdout.written, [])

    def test_a_stream_without_isatty_is_not_a_terminal(self):
        stdout = _NoIsattyStream()
        self._use_streams(_FakeStream(is_tty=True), stdout)
        self.assertIsNone(sixel.query_da1())
        self.assertEqual(stdout.written, [])

        stdin = _NoIsattyStream()
        tty_out = _FakeStream(is_tty=True)
        self._use_streams(stdin, tty_out)
        self.assertIsNone(sixel.query_da1())
        self.assertEqual(tty_out.written, [])

    def test_da1_reply_is_parsed(self):
        stdout = _FakeStream(is_tty=True)
        terminal = _FakePosixTerminal([b"\x1b[?62;1;2;4;6;9c"])
        self._use_streams(_FakeStream(is_tty=True), stdout)
        self._use_posix(terminal)
        self.assertEqual(sixel.query_da1(), [62, 1, 2, 4, 6, 9])
        self.assertEqual(stdout.written, ["\x1b[c"])
        self.assertEqual(terminal.raw_calls, 1)
        self.assertEqual(terminal.restores, 1)

    def test_the_query_bytes_are_escape_bracket_c(self):
        self.assertEqual(sixel.DA1_QUERY, "\x1b[c")
        self.assertEqual(sixel.DA1_QUERY.encode("ascii"), b"\x1b[c")

    def test_a_reply_split_across_reads_is_assembled(self):
        terminal = _FakePosixTerminal([b"\x1b[?6", b"2;4", b"c"])
        self._use_streams(_FakeStream(is_tty=True), _FakeStream(is_tty=True))
        self._use_posix(terminal)
        self.assertEqual(sixel.query_da1(), [62, 4])
        self.assertEqual(terminal.restores, 1)

    def test_stray_bytes_before_the_csi_are_ignored(self):
        terminal = _FakePosixTerminal([b"junk\x1b[?62;4c"])
        self._use_streams(_FakeStream(is_tty=True), _FakeStream(is_tty=True))
        self._use_posix(terminal)
        self.assertEqual(sixel.query_da1(), [62, 4])

    def test_unparseable_reply_returns_none_and_restores_the_mode(self):
        terminal = _FakePosixTerminal([b"not an escape sequence at all c"])
        self._use_streams(_FakeStream(is_tty=True), _FakeStream(is_tty=True))
        self._use_posix(terminal)
        self.assertIsNone(sixel.query_da1())
        self.assertEqual(terminal.restores, 1)

    def test_timeout_with_no_data_returns_none_and_restores_the_mode(self):
        terminal = _FakePosixTerminal([])
        self._use_streams(_FakeStream(is_tty=True), _FakeStream(is_tty=True))
        self._use_posix(terminal)
        self.assertIsNone(sixel.query_da1(timeout=0.01))
        self.assertEqual(terminal.restores, 1)

    def test_the_mode_is_restored_even_when_reading_blows_up(self):
        terminal = _FakePosixTerminal([b"\x1b[?62;4c"])
        self._use_streams(_FakeStream(is_tty=True), _FakeStream(is_tty=True))
        self._use_posix(terminal)
        with mock.patch.object(sixel.os, "read", side_effect=OSError("boom")):
            self.assertIsNone(sixel.query_da1())
        self.assertEqual(terminal.restores, 1)

    def test_windows_polling_parses_the_reply(self):
        stdout = _FakeStream(is_tty=True)
        self._use_streams(_FakeStream(is_tty=True), stdout)
        self._use_windows(_FakeMsvcrt("\x1b[?62;1;2;4;6;9c"))
        self.assertEqual(sixel.query_da1(), [62, 1, 2, 4, 6, 9])
        self.assertEqual(stdout.written, ["\x1b[c"])

    def test_windows_timeout_returns_none(self):
        self._use_streams(_FakeStream(is_tty=True), _FakeStream(is_tty=True))
        self._use_windows(_FakeMsvcrt(""))
        self.assertIsNone(sixel.query_da1(timeout=0.01))


class DetectionReportTests(_Da1TestCase):
    def setUp(self):
        sixel.sixel_supported.cache_clear()
        self.addCleanup(sixel.sixel_supported.cache_clear)

    def test_report_names_the_verdict(self):
        self._use_streams(_FakeStream(is_tty=True), _FakeStream(is_tty=True))
        self._use_posix(_FakePosixTerminal([b"\x1b[?62;1;2;4c"]))
        with mock.patch.dict(sixel.os.environ, {"TERM": "mintty"}, clear=True):
            report = sixel.detection_report()
        self.assertIsInstance(report, str)
        self.assertIn("verdict", report)
        self.assertIn("True", report)
        self.assertIn("TERM=", report)
        self.assertIn("62", report)

    def test_report_survives_having_no_tty(self):
        self._use_streams(_FakeStream(is_tty=False), _FakeStream(is_tty=False))
        with mock.patch.dict(sixel.os.environ, {}, clear=True):
            report = sixel.detection_report()
        self.assertIn("verdict", report)
        self.assertIn("no answer", report)
        self.assertGreater(len(report.splitlines()), 1)


class SupportedTests(unittest.TestCase):
    def setUp(self):
        sixel.sixel_supported.cache_clear()
        self.addCleanup(sixel.sixel_supported.cache_clear)

    def _supported(self, env: dict[str, str]) -> bool:
        """Env-only verdict: the terminal is taken to be silent."""
        sixel.sixel_supported.cache_clear()
        with mock.patch.object(sixel, "query_da1", return_value=None):
            with mock.patch.dict(sixel.os.environ, env, clear=True):
                return sixel.sixel_supported()

    def _supported_with_da1(self, attrs, env: dict[str, str]) -> bool:
        sixel.sixel_supported.cache_clear()
        with mock.patch.object(sixel, "query_da1", return_value=attrs):
            with mock.patch.dict(sixel.os.environ, env, clear=True):
                return sixel.sixel_supported()

    def test_the_terminals_own_answer_beats_the_env_vars(self):
        # kitty says no by env, but the terminal answered with attribute 4.
        self.assertTrue(
            self._supported_with_da1(
                [62, 1, 2, 4, 6, 9], {"KITTY_WINDOW_ID": "1", "TERM": "xterm-kitty"}
            )
        )
        # Windows Terminal says yes by env, but this reply has no 4 in it.
        self.assertFalse(
            self._supported_with_da1(
                [62, 1, 2, 6, 9], {"WT_SESSION": "abc-123", "TERM_PROGRAM": "WezTerm"}
            )
        )

    def test_override_still_beats_a_da1_answer(self):
        self.assertTrue(self._supported_with_da1([62, 1], {"TSTUDIO_SIXEL": "1"}))
        self.assertFalse(self._supported_with_da1([62, 4], {"TSTUDIO_SIXEL": "0"}))

    def test_an_empty_da1_reply_means_no_sixel(self):
        self.assertFalse(self._supported_with_da1([], {"WT_SESSION": "abc"}))

    def test_mintty_via_term_program(self):
        self.assertTrue(self._supported({"TERM_PROGRAM": "mintty"}))

    def test_git_bash_in_mintty_is_supported(self):
        self.assertTrue(
            self._supported({"MSYSTEM": "UCRT64", "TERM": "xterm-256color"})
        )

    def test_msystem_alone_is_not_enough(self):
        self.assertFalse(self._supported({"MSYSTEM": "UCRT64", "TERM": "dumb"}))

    def test_manual_override_wins(self):
        # True even inside kitty, which otherwise reports False.
        for value in ("1", "true", "yes", "TRUE", "Yes"):
            with self.subTest(value=value):
                self.assertTrue(
                    self._supported({"TSTUDIO_SIXEL": value, "KITTY_WINDOW_ID": "3"})
                )
        # False even inside Windows Terminal, which otherwise reports True.
        for value in ("0", "false", "no", "FALSE", "No"):
            with self.subTest(value=value):
                self.assertFalse(
                    self._supported({"TSTUDIO_SIXEL": value, "WT_SESSION": "abc-123"})
                )

    def test_windows_terminal(self):
        self.assertTrue(self._supported({"WT_SESSION": "e1f2-dead-beef"}))

    def test_wezterm(self):
        self.assertTrue(self._supported({"TERM_PROGRAM": "WezTerm"}))

    def test_mlterm_via_term_program(self):
        self.assertTrue(self._supported({"TERM_PROGRAM": "mlterm"}))

    def test_foot(self):
        self.assertTrue(self._supported({"TERM": "foot-extra"}))

    def test_xterm_sixel(self):
        self.assertTrue(self._supported({"TERM": "xterm-sixel"}))

    def test_mlterm_via_term(self):
        self.assertTrue(self._supported({"TERM": "mlterm-256color"}))

    def test_kitty_is_false(self):
        self.assertFalse(
            self._supported({"KITTY_WINDOW_ID": "1", "TERM": "xterm-kitty"})
        )

    def test_ghostty_is_false(self):
        self.assertFalse(
            self._supported({"TERM_PROGRAM": "ghostty", "TERM": "xterm-ghostty"})
        )

    def test_unknown_terminal_is_false(self):
        self.assertFalse(self._supported({"TERM": "xterm-256color"}))

    def test_empty_environment_is_false(self):
        self.assertFalse(self._supported({}))

    def test_result_is_cached(self):
        sixel.sixel_supported.cache_clear()
        with mock.patch.object(sixel, "query_da1", return_value=None):
            with mock.patch.dict(sixel.os.environ, {"WT_SESSION": "x"}, clear=True):
                self.assertTrue(sixel.sixel_supported())
            # The environment is gone, but the cached answer stands.
            with mock.patch.dict(sixel.os.environ, {}, clear=True):
                self.assertTrue(sixel.sixel_supported())
                sixel.sixel_supported.cache_clear()
                self.assertFalse(sixel.sixel_supported())


class HintTests(unittest.TestCase):
    def test_hint_text_is_exact(self):
        self.assertEqual(
            sixel.SIXEL_HINT,
            "sixel is detected automatically when the terminal answers a DA1 "
            "query; set TSTUDIO_SIXEL=1 to force it on or 0 to force it off",
        )

    def test_hint_is_one_line(self):
        self.assertNotIn("\n", sixel.SIXEL_HINT)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.frame = Path(self._tmp.name) / "frame.png"
        self.frame.write_bytes(b"\x89PNG\r\n\x1a\nnot really a png")

    def test_argv_is_exact(self):
        self.assertEqual(
            sixel.sixel_command(self.frame, 800, 450),
            ["magick", str(self.frame), "-resize", "800x450",
             "-colors", "256", "sixel:-"],
        )

    def test_colors_are_passed_through(self):
        argv = sixel.sixel_command(self.frame, 600, 338, colors=64)
        self.assertEqual(argv[argv.index("-colors") + 1], "64")

    def test_missing_path_raises(self):
        with self.assertRaises(ClipError):
            sixel.sixel_command(Path(self._tmp.name) / "nope.png", 800, 450)

    def test_zero_width_raises(self):
        with self.assertRaises(ClipError):
            sixel.sixel_command(self.frame, 0, 450)

    def test_zero_height_raises(self):
        with self.assertRaises(ClipError):
            sixel.sixel_command(self.frame, 800, 0)

    def test_one_colour_raises(self):
        with self.assertRaises(ClipError):
            sixel.sixel_command(self.frame, 800, 450, colors=1)

    def test_too_many_colours_raises(self):
        with self.assertRaises(ClipError):
            sixel.sixel_command(self.frame, 800, 450, colors=300)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.frame = Path(self._tmp.name) / "frame.png"
        self.frame.write_bytes(b"\x89PNG\r\n\x1a\nnot really a png")

    def test_returns_the_sixel_bytes(self):
        payload = b"\x1bPq#0;2;100;0;0#0~~@@vv@@~~$-"
        fake = mock.Mock(returncode=0, stdout=payload, stderr=b"")
        with mock.patch.object(sixel.subprocess, "run", return_value=fake) as run:
            self.assertEqual(sixel.render_sixel(self.frame, 800, 450), payload)
        argv = run.call_args.args[0]
        self.assertEqual(argv, sixel.sixel_command(self.frame, 800, 450))
        self.assertEqual(run.call_args.kwargs.get("timeout"), 30)

    def test_failure_names_the_file_and_the_reason(self):
        fake = mock.Mock(
            returncode=1,
            stdout=b"",
            stderr=b"magick: noise\nmagick: no decode delegate for this image format",
        )
        with mock.patch.object(sixel.subprocess, "run", return_value=fake):
            with self.assertRaises(ClipError) as ctx:
                sixel.render_sixel(self.frame, 800, 450)
        message = str(ctx.exception)
        self.assertTrue(message.startswith("could not render "))
        self.assertIn("frame.png", message)
        self.assertIn("no decode delegate", message)

    def test_missing_imagemagick_is_reported(self):
        with mock.patch.object(
            sixel.subprocess, "run", side_effect=FileNotFoundError("magick")
        ):
            with self.assertRaises(ClipError) as ctx:
                sixel.render_sixel(self.frame, 800, 450)
        message = str(ctx.exception)
        self.assertTrue(message.startswith("could not render "))
        self.assertIn("ImageMagick", message)

    def test_timeout_is_reported(self):
        with mock.patch.object(
            sixel.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["magick"], timeout=30),
        ):
            with self.assertRaises(ClipError) as ctx:
                sixel.render_sixel(self.frame, 800, 450)
        self.assertTrue(str(ctx.exception).startswith("could not render "))

    def test_empty_output_is_an_error(self):
        fake = mock.Mock(returncode=0, stdout=b"", stderr=b"")
        with mock.patch.object(sixel.subprocess, "run", return_value=fake):
            with self.assertRaises(ClipError) as ctx:
                sixel.render_sixel(self.frame, 800, 450)
        self.assertTrue(str(ctx.exception).startswith("could not render "))


class _BufferedStream:
    """A text-ish stream that exposes a binary .buffer, like sys.stdout does."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1


class _HostileStream:
    """A stream that refuses bytes, to prove no TypeError escapes."""

    def write(self, data):
        raise TypeError("write() argument must be str, not bytes")


class WriteTests(unittest.TestCase):
    def test_writes_to_a_bytesio_directly(self):
        payload = b"\x1bPq#0;2;0;0;100#0~~$-"
        stream = io.BytesIO()
        sixel.write_sixel(payload, stream)
        self.assertEqual(stream.getvalue(), payload)

    def test_writes_through_a_buffer_attribute(self):
        payload = b"\x1bPq#0~~$-"
        stream = _BufferedStream()
        sixel.write_sixel(payload, stream)
        self.assertEqual(stream.buffer.getvalue(), payload)
        self.assertGreaterEqual(stream.flushes, 1)

    def test_flush_is_called_on_a_plain_stream(self):
        stream = io.BytesIO()
        with mock.patch.object(stream, "flush", wraps=stream.flush) as flush:
            sixel.write_sixel(b"\x1bPq$-", stream)
        flush.assert_called()

    def test_a_stream_that_refuses_bytes_raises_clip_error(self):
        with self.assertRaises(ClipError):
            sixel.write_sixel(b"\x1bPq$-", _HostileStream())


class CellPixelTests(unittest.TestCase):
    def test_default_cell_size(self):
        with mock.patch.dict(sixel.os.environ, {}, clear=True):
            self.assertEqual(sixel.cell_pixels(), (10, 20))

    def test_env_override(self):
        with mock.patch.dict(sixel.os.environ, {"TSTUDIO_CELL_PX": "8x16"}, clear=True):
            self.assertEqual(sixel.cell_pixels(), (8, 16))

    def test_garbage_falls_back(self):
        with mock.patch.dict(
            sixel.os.environ, {"TSTUDIO_CELL_PX": "garbage"}, clear=True
        ):
            self.assertEqual(sixel.cell_pixels(), (10, 20))

    def test_other_bad_values_fall_back(self):
        for value in ("", "8", "8x", "x16", "0x16", "8x0", "-8x16", "8x16x32"):
            with self.subTest(value=value):
                with mock.patch.dict(
                    sixel.os.environ, {"TSTUDIO_CELL_PX": value}, clear=True
                ):
                    self.assertEqual(sixel.cell_pixels(), (10, 20))


class SizeForTests(unittest.TestCase):
    def test_default_terminal_box(self):
        with mock.patch.dict(sixel.os.environ, {}, clear=True):
            self.assertEqual(sixel.sixel_size_for(80, 24), (800, 480))

    def test_uses_the_env_cell_size(self):
        with mock.patch.dict(sixel.os.environ, {"TSTUDIO_CELL_PX": "8x16"}, clear=True):
            self.assertEqual(sixel.sixel_size_for(80, 24), (640, 384))

    def test_zero_cols_raises(self):
        with self.assertRaises(ClipError):
            sixel.sixel_size_for(0, 24)

    def test_zero_rows_raises(self):
        with self.assertRaises(ClipError):
            sixel.sixel_size_for(80, 0)


@unittest.skipUnless(HAVE_MAGICK, "ImageMagick not installed")
class IntegrationTests(unittest.TestCase):
    def test_real_magick_produces_sixel_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            red = Path(d) / "red.png"
            proc = subprocess.run(
                ["magick", "-size", "8x8", "xc:red", str(red)],
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            data = sixel.render_sixel(red, 8, 8)
        self.assertTrue(data.startswith(b"\x1bP"))
        self.assertGreater(len(data), 10)


if __name__ == "__main__":
    unittest.main()
