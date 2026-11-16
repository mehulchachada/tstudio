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


# Installers that add no PATH entry of their own. Checked after PATH itself:
# winget's shinchiro.mpv lands in "C:\Program Files\MPV Player" and adds
# nothing to PATH, so shutil.which() alone misses a perfectly good mpv.
_MPV_FALLBACK_PATHS = (
    r"C:\Program Files\MPV Player\mpv.exe",
    r"C:\Program Files\mpv\mpv.exe",
    r"C:\Program Files (x86)\MPV Player\mpv.exe",
    "/opt/homebrew/bin/mpv",
    "/usr/local/bin/mpv",
    "/usr/bin/mpv",
)


@lru_cache(maxsize=1)
def mpv_path() -> str | None:
    """Where mpv is, or None. PATH first, then the known install locations."""
    found = shutil.which("mpv")
    if found:
        return found
    for candidate in _MPV_FALLBACK_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


def mpv_available() -> bool:
    """True when mpv can be found, on PATH or where installers leave it.

    Call ``mpv_path.cache_clear()`` if mpv may have been installed since.
    """
    return mpv_path() is not None


def _require_file(path: Path) -> None:
    """Raise ClipError unless ``path`` is a file we can actually hand to mpv."""
    try:
        exists = path.exists()
    except OSError as exc:
        raise ClipError(f"cannot read {path.name}: {exc}") from exc
    if not exists:
        raise ClipError(f"there is nothing to play at {path} - the file is missing")


#: Force or forbid drawing video in the terminal: "sixel", "tct", or a false
#: value to always use a window. Unset means ask the terminal (see terminal_vo).
TERMINAL_PLAYBACK_ENV: str = "TSTUDIO_TERMINAL_PLAYBACK"

_OFF_VALUES = frozenset({"0", "no", "off", "false", "none", "window"})


def terminal_vo() -> str | None:
    """Which mpv video output can actually draw in THIS terminal, or None.

    Only ``sixel`` is ever chosen on its own, because sixel is the only one
    that can be *asked* about: DA1 attribute 4 is an answer from the terminal
    itself, not a guess.

    ``tct`` is a guess, and the guess is wrong in at least one terminal that
    people use daily. In the Claude desktop terminal - which answers DA1 with
    no attribute 4, so it gets no sixel either - ``mpv --vo=tct`` writes
    nothing at all. Measured twice there, the second time with
    ``--vo-tct-width/height`` forced, in case size detection was the problem:
    zero bytes both times. The clip then plays its audio over a blank screen,
    and ``--really-quiet`` swallows whatever mpv might have said about it.

    Silent audio-only playback is a worse answer than a window, so when the
    terminal does not claim sixel this returns None and the caller opens mpv's
    own window instead.

    ``TSTUDIO_TERMINAL_PLAYBACK=tct`` forces tct where it is known to work;
    ``=sixel`` forces sixel; a false value never draws in the terminal.
    """
    forced = os.environ.get(TERMINAL_PLAYBACK_ENV, "").strip().lower()
    if forced in ("sixel", "tct"):
        return forced
    if forced in _OFF_VALUES:
        return None
    from tstudio.gallery.sixel import sixel_supported
    return "sixel" if sixel_supported() else None


def plays_in_terminal() -> bool:
    """True when a clip will be drawn in the terminal rather than in a window."""
    return terminal_vo() is not None


def mpv_command(path: Path) -> list[str]:
    """The argv that plays ``path`` where it will actually be seen.

    Inside the terminal when the terminal can draw it, in mpv's own window
    when it cannot - never a terminal video output that would play the sound
    and show nothing. Pure apart from asking the terminal what it supports.
    Raises ClipError if the file is missing.
    """
    _require_file(path)
    mpv = mpv_path() or "mpv"
    vo = terminal_vo()
    if vo is None:
        return [mpv, "--really-quiet", str(path)]
    return [mpv, f"--vo={vo}", "--really-quiet", str(path)]


def play_with_mpv(path: Path) -> int:
    """Play ``path`` with mpv and return mpv's exit code.

    Where the terminal can draw video it plays there; where it cannot, mpv's
    own window opens instead. ``plays_in_terminal()`` says which, so a UI can
    tell the user where to look.

    The CALLER must suspend the Textual app around this call::

        with app.suspend():
            code = play_with_mpv(path)

    With a terminal video output mpv takes over the terminal completely - it
    writes frames to stdout and reads keys from stdin - so without
    ``app.suspend()`` mpv and Textual fight over the same screen and both end
    up corrupted. Suspending is harmless in the windowed case, where mpv still
    reads keys from the terminal it was started in. This module deliberately
    does not import textual; suspending is the UI layer's job.

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

    `in_terminal=True` asks for the video inside the terminal, which is a neat
    trick for a peek at the source - but only where the terminal has sixel,
    since that is the only one that can be verified; elsewhere it falls back
    to a window rather than playing the sound over a blank screen. For
    reviewing a finished clip you want the real thing anyway, so the default
    opens mpv's own window.
    """
    _require_file(path)
    if in_terminal:
        return mpv_command(path)
    return [mpv_path() or "mpv", "--really-quiet", str(path)]


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
