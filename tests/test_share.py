"""Tests for tstudio.gallery.share - nothing here opens a browser or a window."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tstudio import ClipError
from tstudio.gallery import share


def _done(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout="", stderr=stderr
    )


def _argv(call) -> list[str]:
    args = call.args[0] if call.args else call.kwargs.get("args")
    return list(args)


class _TempFile:
    """A real file inside a temporary directory, removed on exit."""

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "clip.mp4"
        path.write_bytes(b"not really a video")
        return path

    def __exit__(self, *exc_info) -> None:
        self._tmp.cleanup()


class ShareTargetTests(unittest.TestCase):
    def test_the_five_targets_exist_with_the_expected_labels_and_presets(self):
        expected = {
            "youtube": ("YouTube Shorts", "https://www.youtube.com/upload", "shorts"),
            "instagram": ("Instagram Reels", "https://www.instagram.com/", "reels"),
            "tiktok": ("TikTok", "https://www.tiktok.com/upload", "tiktok"),
            "x": ("X (Twitter)", "https://x.com/compose/post", "x"),
            "file": ("Just the file", "", None),
        }
        self.assertEqual(set(share.SHARE_TARGETS), set(expected))
        for name, (label, url, preset) in expected.items():
            target = share.SHARE_TARGETS[name]
            self.assertEqual(target.name, name)
            self.assertEqual(target.label, label)
            self.assertEqual(target.upload_url, url)
            self.assertEqual(target.preset, preset)

    def test_every_upload_url_except_the_file_target_is_https(self):
        for name, target in share.SHARE_TARGETS.items():
            if name == "file":
                self.assertEqual(target.upload_url, "")
            else:
                self.assertTrue(
                    target.upload_url.startswith("https://"),
                    f"{name} upload_url is {target.upload_url!r}",
                )

    def test_every_target_has_a_note(self):
        for name, target in share.SHARE_TARGETS.items():
            self.assertTrue(target.note.strip(), f"{name} has no note")

    def test_presets_name_real_presets(self):
        from tstudio.presets import PRESETS

        for target in share.SHARE_TARGETS.values():
            if target.preset is not None:
                self.assertIn(target.preset, PRESETS)

    def test_targets_are_frozen(self):
        with self.assertRaises(Exception):
            share.SHARE_TARGETS["youtube"].label = "something else"  # type: ignore[misc]


class GetTargetTests(unittest.TestCase):
    def test_lookup_is_case_insensitive(self):
        lower = share.get_target("youtube")
        upper = share.get_target("YOUTUBE")
        self.assertIs(lower, upper)
        self.assertEqual(lower.name, "youtube")

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(share.get_target("  TikTok  ").name, "tiktok")

    def test_unknown_target_lists_the_valid_ones(self):
        with self.assertRaises(ClipError) as ctx:
            share.get_target("myspace")
        message = str(ctx.exception)
        self.assertIn("myspace", message)
        for name in ("youtube", "instagram", "tiktok", "x", "file"):
            self.assertIn(name, message)


class RevealTests(unittest.TestCase):
    def test_missing_path_raises_before_any_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "gone.mp4"
            with mock.patch.object(share.subprocess, "run") as run:
                with self.assertRaises(ClipError) as ctx:
                    share.reveal_in_file_manager(missing)
        run.assert_not_called()
        self.assertIn("gone.mp4", str(ctx.exception))

    def test_explorer_nonzero_exit_is_not_an_error(self):
        # explorer.exe returns a non-zero exit code even when it worked.
        with _TempFile() as path:
            with mock.patch.object(share.sys, "platform", "win32"):
                with mock.patch.object(
                    share.subprocess, "run", return_value=_done(returncode=1)
                ) as run:
                    share.reveal_in_file_manager(path)  # must not raise
        run.assert_called_once()

    def test_windows_argv_selects_the_file(self):
        with _TempFile() as path:
            with mock.patch.object(share.sys, "platform", "win32"):
                with mock.patch.object(
                    share.subprocess, "run", return_value=_done()
                ) as run:
                    share.reveal_in_file_manager(path)
        argv = _argv(run.call_args)
        self.assertEqual(argv, ["explorer", f"/select,{path}"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_macos_argv_reveals_the_file(self):
        with _TempFile() as path:
            with mock.patch.object(share.sys, "platform", "darwin"):
                with mock.patch.object(
                    share.subprocess, "run", return_value=_done()
                ) as run:
                    share.reveal_in_file_manager(path)
        self.assertEqual(_argv(run.call_args), ["open", "-R", str(path)])

    def test_linux_argv_opens_the_parent_directory(self):
        with _TempFile() as path:
            with mock.patch.object(share.sys, "platform", "linux"):
                with mock.patch.object(
                    share.subprocess, "run", return_value=_done()
                ) as run:
                    share.reveal_in_file_manager(path)
        self.assertEqual(_argv(run.call_args), ["xdg-open", str(path.parent)])

    def test_nonzero_exit_off_windows_is_an_error(self):
        with _TempFile() as path:
            with mock.patch.object(share.sys, "platform", "darwin"):
                with mock.patch.object(
                    share.subprocess,
                    "run",
                    return_value=_done(returncode=1, stderr="no such thing\n"),
                ):
                    with self.assertRaises(ClipError) as ctx:
                        share.reveal_in_file_manager(path)
        self.assertIn("no such thing", str(ctx.exception))

    def test_unsupported_platform_raises_clip_error(self):
        with _TempFile() as path:
            with mock.patch.object(share.sys, "platform", "sunos5"):
                with mock.patch.object(share.subprocess, "run") as run:
                    with self.assertRaises(ClipError) as ctx:
                        share.reveal_in_file_manager(path)
        run.assert_not_called()
        self.assertIn("sunos5", str(ctx.exception))

    def test_a_missing_file_manager_becomes_a_clip_error(self):
        with _TempFile() as path:
            with mock.patch.object(share.sys, "platform", "linux"):
                with mock.patch.object(
                    share.subprocess, "run", side_effect=FileNotFoundError()
                ):
                    with self.assertRaises(ClipError) as ctx:
                        share.reveal_in_file_manager(path)
        self.assertIn("xdg-open", str(ctx.exception))

    def test_a_timeout_becomes_a_clip_error(self):
        with _TempFile() as path:
            with mock.patch.object(share.sys, "platform", "win32"):
                with mock.patch.object(
                    share.subprocess,
                    "run",
                    side_effect=subprocess.TimeoutExpired(cmd="explorer", timeout=20),
                ):
                    with self.assertRaises(ClipError):
                        share.reveal_in_file_manager(path)


class OpenUploadPageTests(unittest.TestCase):
    def test_each_target_opens_its_own_url(self):
        for name in ("youtube", "instagram", "tiktok", "x"):
            target = share.get_target(name)
            with mock.patch.object(
                share.webbrowser, "open", return_value=True
            ) as opened:
                share.open_upload_page(target)
            opened.assert_called_once_with(target.upload_url)

    def test_the_file_target_has_no_upload_page(self):
        with mock.patch.object(share.webbrowser, "open") as opened:
            with self.assertRaises(ClipError) as ctx:
                share.open_upload_page(share.get_target("file"))
        opened.assert_not_called()
        self.assertIn("Just the file", str(ctx.exception))

    def test_a_browser_that_refuses_to_open_is_an_error(self):
        with mock.patch.object(share.webbrowser, "open", return_value=False):
            with self.assertRaises(ClipError) as ctx:
                share.open_upload_page(share.get_target("tiktok"))
        self.assertIn("https://www.tiktok.com/upload", str(ctx.exception))

    def test_an_os_error_from_the_browser_becomes_a_clip_error(self):
        with mock.patch.object(
            share.webbrowser, "open", side_effect=OSError("no display")
        ):
            with self.assertRaises(ClipError):
                share.open_upload_page(share.get_target("x"))


class SharePlanTests(unittest.TestCase):
    def test_youtube_plan_from_an_unexported_source(self):
        steps = share.share_plan(share.get_target("youtube"), False)
        self.assertEqual(
            steps,
            [
                "export for YouTube Shorts (1080x1920)",
                "copy the file to the clipboard",
                "open the upload page",
            ],
        )

    def test_an_already_exported_source_skips_the_export_step(self):
        steps = share.share_plan(share.get_target("youtube"), True)
        self.assertEqual(
            steps,
            ["copy the file to the clipboard", "open the upload page"],
        )
        self.assertFalse(any("export" in step for step in steps))

    def test_every_preset_target_offers_an_export_step_when_not_exported(self):
        for name in ("youtube", "instagram", "tiktok", "x"):
            target = share.get_target(name)
            steps = share.share_plan(target, False)
            self.assertTrue(steps[0].startswith(f"export for {target.label} ("))
            self.assertEqual(len(steps), 3)

    def test_the_file_target_never_exports(self):
        for exported in (False, True):
            steps = share.share_plan(share.get_target("file"), exported)
            self.assertFalse(any("export" in step for step in steps))
            self.assertEqual(
                steps,
                [
                    "copy the file to the clipboard",
                    "show the file in the file manager",
                ],
            )

    def test_share_plan_touches_nothing(self):
        with mock.patch.object(share.subprocess, "run") as run:
            with mock.patch.object(share.webbrowser, "open") as opened:
                for name in share.SHARE_TARGETS:
                    share.share_plan(share.get_target(name), False)
                    share.share_plan(share.get_target(name), True)
        run.assert_not_called()
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
