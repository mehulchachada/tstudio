"""Thumbnails for the gallery: a disk cache, one-frame extraction, and ANSI art."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from vidclip import ClipError
from vidclip.convert import tonemap_filter
from vidclip.gallery.entries import IMAGE_SUFFIXES, VIDEO_SUFFIXES
from vidclip.media import probe


def _default_cache_dir() -> Path:
    """%LOCALAPPDATA%\\vclip\\thumbs on Windows, ~/.vclip/thumbs elsewhere."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "vclip" / "thumbs"
    return Path.home() / ".vclip" / "thumbs"


CACHE_DIR: Path = _default_cache_dir()


def cache_key(path: Path) -> str:
    """A stable key for (path, mtime, size); editing the file changes it.

    The path is resolved and lower-cased because Windows paths are
    case-insensitive, so the same file always maps to the same key.
    """
    st = path.stat()
    ident = str(path.resolve()).lower()
    digest = hashlib.sha1(ident.encode("utf-8")).hexdigest()
    return f"{digest}_{int(st.st_mtime)}_{st.st_size}"


def cached_thumbnail(path: Path) -> Path | None:
    """The cached PNG for this exact (path, mtime, size), or None."""
    candidate = CACHE_DIR / f"{cache_key(path)}.png"
    return candidate if candidate.is_file() else None


def prune_cache(limit: int = 500) -> int:
    """Keep the ``limit`` newest cache files by mtime, delete the rest.

    Returns how many files were deleted. A missing cache directory is fine.
    """
    if not CACHE_DIR.is_dir():
        return 0
    files = [p for p in CACHE_DIR.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    deleted = 0
    for stale in files[limit:]:
        stale.unlink()
        deleted += 1
    return deleted


def _frame_timestamp(at: float | None, duration: float) -> float:
    """The timestamp to grab: ``at`` when given, else 25% in, clamped to the file."""
    t = at if at is not None else duration * 0.25
    if duration > 0:
        return max(0.0, min(t, duration - 0.1))
    return 0.0


def thumbnail(path: Path, *, at: float | None = None, force: bool = False) -> Path:
    """A PNG on disk for ``path``: the file itself for images, one frame for videos.

    Blocking and safe to call from a worker thread. Raises ClipError for
    anything that is neither image nor video, or when no frame can be read.
    """
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return path
    if suffix not in VIDEO_SUFFIXES:
        raise ClipError(f"no thumbnail for {path.name} - it is not a video or an image")

    key = cache_key(path)
    out = CACHE_DIR / f"{key}.png"
    if out.is_file() and not force:
        return out

    try:
        info = probe(path)
    except ClipError as exc:
        raise ClipError(f"could not read a frame from {path.name}: {exc}") from exc
    t = _frame_timestamp(at, info.duration)
    vf = tonemap_filter(info)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{t:g}", "-i", str(path), "-frames:v", "1",
    ]
    if vf is not None:
        cmd += ["-vf", vf]
    # The sibling keeps a .png extension so ffmpeg can infer the muxer.
    tmp = out.with_name(out.name + ".tmp.png")
    cmd.append(str(tmp))

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        try:
            tmp.unlink()
        except OSError:
            pass
        detail = proc.stderr.decode("utf-8", "replace").strip()
        suffix_msg = f": {detail}" if detail else ""
        raise ClipError(f"could not read a frame from {path.name}{suffix_msg}")
    if tmp.is_file():
        os.replace(tmp, out)
    else:
        raise ClipError(f"could not read a frame from {path.name}")
    return out


def _parse_p6(data: bytes) -> tuple[int, int, bytes]:
    """Parse a P6 PPM: header of 4 tokens with arbitrary whitespace and # comments."""
    i = 0
    tokens: list[bytes] = []
    while len(tokens) < 4 and i < len(data):
        if tokens:  # whitespace between tokens; the byte after maxval is data
            while i < len(data) and data[i:i + 1].isspace():
                i += 1
        if i < len(data) and data[i:i + 1] == b"#":
            while i < len(data) and data[i:i + 1] not in (b"\n", b"\r"):
                i += 1
            continue
        start = i
        while i < len(data) and not data[i:i + 1].isspace() and data[i:i + 1] != b"#":
            i += 1
        tokens.append(data[start:i])
    if len(tokens) < 4 or tokens[0] != b"P6":
        raise ValueError("not a P6 PPM")
    width = int(tokens[1])
    height = int(tokens[2])
    # Exactly one whitespace byte separates maxval from the raw RGB stream.
    i += 1
    return width, height, data[i:]


def render_art(image: Path, cols: int, rows: int) -> list[str]:
    """The image as ANSI text: one U+2580 cell per two pixels, at most cols x rows.

    Each cell carries the top pixel as 24-bit foreground and the bottom pixel
    as 24-bit background, so Textual renders it as a picture.
    """
    if cols < 1 or rows < 1:
        return []
    cmd = [
        "magick", str(image), "-resize", f"{cols}x{rows * 2}>",
        "-background", "black", "-flatten", "ppm:-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise ClipError(f"could not render {image.name}{': ' + detail if detail else ''}")
    width, height, raw = _parse_p6(proc.stdout)
    if len(raw) < width * height * 3:
        raise ClipError(f"could not render {image.name}: truncated pixel data")

    out_rows = min(rows, (height + 1) // 2)
    out_cols = min(cols, width)
    if out_rows < 1 or out_cols < 1:
        return []

    def pixel(r: int, c: int) -> tuple[int, int, int]:
        off = (r * width + c) * 3
        return raw[off], raw[off + 1], raw[off + 2]

    lines: list[str] = []
    for r in range(out_rows):
        cells: list[str] = []
        for c in range(out_cols):
            tr, tg, tb = pixel(r * 2, c)
            if r * 2 + 1 < height:
                br, bg, bb = pixel(r * 2 + 1, c)
            else:
                br, bg, bb = tr, tg, tb
            cells.append(
                f"\x1b[38;2;{tr};{tg};{tb}m\x1b[48;2;{br};{bg};{bb}m\u2580"
            )
        lines.append("".join(cells) + "\x1b[0m")
    return lines


def art_for(path: Path, cols: int, rows: int, *, at: float | None = None) -> list[str]:
    """thumbnail() then render_art() - the one call the UI makes."""
    return render_art(thumbnail(path, at=at), cols, rows)


def art_size(art: list[str]) -> tuple[int, int]:
    """(cols, rows) actually produced, counting cells and not escape bytes."""
    if not art:
        return (0, 0)
    return (art[0].count("\u2580"), len(art))


def print_art(art: list[str], stream=None) -> None:
    """Print art lines to a stream, forcing UTF-8 so a cp1252 console never raises."""
    import io
    import sys
    if stream is None:
        stream = sys.stdout
    try:
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, LookupError):
        pass
    for line in art:
        try:
            print(line, file=stream)
        except UnicodeEncodeError:
            wrapped = io.TextIOWrapper(stream.buffer, encoding="utf-8", line_buffering=True)
            print(line, file=wrapped)
            wrapped.flush()
