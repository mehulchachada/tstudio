"""Play a clip: mpv in the terminal when it is there, stepped still frames when not.

mpv is detected at runtime and never installed for the user - when it is missing
the gallery falls back to scrubbing, which steps through still frames rendered by
``tstudio.gallery.thumbs``.
"""

from __future__ import annotations

import sys

import os

import math
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from tstudio import ClipError
from tstudio.gallery.thumbs import art_for
from tstudio.media import probe

MPV_HINT: str = "install mpv for in-terminal playback: winget install shinchiro.mpv"


@lru_cache(maxsize=1)
def mpv_available() -> bool:
    """True when mpv is on PATH, cached for the life of the process.

    Call ``mpv_available.cache_clear()`` if mpv may have been installed since.
    """
    return shutil.which("mpv") is not None


def _require_file(path: Path) -> None:
    """Raise ClipError unless ``path`` is a file we can actually hand to mpv."""
    try:
        exists = path.exists()
    except OSError as exc:
        raise ClipError(f"cannot read {path.name}: {exc}") from exc
    if not exists:
        raise ClipError(f"there is nothing to play at {path} - the file is missing")


def mpv_command(path: Path) -> list[str]:
    """The argv that plays ``path`` as truecolor text in this terminal.

    Pure: it builds the list and runs nothing. ``--vo=tct`` is the video output
    that draws into the terminal itself. Raises ClipError if the file is missing.
    """
    _require_file(path)
    return ["mpv", "--vo=tct", "--really-quiet", str(path)]


def play_with_mpv(path: Path) -> int:
    """Play ``path`` in the terminal with mpv and return mpv's exit code.

    The CALLER must suspend the Textual app around this call::

        with app.suspend():
            code = play_with_mpv(path)

    mpv takes over the terminal completely with ``--vo=tct`` - it writes frames
    to stdout and reads keys from stdin - so without ``app.suspend()`` mpv and
    Textual fight over the same screen and both end up corrupted. This module
    deliberately does not import textual; suspending is the UI layer's job.

    Raises ClipError carrying MPV_HINT when mpv is not installed, and ClipError
    when the file is missing or mpv cannot be started.
    """
    if not mpv_available():
        raise ClipError(f"mpv is not installed - {MPV_HINT}")
    cmd = mpv_command(path)
    try:
        proc = subprocess.run(cmd)
    except OSError as exc:
        raise ClipError(f"could not start mpv: {exc}") from exc
    return proc.returncode


def scrub_timestamps(duration: float, steps: int = 10) -> list[float]:
    """``steps`` timestamps evenly spaced from 0 up to - never reaching - the end.

    For a 10 second clip and 10 steps that is [0.0, 1.0, ... 9.0]. The last
    timestamp stays strictly inside the clip because seeking to exactly the end
    yields no frame at all. A duration of 0 or less gives [0.0].
    """
    if steps < 1:
        raise ClipError(f"a clip needs at least one frame to scrub, not {steps}")
    if duration <= 0:
        return [0.0]
    times: list[float] = []
    for i in range(steps):
        t = duration * i / steps
        if t >= duration:  # only reachable through floating-point rounding
            t = math.nextafter(duration, 0.0)
        times.append(t)
    return times


def scrub_frame(
    path: Path,
    index: int,
    cols: int,
    rows: int,
    *,
    steps: int = 10,
) -> list[str]:
    """One still frame of ``path`` as ANSI rows: scrub position ``index`` of ``steps``.

    The index wraps, so -1 is the last position and ``steps`` is back to the
    start. BLOCKING - it shells out to ffmpeg and ImageMagick and takes roughly
    0.5-2.5s, so a UI must run it in a worker thread, never on the UI thread.

    Raises ClipError if the file is missing, unreadable, or has no frame there.
    """
    _require_file(path)
    times = scrub_timestamps(probe(path).duration, steps)
    # len(times) is steps except for a zero-length clip, where it is 1.
    at = times[index % len(times)]
    return art_for(path, cols, rows, at=at)


def player_command(path: Path, *, in_terminal: bool = False) -> list[str]:
    """argv for watching `path` in mpv.

    `in_terminal=True` draws the video as text in the terminal (--vo=tct),
    which is a neat trick for a peek at the source. For reviewing a finished
    clip you want the real thing, so the default opens mpv's own window.
    """
    _require_file(path)
    if in_terminal:
        return ["mpv", "--vo=tct", "--really-quiet", str(path)]
    return ["mpv", "--really-quiet", str(path)]


def open_in_player(path: Path) -> str:
    """Open the clip in mpv, or hand it to whatever the OS plays video with.

    Returns the name of what was used, so the caller can say so. Raises
    ClipError only when nothing at all could open it - a missing mpv is not
    an error, it is a fallback.
    """
    _require_file(path)
    if mpv_available():
        try:
            subprocess.Popen(player_command(path),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "mpv"
        except OSError:
            pass  # fall through to the system player
    if sys.platform == "win32":
        try:
            os.startfile(str(path))  # noqa: S606 - the OS picks the handler
            return "the system player"
        except OSError as exc:
            raise ClipError(f"could not open {path.name}: {exc}") from exc
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "the system player"
    except OSError as exc:
        raise ClipError(f"could not open {path.name}: {exc}") from exc
