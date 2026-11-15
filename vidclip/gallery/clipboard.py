"""Copy files, images and text to the Windows clipboard via PowerShell."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from vidclip import ClipError
from vidclip.gallery.entries import IMAGE_SUFFIXES

_TIMEOUT = 20

_COPY_FILE_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms\n"
    "$c = New-Object System.Collections.Specialized.StringCollection\n"
    "$c.Add($env:VCLIP_TARGET) | Out-Null\n"
    "[System.Windows.Forms.Clipboard]::SetFileDropList($c)"
)

_COPY_IMAGE_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms,System.Drawing\n"
    "$i = [System.Drawing.Image]::FromFile($env:VCLIP_TARGET)\n"
    "[System.Windows.Forms.Clipboard]::SetImage($i)\n"
    "$i.Dispose()"
)

_COPY_TEXT_SCRIPT = "Set-Clipboard -Value $env:VCLIP_TARGET"


def _run(script: str, payload: str) -> None:
    """Run a PowerShell clipboard script with the payload in VCLIP_TARGET."""
    env = {**os.environ, "VCLIP_TARGET": payload}
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClipError(
            f"could not copy to the clipboard: PowerShell timed out after {_TIMEOUT}s"
        ) from exc
    if result.returncode != 0:
        raise ClipError(_failure_message(result.stderr))
    if result.stderr.strip():
        raise ClipError(_failure_message(result.stderr))


def _failure_message(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    last = lines[-1] if lines else "unknown error"
    return f"could not copy to the clipboard: {last}"


def _require_file(path: Path) -> None:
    if not path.is_file():
        raise ClipError(f"cannot copy {path}: the file does not exist")


def copy_file(path: Path) -> None:
    """Put the file itself on the clipboard as a file drop list."""
    _require_file(path)
    _run(_COPY_FILE_SCRIPT, str(path))


def copy_image(path: Path) -> None:
    """Put the picture itself on the clipboard as a bitmap."""
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ClipError(
            f"{path.suffix} is not an image; use copy_file for {path.name}"
        )
    _require_file(path)
    _run(_COPY_IMAGE_SCRIPT, str(path))


def copy_text(text: str) -> None:
    """Put plain text on the clipboard."""
    _run(_COPY_TEXT_SCRIPT, text)
