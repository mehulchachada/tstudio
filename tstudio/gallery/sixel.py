"""True pixel graphics in the terminal via the DEC SIXEL protocol.

The gallery's default renderer draws images as half-block coloured text, which
works in any terminal. Sixel is the sharper option where it is available: real
pixels, one byte stream handed straight to the terminal. It is also heavy - a
4K frame resized to 600x338 is roughly 373 KB of sixel bytes - so callers
should size the image to the cells they actually mean to fill.

ImageMagick 7 (``magick``, with ``SIXEL rw-`` in ``magick -list format``) does
the encoding; ``-colors 256`` keeps the palette small enough to matter.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from tstudio import ClipError

SIXEL_HINT: str = (
    "sixel is detected automatically when the terminal answers a DA1 query; "
    "set TSTUDIO_SIXEL=1 to force it on or 0 to force it off"
)

_TRUE_VALUES = frozenset({"1", "true", "yes"})
_FALSE_VALUES = frozenset({"0", "false", "no"})

#: TERM_PROGRAM values that mean sixel works.
_SIXEL_TERM_PROGRAMS = frozenset({"WezTerm", "mlterm"})

#: The Primary Device Attributes request: ESC [ c, i.e. b"\x1b[c".
DA1_QUERY: str = "\x1b[c"

#: A DA1 reply: CSI ? then a ';'-separated integer list, terminated by 'c'.
_DA1_REPLY_RE = re.compile(r"\x1b\[\?([0-9;]*)c")

#: The DA1 attribute that means "this terminal does sixel graphics".
DA1_SIXEL_ATTRIBUTE: int = 4

#: True on Windows. A module-level flag so tests can exercise either branch.
_IS_WINDOWS: bool = os.name == "nt"

#: How long to sleep between msvcrt polls on Windows.
_POLL_SECONDS = 0.002

#: Environment variables ``detection_report`` prints.
_REPORT_ENV_VARS: tuple[str, ...] = (
    "TSTUDIO_SIXEL",
    "WT_SESSION",
    "TERM_PROGRAM",
    "TERM",
    "MSYSTEM",
    "KITTY_WINDOW_ID",
)

#: Pixel size of one character cell when the terminal does not tell us.
DEFAULT_CELL_PX: tuple[int, int] = (10, 20)

#: The bytes a sixel stream starts with: DCS followed by 'q'.
SIXEL_INTRODUCER: bytes = b"\x1bP"

_TIMEOUT_SECONDS = 30


def _isatty(stream: object) -> bool:
    """Whether ``stream`` is a terminal. False for streams without ``isatty``."""
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except Exception:
        return False


def _write_da1_query() -> bool:
    """Send the DA1 request on stdout. False if the write did not go through."""
    try:
        sys.stdout.write(DA1_QUERY)
        sys.stdout.flush()
    except Exception:
        return False
    return True


def _parse_da1_reply(text: str) -> list[int] | None:
    """The attribute list inside ``text``, or None if there is no DA1 reply.

    Searches rather than matches, so stray bytes ahead of the CSI - a stale
    keypress, a half-read escape - do not spoil an otherwise good answer.
    """
    match = _DA1_REPLY_RE.search(text)
    if match is None:
        return None
    attrs = [part for part in match.group(1).split(";") if part.strip()]
    if not attrs:
        return None
    try:
        return [int(part) for part in attrs]
    except ValueError:  # pragma: no cover - the regex admits digits only
        return None


def _query_da1_posix(timeout: float) -> list[int] | None:
    """DA1 round trip using termios/tty for raw mode and select for the wait."""
    import select
    import termios
    import tty

    try:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except Exception:
        return None

    try:
        tty.setraw(fd)
        if not _write_da1_query():
            return None
        deadline = time.monotonic() + timeout
        collected: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            chunk = os.read(fd, 64)
            if not chunk:
                break
            collected.append(chunk.decode("ascii", "replace"))
            if "c" in collected[-1]:
                break
        return _parse_da1_reply("".join(collected))
    except Exception:
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except Exception:
            pass


def _query_da1_windows(timeout: float) -> list[int] | None:
    """DA1 round trip using msvcrt, which reads keys without echoing them."""
    import msvcrt

    try:
        if not _write_da1_query():
            return None
        deadline = time.monotonic() + timeout
        collected: list[str] = []
        while time.monotonic() < deadline:
            if not msvcrt.kbhit():
                time.sleep(_POLL_SECONDS)
                continue
            char = msvcrt.getwch()
            if not char:
                break
            collected.append(char)
            if char == "c":
                break
        return _parse_da1_reply("".join(collected))
    except Exception:
        return None


def query_da1(timeout: float = 0.25) -> list[int] | None:
    """Ask the terminal what it is, the way yazi does, and return its answer.

    Writes a Primary Device Attributes request (``ESC [ c``) and reads the
    CSI reply - ``ESC [ ? 62 ; 1 ; 2 ; 4 ; 6 c`` and the like - back off
    stdin, returning the integers as a list with the leading ``?`` dropped.
    Attribute 4 in that list means sixel graphics.

    Returns None when there is no usable answer: either stream is not a tty,
    nothing readable arrived before ``timeout``, or the reply did not parse.
    Never hangs, never raises, and always puts the terminal back the way it
    was found.
    """
    if not _isatty(sys.stdin) or not _isatty(sys.stdout):
        return None
    if _IS_WINDOWS:
        return _query_da1_windows(timeout)
    return _query_da1_posix(timeout)


@functools.lru_cache(maxsize=1)
def sixel_supported() -> bool:
    """Whether this terminal can display sixel.

    Asked, not guessed, where possible. ``TSTUDIO_SIXEL`` wins outright; then
    a DA1 query - if the terminal answers, attribute 4 in its reply is the
    authoritative yes or no and no environment variable overrides it. Only a
    terminal that will not answer (a pipe, a test runner, an older emulator)
    falls back to the environment-variable rules.

    Known-good by environment: Windows Terminal 1.22+, WezTerm, foot, mlterm,
    xterm built with sixel, and mintty - which is what Git Bash runs in, and
    which advertises nothing but ``TERM=xterm-256color`` and ``MSYSTEM``.
    kitty and Ghostty use their own graphics protocol and report False.

    The result is cached; call ``sixel_supported.cache_clear()`` after
    changing the environment.
    """
    env = os.environ

    override = env.get("TSTUDIO_SIXEL", "").strip().lower()
    if override in _TRUE_VALUES:
        return True
    if override in _FALSE_VALUES:
        return False

    attrs = query_da1()
    if attrs is not None:
        return DA1_SIXEL_ATTRIBUTE in attrs

    if env.get("WT_SESSION"):
        return True

    term_program = env.get("TERM_PROGRAM", "")
    if term_program in _SIXEL_TERM_PROGRAMS:
        return True

    term = env.get("TERM", "")
    if "foot" in term or term == "xterm-sixel" or "mlterm" in term:
        return True

    # Git Bash in mintty: sixel works, but nothing says so except MSYSTEM.
    if term_program == "mintty" or (env.get("MSYSTEM") and "xterm" in term):
        return True

    if env.get("KITTY_WINDOW_ID") or term_program == "ghostty":
        return False

    return False


def detection_report() -> str:
    """A short diagnostic explaining why sixel is, or is not, in use.

    Built for a human staring at block art and wondering why. Returns the
    text; printing it is the caller's business.
    """
    attrs = query_da1()
    if attrs is None:
        reply = "no answer (not a tty, timed out, or unparseable)"
        sixel_attr = "unknown"
    else:
        reply = "[" + ", ".join(str(value) for value in attrs) + "]"
        sixel_attr = "yes" if DA1_SIXEL_ATTRIBUTE in attrs else "no"

    lines = [
        "sixel detection",
        f"  stdin is a tty:  {_isatty(sys.stdin)}",
        f"  stdout is a tty: {_isatty(sys.stdout)}",
        f"  DA1 reply:       {reply}",
        f"  attribute 4 (sixel): {sixel_attr}",
        "  environment:",
    ]
    for name in _REPORT_ENV_VARS:
        value = os.environ.get(name)
        lines.append(f"    {name}={value if value is not None else '(unset)'}")
    lines.append(f"  verdict: sixel_supported() = {sixel_supported()}")
    return "\n".join(lines)


def sixel_command(path: Path, width: int, height: int, *, colors: int = 256) -> list[str]:
    """The ImageMagick argv that writes ``path`` as sixel bytes on stdout.

    Pure: builds the list and validates the arguments, runs nothing. Never
    ``shell=True`` anywhere - the caller passes this list to subprocess.
    """
    if not isinstance(path, Path):
        path = Path(path)
    if not path.exists():
        raise ClipError(f"could not render {path.name} - the file does not exist")
    if width < 1 or height < 1:
        raise ClipError(
            f"sixel size must be at least 1x1 pixel, not {width}x{height}"
        )
    if colors < 2 or colors > 256:
        raise ClipError(f"sixel needs between 2 and 256 colours, not {colors}")
    return [
        "magick",
        str(path),
        "-resize",
        f"{width}x{height}",
        "-colors",
        str(colors),
        "sixel:-",
    ]


def render_sixel(path: Path, width: int, height: int, *, colors: int = 256) -> bytes:
    """``path`` encoded as raw sixel bytes, resized to fit ``width`` x ``height``.

    The result is large - hundreds of kilobytes for a big frame - so ask for
    the pixel size you will actually draw. Raises ClipError when ImageMagick
    is missing, fails, takes longer than 30 seconds, or writes nothing.
    """
    cmd = sixel_command(path, width, height, colors=colors)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise ClipError(
            f"could not render {path.name} - ImageMagick (magick) is not installed "
            "or not on PATH"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClipError(
            f"could not render {path.name} - ImageMagick took longer than "
            f"{_TIMEOUT_SECONDS} seconds"
        ) from exc
    except OSError as exc:
        raise ClipError(f"could not render {path.name} - {exc}") from exc

    if proc.returncode != 0:
        detail = _last_stderr_line(proc.stderr)
        raise ClipError(
            f"could not render {path.name} as sixel{': ' + detail if detail else ''}"
        )
    if not proc.stdout:
        raise ClipError(
            f"could not render {path.name} - ImageMagick produced no sixel output"
        )
    return proc.stdout


def _last_stderr_line(stderr: bytes | None) -> str:
    """The last non-blank line of ImageMagick's stderr, decoded safely."""
    if not stderr:
        return ""
    text = stderr.decode("utf-8", "replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def write_sixel(data: bytes, stream=None) -> None:
    """Write sixel bytes to ``stream`` (default ``sys.stdout.buffer``) and flush.

    This is the one function in tstudio allowed to write to a stream itself:
    sixel is a byte protocol the terminal reads directly, so there is nothing
    to return and nothing to format. Everything else returns its output.

    Streams without a ``buffer`` attribute - a BytesIO, a test double, an
    already-binary wrapper - are written to directly. A stream that will not
    take bytes raises ClipError rather than letting a TypeError escape.
    """
    if stream is None:
        stream = getattr(sys.stdout, "buffer", sys.stdout)
    target = getattr(stream, "buffer", stream)
    try:
        target.write(data)
    except (TypeError, ValueError, AttributeError, OSError) as exc:
        raise ClipError(f"could not write sixel data to the terminal: {exc}") from exc
    seen: list[object] = []
    for candidate in (target, stream):
        if any(candidate is other for other in seen):
            continue
        seen.append(candidate)
        flush = getattr(candidate, "flush", None)
        if callable(flush):
            try:
                flush()
            except (ValueError, OSError):
                pass


def cell_pixels() -> tuple[int, int]:
    """The pixel size of one character cell, as (width, height).

    Used to turn a size in cells into a size in pixels. ``TSTUDIO_CELL_PX``
    overrides it as ``"WxH"``, for example ``8x16``; anything unparseable
    falls back to the common default rather than raising.
    """
    raw = os.environ.get("TSTUDIO_CELL_PX", "").strip().lower()
    if not raw:
        return DEFAULT_CELL_PX
    parts = raw.split("x")
    if len(parts) != 2:
        return DEFAULT_CELL_PX
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError:
        return DEFAULT_CELL_PX
    if width < 1 or height < 1:
        return DEFAULT_CELL_PX
    return (width, height)


def sixel_size_for(cols: int, rows: int) -> tuple[int, int]:
    """Pixel (width, height) for a region of ``cols`` x ``rows`` character cells."""
    if cols < 1 or rows < 1:
        raise ClipError(
            f"a sixel region needs at least 1 column and 1 row, not {cols}x{rows}"
        )
    cell_w, cell_h = cell_pixels()
    return (cols * cell_w, rows * cell_h)
