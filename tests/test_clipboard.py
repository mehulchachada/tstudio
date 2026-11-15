"""Tests for vidclip.gallery.clipboard - nothing here may touch the real clipboard."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vidclip import ClipError
from vidclip.gallery import clipboard


def _ok(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout="", stderr=stderr
    )


def _argv(call) -> list[str]:
    args = call.args[0] if call.args else call.kwargs.get("args")
    return list(args)


def _env(call) -> dict:
    return call.kwargs["env"]


class CopyFileTests(unittest.TestCase):
    def test_command_shape_and_path_only_in_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.png"
            path.write_bytes(b"x")
            with mock.patch.object(
                clipboard.subprocess, "run", return_value=_ok()
            ) as run:
                clipboard.copy_file(path)
        argv = _argv(run.call_args)
        self.assertEqual(argv[0], "powershell.exe")
        self.assertIn("-NoProfile", argv)
        self.assertIn("-STA", argv)
        self.assertIn("SetFileDropList", " ".join(argv))
        self.assertEqual(_env(run.call_args)["VCLIP_TARGET"], str(path))
        for element in argv:
            self.assertNotIn(str(path), element)

    def test_an_apostrophe_in_the_name_is_not_interpolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Assassin's Creed clip.png"
            path.write_bytes(b"x")
            with mock.patch.object(
                clipboard.subprocess, "run", return_value=_ok()
            ) as run:
                clipboard.copy_file(path)
        argv = _argv(run.call_args)
        self.assertEqual(_env(run.call_args)["VCLIP_TARGET"], str(path))
        for element in argv:
            self.assertNotIn(str(path), element)
            self.assertNotIn("Assassin's Creed clip.png", element)

    def test_missing_path_raises_before_any_subprocess(self):
        with mock.patch.object(clipboard.subprocess, "run") as run:
            with self.assertRaises(ClipError) as ctx:
                clipboard.copy_file(Path("C:/nope/missing.png"))
        run.assert_not_called()
        self.assertIn("missing.png", str(ctx.exception))

    def test_nonzero_exit_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.png"
            path.write_bytes(b"x")
            with mock.patch.object(
                clipboard.subprocess,
                "run",
                return_value=_ok(returncode=1, stderr="boom\n"),
            ):
                with self.assertRaises(ClipError) as ctx:
                    clipboard.copy_file(path)
        self.assertTrue(
            str(ctx.exception).startswith("could not copy to the clipboard:")
        )

    def test_timeout_becomes_clip_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.png"
            path.write_bytes(b"x")
            with mock.patch.object(
                clipboard.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(
                    cmd="powershell.exe", timeout=20
                ),
            ):
                with self.assertRaises(ClipError):
                    clipboard.copy_file(path)


class CopyImageTests(unittest.TestCase):
    def test_command_shape_and_path_only_in_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "frame.png"
            path.write_bytes(b"x")
            with mock.patch.object(
                clipboard.subprocess, "run", return_value=_ok()
            ) as run:
                clipboard.copy_image(path)
        argv = _argv(run.call_args)
        self.assertIn("SetImage", " ".join(argv))
        self.assertEqual(_env(run.call_args)["VCLIP_TARGET"], str(path))
        for element in argv:
            self.assertNotIn(str(path), element)

    def test_non_image_suffix_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.mp4"
            path.write_bytes(b"x")
            with mock.patch.object(clipboard.subprocess, "run") as run:
                with self.assertRaises(ClipError) as ctx:
                    clipboard.copy_image(path)
        run.assert_not_called()
        self.assertIn("copy_file", str(ctx.exception))


class CopyTextTests(unittest.TestCase):
    def test_command_shape_and_text_only_in_env(self):
        with mock.patch.object(
            clipboard.subprocess, "run", return_value=_ok()
        ) as run:
            clipboard.copy_text("hello world")
        argv = _argv(run.call_args)
        joined = " ".join(argv)
        self.assertIn("Set-Clipboard", joined)
        self.assertIn("-Value", joined)
        self.assertEqual(_env(run.call_args)["VCLIP_TARGET"], "hello world")
        for element in argv:
            self.assertNotIn("hello world", element)


class CommandSafetyTests(unittest.TestCase):
    def test_set_clipboard_path_is_never_used(self):
        for script in (
            clipboard._COPY_FILE_SCRIPT,
            clipboard._COPY_IMAGE_SCRIPT,
            clipboard._COPY_TEXT_SCRIPT,
        ):
            self.assertNotIn("-Path", script)


if __name__ == "__main__":
    unittest.main()
