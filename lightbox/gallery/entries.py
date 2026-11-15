"""List a folder as gallery entries: classify, sort, filter and summarise."""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path

from lightbox import ClipError

VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".m4v", ".webm", ".avi", ".wmv", ".flv", ".ts"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".avif"}

_RUN = re.compile(r"\d+|\D+")


def _natural_key(name: str) -> list[object]:
    """Split a name into digit and non-digit runs so 2 sorts before 10."""
    parts: list[object] = []
    for run in _RUN.findall(name):
        if run.isdigit():
            parts.append((1, int(run), ""))
        else:
            parts.append((0, 0, run.lower()))
    return parts


def classify(path: Path) -> str:
    """Return "folder", "video", "image" or "other" for a path."""
    if path.is_dir():
        return "folder"
    suffix = path.suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    return "other"


def _is_hidden(path: Path) -> bool:
    """True for dot-files and anything with the Windows hidden/system attribute."""
    if path.name.startswith("."):
        return True
    try:
        attrs = path.stat().st_file_attributes
    except (AttributeError, OSError):
        return False
    hidden = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0)
    system = getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0)
    return bool(attrs & (hidden | system))


@dataclass(frozen=True)
class Entry:
    """One row in the gallery: a folder, video, image or other file."""

    path: Path
    kind: str  # "folder" | "video" | "image" | "other"
    size: int  # bytes; 0 for a folder
    mtime: float  # POSIX timestamp
    is_parent: bool  # True only for the ".." entry

    @property
    def name(self) -> str:
        return ".." if self.is_parent else self.path.name


def list_folder(
    folder: Path,
    *,
    show_hidden: bool = False,
    kinds: frozenset[str] | None = None,
) -> list[Entry]:
    """List a folder as entries: parent first, then folders, then files, naturally sorted."""
    if not folder.is_dir():
        raise ClipError(f"not a folder: {folder}")

    entries: list[Entry] = []
    if folder.parent != folder:
        entries.append(Entry(path=folder.parent, kind="folder", size=0, mtime=0.0, is_parent=True))

    for path in folder.iterdir():
        if not show_hidden and _is_hidden(path):
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        kind = classify(path)
        if kinds is not None and kind not in kinds and kind != "folder":
            continue
        is_dir = kind == "folder"
        entries.append(
            Entry(
                path=path,
                kind=kind,
                size=0 if is_dir else st.st_size,
                mtime=st.st_mtime,
                is_parent=False,
            )
        )

    entries.sort(key=lambda e: (not e.is_parent, e.kind != "folder", _natural_key(e.name)))
    return entries


def folder_summary(entries: list[Entry]) -> tuple[int, int]:
    """Return (count of non-parent entries, total bytes of the file entries)."""
    count = sum(1 for e in entries if not e.is_parent)
    size = sum(e.size for e in entries if not e.is_parent and e.kind != "folder")
    return count, size


def filter_entries(entries: list[Entry], query: str) -> list[Entry]:
    """Case-insensitive substring match on the name; the parent entry always survives."""
    q = query.strip().lower()
    if not q:
        return list(entries)
    return [e for e in entries if e.is_parent or q in e.name.lower()]
