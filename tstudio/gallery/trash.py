"""Move files to the Windows Recycle Bin via shell32.SHFileOperationW."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
from pathlib import Path

from tstudio import ClipError

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040  # this is the flag that means Recycle Bin instead of erase


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.wintypes.HWND),
        ("wFunc", ctypes.wintypes.UINT),
        ("pFrom", ctypes.wintypes.LPCWSTR),
        ("pTo", ctypes.wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", ctypes.wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.wintypes.LPCWSTR),
    ]


def is_available() -> bool:
    """True on Windows with shell32 loadable, else False."""
    if sys.platform != "win32":
        return False
    try:
        ctypes.windll.shell32
        return True
    except Exception:
        return False


def send_to_trash(path: Path) -> None:
    """Move a file to the Recycle Bin; it stays recoverable there."""
    if not is_available():
        raise ClipError(
            "the Recycle Bin is only available on Windows; refusing to delete "
            f"{path} any other way"
        )
    if not path.is_file():
        raise ClipError(f"cannot move to the Recycle Bin: {path} does not exist")

    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = str(path.resolve()) + "\0\0"
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT

    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        raise ClipError(f"could not move to the Recycle Bin: {path} (code {rc})")
    if op.fAnyOperationsAborted:
        raise ClipError(
            f"could not move to the Recycle Bin: the deletion of {path} was cancelled"
        )
