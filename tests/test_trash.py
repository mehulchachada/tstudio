"""Tests for tstudio.gallery.trash - Recycle Bin, never a permanent delete."""

from __future__ import annotations

import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tstudio import ClipError
from tstudio.gallery import trash


def _fake_api(rc: int = 0, aborted: bool = False):
    """A stand-in for SHFileOperationW that records the struct it is given."""
    recorded: dict = {}

    def fake(ptr):
        # ptr is a ctypes.byref CArgObject; its _obj is the live struct.
        op = ptr._obj
        if aborted:
            op.fAnyOperationsAborted = 1
        recorded["op"] = op
        return rc

    fake.recorded = recorded
    return fake


class SendToTrashTests(unittest.TestCase):
    def _call(self, path: Path, fake):
        with mock.patch.object(trash, "is_available", return_value=True), mock.patch.object(
            ctypes.windll.shell32, "SHFileOperationW", fake
        ) as api:
            trash.send_to_trash(path)
        return api

    def test_the_path_is_double_nul_terminated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.png"
            path.write_bytes(b"x")
            fake = _fake_api()
            self._call(path, fake)
        # Reading the LPCWSTR field gives a Python str truncated at the first
        # NUL, so check the raw memory to prove the double-NUL terminator.
        op = fake.recorded["op"]
        base = ctypes.addressof(op)
        # pFrom is the third field: HWND (8) + UINT (4) + 4 pad = offset 16.
        ptr_addr = ctypes.c_void_p.from_address(base + 16).value
        expected = (str(path.resolve()) + "\0\0").encode("utf-16-le")
        raw = ctypes.string_at(ptr_addr, len(expected))
        self.assertEqual(raw, expected)

    def test_allow_undo_is_always_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.png"
            path.write_bytes(b"x")
            fake = _fake_api()
            self._call(path, fake)
        self.assertTrue(fake.recorded["op"].fFlags & trash.FOF_ALLOWUNDO)

    def test_missing_path_raises_before_the_api(self):
        fake = _fake_api()
        with mock.patch.object(trash, "is_available", return_value=True), mock.patch.object(
            ctypes.windll.shell32, "SHFileOperationW", fake
        ):
            with self.assertRaises(ClipError) as ctx:
                trash.send_to_trash(Path("C:/nope/missing.png"))
        self.assertEqual(fake.recorded, {})
        self.assertIn("missing.png", str(ctx.exception))

    def test_nonzero_rc_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.png"
            path.write_bytes(b"x")
            fake = _fake_api(rc=12)
            with mock.patch.object(trash, "is_available", return_value=True), mock.patch.object(
                ctypes.windll.shell32, "SHFileOperationW", fake
            ):
                with self.assertRaises(ClipError) as ctx:
                    trash.send_to_trash(path)
        self.assertTrue(
            str(ctx.exception).startswith("could not move to the Recycle Bin:")
        )
        self.assertIn("12", str(ctx.exception))

    def test_aborted_operation_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clip.png"
            path.write_bytes(b"x")
            fake = _fake_api(aborted=True)
            with mock.patch.object(trash, "is_available", return_value=True), mock.patch.object(
                ctypes.windll.shell32, "SHFileOperationW", fake
            ):
                with self.assertRaises(ClipError) as ctx:
                    trash.send_to_trash(path)
        self.assertIn("cancelled", str(ctx.exception))


class SourceSafetyTests(unittest.TestCase):
    def test_module_never_calls_os_remove(self):
        source = Path(trash.__file__).read_text()
        for forbidden in ("os.remove", "os.unlink", "shutil.rmtree", ".unlink("):
            self.assertNotIn(forbidden, source)


@unittest.skipUnless(trash.is_available(), "Recycle Bin is Windows-only")
class RealDeletionTests(unittest.TestCase):
    def test_one_real_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vclip-trash-test.txt"
            path.write_text("safe to recycle\n")
            trash.send_to_trash(path)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
