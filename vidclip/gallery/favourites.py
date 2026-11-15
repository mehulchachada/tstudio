"""Persist starred folders and files, plus a recent list, as JSON on disk."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_VERSION = 1
_RECENT_CAP = 50


def _default_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "vclip" / "favourites.json"
    return Path.home() / ".vclip" / "favourites.json"


def _normalise(path: Path) -> Path:
    """Resolve to an absolute path; casefold on Windows so C:\\A and c:\\a compare equal."""
    p = path.resolve()
    if os.name == "nt":
        return Path(str(p).casefold())
    return p


@dataclass
class FavEntry:
    """One starred path with when it was added and an optional note."""

    path: Path
    added: str  # ISO-8601, second precision
    note: str = ""


class Favourites:
    """Starred folders and files, plus a capped recent list, persisted as JSON."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else _default_path()
        self.folders: list[FavEntry] = []
        self.files: list[FavEntry] = []
        self.recent: list[FavEntry] = []

    def load(self) -> None:
        """Load from disk; a missing or corrupt file leaves an empty, usable object."""
        self.folders = []
        self.files = []
        self.recent = []
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != _VERSION:
            return
        for key, target in (("folders", self.folders), ("files", self.files), ("recent", self.recent)):
            for item in data.get(key, []):
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    target.append(
                        FavEntry(
                            path=Path(item["path"]),
                            added=str(item.get("added", "")),
                            note=str(item.get("note", "")),
                        )
                    )

    def save(self) -> None:
        """Atomically write the current state: <path>.tmp then os.replace."""
        data = {
            "version": _VERSION,
            "folders": [self._entry_dict(e) for e in self.folders],
            "files": [self._entry_dict(e) for e in self.files],
            "recent": [self._entry_dict(e) for e in self.recent],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _entry_dict(e: FavEntry) -> dict:
        return {"path": str(e.path), "added": e.added, "note": e.note}

    def is_favourite(self, path: Path) -> bool:
        """True if the path is starred as a folder or a file (case-insensitive on Windows)."""
        key = _normalise(path)
        return any(_normalise(e.path) == key for e in self.folders) or any(
            _normalise(e.path) == key for e in self.files
        )

    def toggle(self, path: Path) -> bool:
        """Star or unstar a path; directories go to folders, everything else to files."""
        key = _normalise(path)
        target = self.folders if path.is_dir() else self.files
        for i, e in enumerate(target):
            if _normalise(e.path) == key:
                del target[i]
                return False
        target.append(FavEntry(path=path, added=datetime.now().isoformat(timespec="seconds")))
        return True

    def add_recent(self, path: Path) -> None:
        """Record a path as most recently used: newest first, de-duplicated, capped at 50."""
        key = _normalise(path)
        self.recent = [e for e in self.recent if _normalise(e.path) != key]
        self.recent.insert(
            0, FavEntry(path=path, added=datetime.now().isoformat(timespec="seconds"))
        )
        del self.recent[_RECENT_CAP:]

    def prune_missing(self) -> list[Path]:
        """Return starred paths that no longer exist; does not remove them."""
        missing: list[Path] = []
        for e in [*self.folders, *self.files]:
            if not e.path.exists():
                missing.append(e.path)
        return missing
