"""Share a clip by handing it off: clipboard, file manager, or upload page.

There is no upload here on purpose. YouTube, Instagram, TikTok and X all
require a registered developer app and an OAuth round-trip before a single
byte can be posted, and a share button that quietly does nothing is worse
than no share button at all. So "share" means: get the file ready, then put
it somewhere the user can finish the job in two clicks - on the clipboard,
selected in the file manager, or with the platform's own upload page open in
the browser, ready to drop the file onto.
"""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from tstudio import ClipError
from tstudio.presets import get_preset

_TIMEOUT = 20


@dataclass(frozen=True)
class ShareTarget:
    name: str
    label: str          # "YouTube Shorts"
    upload_url: str     # "" when there is no web upload page
    preset: str | None  # a name from tstudio.presets.PRESETS, or None
    note: str           # one short, honest line shown to the user


# Honest notes: what the platform actually lets a desktop user do in
# November 2026. None of these are upload APIs, they are web pages.
SHARE_TARGETS: dict[str, ShareTarget] = {
    "youtube": ShareTarget(
        "youtube", "YouTube Shorts", "https://www.youtube.com/upload", "shorts",
        "The upload page opens in the browser; drop the file on it and pick "
        "Shorts.",
    ),
    "instagram": ShareTarget(
        "instagram", "Instagram Reels", "https://www.instagram.com/", "reels",
        "Instagram has no desktop upload for Reels; open the app or use the "
        "web composer.",
    ),
    "tiktok": ShareTarget(
        "tiktok", "TikTok", "https://www.tiktok.com/upload", "tiktok",
        "The web uploader works, but it needs you signed in on that browser.",
    ),
    "x": ShareTarget(
        "x", "X (Twitter)", "https://x.com/compose/post", "x",
        "The composer opens empty; attach the file yourself, it cannot be "
        "pre-filled.",
    ),
    "file": ShareTarget(
        "file", "Just the file", "", None,
        "No upload page - the file goes on the clipboard and is shown in the "
        "file manager.",
    ),
}


def get_target(name: str) -> ShareTarget:
    """Resolve a share target name, ignoring case; ClipError if unknown."""
    key = name.strip().lower()
    if key in SHARE_TARGETS:
        return SHARE_TARGETS[key]
    raise ClipError(
        f"unknown share target {name!r}; valid targets: "
        + ", ".join(SHARE_TARGETS)
    )


def _reveal_command(path: Path) -> list[str]:
    """The file-manager command for this platform; ClipError if there is none."""
    target = str(path if path.is_absolute() else path.resolve())
    if sys.platform == "win32":
        # explorer wants the path glued to the switch: /select,C:\dir\clip.mp4
        return ["explorer", f"/select,{target}"]
    if sys.platform == "darwin":
        return ["open", "-R", target]
    if sys.platform.startswith("linux"):
        return ["xdg-open", str(path.parent)]
    raise ClipError(
        f"showing a file in the file manager is not supported on "
        f"{sys.platform}; the file is at {path}"
    )


def reveal_in_file_manager(path: Path) -> None:
    """Open the OS file manager with this file selected."""
    if not path.is_file():
        raise ClipError(f"cannot show {path} in the file manager: it does not exist")

    command = _reveal_command(path)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise ClipError(
            f"could not show {path.name} in the file manager: "
            f"{command[0]} was not found on this system"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClipError(
            f"could not show {path.name} in the file manager: "
            f"{command[0]} timed out after {_TIMEOUT}s"
        ) from exc
    except OSError as exc:
        raise ClipError(
            f"could not show {path.name} in the file manager: {exc}"
        ) from exc

    if sys.platform == "win32":
        # Documented quirk: explorer.exe returns 1 even when it worked, so a
        # non-zero exit code here means nothing and must not become an error.
        return
    if result.returncode != 0:
        detail = _last_line(result.stderr) or f"exit code {result.returncode}"
        raise ClipError(
            f"could not show {path.name} in the file manager: {detail}"
        )


def _last_line(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def open_upload_page(target: ShareTarget) -> None:
    """Open the target's upload page in the default browser."""
    if not target.upload_url:
        raise ClipError(
            f"{target.label} has no upload page to open; copy the file and "
            "upload it yourself"
        )
    try:
        opened = webbrowser.open(target.upload_url)
    except OSError as exc:
        raise ClipError(
            f"could not open the {target.label} upload page: {exc}"
        ) from exc
    if not opened:
        raise ClipError(
            f"could not open a browser for the {target.label} upload page; "
            f"the address is {target.upload_url}"
        )


def share_plan(target: ShareTarget, source_exported: bool) -> list[str]:
    """The ordered steps the UI shows for this target. Pure, no side effects."""
    steps: list[str] = []
    if target.preset is not None and not source_exported:
        preset = get_preset(target.preset)
        steps.append(
            f"export for {target.label} ({preset.width}x{preset.height})"
        )
    steps.append("copy the file to the clipboard")
    if target.upload_url:
        steps.append("open the upload page")
    else:
        steps.append("show the file in the file manager")
    return steps
