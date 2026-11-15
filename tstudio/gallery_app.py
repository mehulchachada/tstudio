"""The tstudio gallery screen: places, files and a preview column in one terminal app."""

from __future__ import annotations

import unicodedata

import sys
from datetime import datetime
from pathlib import Path

from tstudio import ClipError
from tstudio.gallery import branding
from tstudio.gallery.entries import Entry, filter_entries, folder_summary, list_folder
from tstudio.gallery.favourites import Favourites
from tstudio.gallery.thumbs import art_for
from tstudio.media import MediaInfo, is_hdr, probe
from tstudio.timespec import format_time

_ICONS = {
    "folder": "📁",
    "video": "🎬",
    "image": "🖼️",   # variation selector: without it this is 1 cell, not 2
    "other": "▫",
}

#: Rows of the preview pane reserved for the metadata block under the picture.
_META_ROWS = 2
_MIN_ART_COLS = 8
_MIN_ART_ROWS = 4

_EMPTY_PREVIEW = "select a file"
_LOADING_PREVIEW = "loading…"


def _cells(text: str) -> int:
    """Display width of text in terminal cells (stdlib only)."""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _icon_for(entry: Entry) -> str:
    """The icon, padded so every icon is exactly two cells and names line up."""
    glyph = "📁" if entry.is_parent else _ICONS.get(entry.kind, "▫")
    return glyph + " " * max(0, 2 - _cells(glyph))


def human_size(n: int) -> str:
    """1024-based human size with one decimal: 3.7 GB, 132.1 MB, 2.1 KB, 0 B."""
    if n < 1024:
        return f"{n} B"
    value = float(n)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        value /= 1024.0
        if value < 1024.0 or unit == "PB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PB"


def _truncate(text: str, width: int) -> str:
    """Fit text to width cells, dropping the MIDDLE.

    These filenames are "Phantom Blade Zero 2026.11.15 - 05.37.37.08.DVR.mp4":
    the title is at the front and the only thing telling two apart is the
    timestamp at the back. Cutting either end loses the distinction, so cut
    the middle and keep both.
    """
    if width <= 0:
        return ""
    if _cells(text) <= width:
        return text
    if width <= 3:
        return "…" * width
    keep = width - 1
    head_room = (keep + 1) // 2
    tail_room = keep - head_room
    head = ""
    for ch in text:
        if _cells(head) + _cells(ch) > head_room:
            break
        head += ch
    tail = ""
    for ch in reversed(text):
        if _cells(tail) + _cells(ch) > tail_room:
            break
        tail = ch + tail
    return head + "…" + tail

def _name_cell(entry: Entry, fav: Favourites, width: int) -> str:
    """Icon, name and star, truncated to the column width."""
    icon = _icon_for(entry)
    star = " ★" if fav.is_favourite(entry.path) else ""
    # Truncate only the name: _truncate keeps the tail, so folding the icon
    # into it would cut the icon off every row long enough to need trimming.
    # Widths are in cells, not characters - the icons are double-width.
    room = width - _cells(icon) - 1 - _cells(star)
    return icon + " " + _truncate(entry.name, room) + star


def _size_cell(entry: Entry) -> str:
    """Human size, or — for a folder or the parent row."""
    if entry.is_parent or entry.kind == "folder":
        return "—"
    return human_size(entry.size)


def _modified_cell(entry: Entry) -> str:
    """%d %b %H:%M from the mtime, or — for the parent row."""
    if entry.is_parent:
        return "—"
    return datetime.fromtimestamp(entry.mtime).strftime("%d %b %H:%M")


def _fps_text(fps: float | None) -> str:
    """29.97 -> 29.97, 30.0 -> 30, unknown -> ?."""
    if not fps:
        return "?"
    if abs(fps - round(fps)) < 0.01:
        return str(int(round(fps)))
    return f"{fps:.2f}"


def _video_meta_lines(info: MediaInfo) -> list[str]:
    """Two compact metadata lines for a video: timing/geometry, then codec/size."""
    video = info.video
    first = format_time(info.duration)
    if video is not None:
        first += f" · {video.width}x{video.height} @{_fps_text(video.fps)}fps"
    rest: list[str] = []
    if video is not None and video.codec:
        rest.append(video.codec)
    if video is not None and is_hdr(video):
        rest.append("HDR10")
    rest.append(human_size(info.size))
    return [first, " · ".join(rest)]


def build_gallery(folder: Path, favourites: Favourites | None = None) -> object:
    """Build and return the GalleryApp. Imports textual here, never at module level."""
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.reactive import reactive
    from textual.widgets import DataTable, Input, ListItem, ListView, Static
    from textual.worker import get_current_worker

    from rich.text import Text

    if favourites is None:
        favourites = Favourites()

    class GalleryApp(App):
        TITLE = branding.NAME
        SUB_TITLE = branding.TAGLINE

        CSS = f"""
        Screen {{
            padding: 0;
        }}
        #header {{
            height: 3;
            color: {branding.AMBER};
            padding: 0 1;
        }}
        #panes {{
            height: 1fr;
        }}
        #places {{
            width: 16;
            height: 1fr;
        }}
        #files {{
            width: 1fr;
            height: 1fr;
        }}
        #preview {{
            width: 30;
            height: 1fr;
            border: round {branding.AMBER};
            padding: 1;
            overflow: hidden;
        }}
        #preview-art {{
            width: 1fr;
            height: 1fr;
            overflow: hidden;
        }}
        #preview-meta {{
            width: 1fr;
            height: auto;
            max-height: {_META_ROWS + 2};
            overflow: hidden;
        }}
        #status {{
            height: 1;
            padding: 0 1;
        }}
        #filter {{
            display: none;
            height: 1;
            width: 100%;
            border: none;
            padding: 0 1;
        }}
        """

        BINDINGS = [
            Binding("ctrl+q", "quit", "Quit", priority=True),
            Binding("f5", "reload", "Reload", priority=True),
            Binding("j", "down", "Down", show=False),
            Binding("k", "up", "Up", show=False),
            Binding("g", "first", "First", show=False),
            Binding("G", "last", "Last", show=False),
            Binding("f", "toggle_favourite", "Star", show=False),
            Binding(".", "toggle_hidden", "Hidden", show=False),
            Binding("/", "focus_filter", "Filter", show=False),
            Binding("backspace", "go_up", "Up folder", show=False),
            Binding("left", "focus_left", "Left", show=False),
            Binding("right", "focus_right", "Right", show=False),
        ]

        # Reactive state. Assigning any of these is the whole API: the
        # watchers below do the reloading, filtering and previewing, so no
        # caller ever has to remember to repaint anything by hand.
        folder: reactive[Path] = reactive(Path("."), init=False)
        show_hidden: reactive[bool] = reactive(False, init=False)
        query_text: reactive[str] = reactive("", init=False)
        selected: reactive[Path | None] = reactive(None, init=False)

        def __init__(self, folder: Path, favourites: Favourites) -> None:
            # Watchers run on assignment, and nothing is mounted yet, so they
            # stay inert until on_mount flips this.
            self._live = False
            super().__init__()
            self.favourites = favourites
            self.folder = Path(folder)
            self._entries: list[Entry] = []
            self._status_error = False

        def compose(self) -> ComposeResult:
            yield Static("\n".join(branding.header_lines(self.size.width)), id="header")
            yield Input(placeholder="filter", id="filter")
            with Horizontal(id="panes"):
                with ListView(id="places"):
                    yield ListItem(Static("★ Favourites"))
                    yield ListItem(Static("🕘 Recent"))
                    yield ListItem(Static("─" * 16))
                    for fav in self.favourites.folders:
                        yield ListItem(Static(fav.path.name))
                yield DataTable(id="files")
                with Vertical(id="preview"):
                    yield Static(_EMPTY_PREVIEW, id="preview-art")
                    yield Static("", id="preview-meta")
            yield Static("", id="status")

        def on_mount(self) -> None:
            table = self.query_one("#files", DataTable)
            table.cursor_type = "row"
            table.zebra_stripes = False
            table.add_columns("Name", "Size", "Modified")
            table.focus()
            # border_title is a widget attribute, not a CSS property
            self.query_one("#preview").border_title = "preview"
            self._refresh_header()
            self._live = True
            self._reload(quiet=False)

        def _refresh_header(self) -> None:
            header = self.query_one("#header", Static)
            header.update(Text("\n".join(branding.header_lines(self.size.width)), style=branding.AMBER))

        def on_resize(self) -> None:
            self._refresh_header()

        # -- reactive watchers -----------------------------------------------

        def watch_folder(self, folder: Path) -> None:
            """A new folder means a new listing."""
            if not self._live:
                return
            self._reload()

        def watch_show_hidden(self, show_hidden: bool) -> None:
            """Hidden entries come from the lister, so this re-reads the folder."""
            if not self._live:
                return
            self._reload()

        def watch_query_text(self, query_text: str) -> None:
            """Filtering is a view over the entries already in hand: no disk access."""
            if not self._live:
                return
            self._populate(self._entries)

        def watch_selected(self, selected: Path | None) -> None:
            """Repaint the preview column for whatever is under the cursor."""
            if not self._live:
                return
            pane = self.query_one("#preview")
            kind = self._kind_of(selected)
            if selected is None or kind not in ("video", "image"):
                pane.border_title = "preview"
                self._set_preview(_EMPTY_PREVIEW, [])
                return
            pane.border_title = _truncate(selected.name, max(4, pane.size.width - 2))
            self._set_preview(_LOADING_PREVIEW, [])
            cols = max(_MIN_ART_COLS, pane.size.width)
            rows = max(_MIN_ART_ROWS, pane.size.height - _META_ROWS)
            self._load_preview(selected, kind, cols, rows)

        # -- listing ---------------------------------------------------------

        def _reload(self, *, quiet: bool = True) -> bool:
            """Re-read self.folder into the table. Returns False and shows the error when it fails."""
            folder = self.folder
            try:
                entries = list_folder(folder, show_hidden=self.show_hidden)
            except ClipError as exc:
                self._set_status(str(exc), error=True)
                self.notify(str(exc), title="folder", severity="error")
                return False
            self._entries = entries
            self._populate(entries)
            self._set_status(self._status_line(entries, folder), error=False)
            if not quiet:
                self.favourites.add_recent(folder)
            return True

        def _name_width(self) -> int:
            """Cells available to the Name column.

            table.region.width is 0 before the first layout, which silently
            truncated every name to 10 cells, so derive it from the app size:
            total, less the places and preview panes, less the Size and
            Modified columns and their padding.
            """
            return max(12, self.size.width - 16 - 30 - 26)

        def _populate(self, entries: list[Entry]) -> None:
            table = self.query_one("#files", DataTable)
            table.clear(columns=False)
            width = self._name_width()
            for entry in filter_entries(entries, self.query_text):
                table.add_row(
                    Text(_name_cell(entry, self.favourites, width)),
                    Text(_size_cell(entry)),
                    Text(_modified_cell(entry)),
                )
            if table.row_count:
                table.move_cursor(row=0, column=0)

        def _status_line(self, entries: list[Entry], folder: Path) -> str:
            count, total = folder_summary(entries)
            line = f"{count} items · {human_size(total)} · {folder}"
            width = max(10, self.size.width - 2)
            return _truncate(line, width)

        def _set_status(self, text: str, *, error: bool) -> None:
            self._status_error = error
            status = self.query_one("#status", Static)
            status.update(Text(text, style=branding.RED if error else branding.GREY))

        # -- preview ---------------------------------------------------------

        def _kind_of(self, path: Path | None) -> str | None:
            """The listing's own idea of what this path is - never touches the disk."""
            if path is None:
                return None
            for entry in self._entries:
                if entry.path == path:
                    return "folder" if entry.is_parent else entry.kind
            return None

        def _set_preview(self, message: str, meta: list[str], *, error: bool = False) -> None:
            """Put a plain message (or an error) in the pane."""
            style = branding.RED if error else branding.GREY
            self.query_one("#preview-art", Static).update(Text(message, style=style))
            self.query_one("#preview-meta", Static).update(Text("\n".join(meta), style=branding.GREY))

        def _show_preview(self, path: Path, art: list[str], meta: list[str]) -> None:
            """Paint finished worker output, unless the cursor has moved on since."""
            if path != self.selected:
                return
            picture = Text.from_ansi("\n".join(art)) if art else Text("")
            self.query_one("#preview-art", Static).update(picture)
            self.query_one("#preview-meta", Static).update(Text("\n".join(meta), style=branding.GREY))

        def _show_preview_error(self, path: Path, message: str) -> None:
            if path != self.selected:
                return
            self._set_preview(message, [], error=True)

        @work(thread=True, exclusive=True, group="preview")
        def _load_preview(self, path: Path, kind: str, cols: int, rows: int) -> None:
            """ffmpeg and ImageMagick in a thread: seconds of work, never on the event loop."""
            worker = get_current_worker()
            try:
                if kind == "image":
                    art = art_for(path, cols, rows)
                    meta = [human_size(path.stat().st_size)]
                else:
                    info = probe(path)
                    meta = _video_meta_lines(info)
                    at = info.duration / 2 if info.duration else None
                    art = art_for(path, cols, rows, at=at)
            except ClipError as exc:
                self._from_thread(worker, self._show_preview_error, path, str(exc))
                return
            except OSError as exc:
                self._from_thread(worker, self._show_preview_error, path, str(exc))
                return
            self._from_thread(worker, self._show_preview, path, art, meta)

        def _from_thread(self, worker: object, callback, *args: object) -> None:
            """call_from_thread, but silent when the worker was superseded or the app is gone."""
            if getattr(worker, "is_cancelled", False):
                return
            try:
                self.app.call_from_thread(callback, *args)
            except RuntimeError:
                pass  # app shut down while the preview was still rendering

        # -- navigation ------------------------------------------------------

        def _selected_entry(self) -> Entry | None:
            table = self.query_one("#files", DataTable)
            if table.row_count == 0:
                return None
            visible = filter_entries(self._entries, self.query_text)
            row = table.cursor_row
            if row is None or row >= len(visible):
                return None
            return visible[row]

        def _focus_table(self) -> None:
            self.query_one("#files", DataTable).focus()

        def _focus_places(self) -> None:
            self.query_one("#places", ListView).focus()

        def on_data_table_row_highlighted(self, event) -> None:
            """The cursor is the selection: let the reactive drive the preview."""
            entry = self._selected_entry()
            self.selected = None if entry is None else entry.path

        def action_down(self) -> None:
            self._focus_table()
            self.query_one("#files", DataTable).move_cursor_down()

        def action_up(self) -> None:
            self._focus_table()
            self.query_one("#files", DataTable).move_cursor_up()

        def action_first(self) -> None:
            self._focus_table()
            table = self.query_one("#files", DataTable)
            if table.row_count:
                table.move_cursor(row=0, column=0)

        def action_last(self) -> None:
            self._focus_table()
            table = self.query_one("#files", DataTable)
            if table.row_count:
                table.move_cursor(row=table.row_count - 1, column=0)

        def action_toggle_favourite(self) -> None:
            entry = self._selected_entry()
            if entry is None:
                return
            self.favourites.toggle(entry.path)
            starred = self.favourites.is_favourite(entry.path)
            # _populate resets the cursor, so put it back where the user left it
            table = self.query_one("#files", DataTable)
            row = table.cursor_row
            self._populate(self._entries)
            if 0 <= row < table.row_count:
                table.move_cursor(row=row, column=0)
            verb = "added to" if starred else "removed from"
            self.notify(f"{entry.name} {verb} favourites", title="★")

        def action_toggle_hidden(self) -> None:
            self.show_hidden = not self.show_hidden

        def action_focus_filter(self) -> None:
            self.query_one("#filter", Input).focus()

        def action_go_up(self) -> None:
            if self.folder.parent == self.folder:
                return  # filesystem root: stay quietly
            self.folder = self.folder.parent

        def action_focus_left(self) -> None:
            self._focus_places()

        def action_focus_right(self) -> None:
            self._focus_table()

        def action_reload(self) -> None:
            # Same folder, so the watcher would not fire: re-read it outright.
            self._reload()

        def on_key(self, event) -> None:
            """Enter opens a folder; Escape leaves the filter; single letters stay out of the filter."""
            filter_input = self.query_one("#filter", Input)
            if self.focused is filter_input:
                if event.key == "escape":
                    filter_input.value = ""
                    self.query_text = ""
                    self._focus_table()
                    event.stop()
                elif event.key == "enter":
                    self.query_text = filter_input.value
                    self._focus_table()
                    event.stop()
                else:
                    event.stop()
                return
            if event.key == "enter":
                entry = self._selected_entry()
                if entry is not None and entry.kind == "folder":
                    self.query_text = ""
                    self.folder = entry.path
                elif entry is not None and entry.kind == "video":
                    self._open_editor(entry.path)
                event.stop()

        def _open_editor(self, path: Path) -> None:
            """Hand the terminal to the editor, then take it back.

            The editor is its own Textual App, and an App cannot run inside
            another one. suspend() releases the terminal so it can, which is
            what yazi does for an external editor. On the way back the folder
            is reloaded, because the editor has very likely written a new file
            into it.
            """
            try:
                from tstudio.tui import run_editor
            except Exception as exc:  # noqa: BLE001 - textual missing, etc.
                self.notify(str(exc), title="cannot open the editor", severity="error")
                return
            self.favourites.add_recent(path)
            try:
                with self.suspend():
                    run_editor(path)
            except Exception as exc:  # noqa: BLE001 - never take the gallery down
                self.notify(str(exc), title="editor failed", severity="error")
            self._reload()
            self.refresh(layout=True)

        def on_input_changed(self, event) -> None:
            """Live filtering while the filter input has focus."""
            if event.input.id == "filter":
                self.query_text = event.value

        def on_unmount(self) -> None:
            self.favourites.save()

    return GalleryApp(folder, favourites)


def run_gallery(folder: Path) -> int:
    """Load favourites, build the app, run it, and return an exit code. Never a traceback."""
    favourites = Favourites()
    favourites.load()
    try:
        app = build_gallery(folder, favourites)
    except ImportError:
        print("The gallery needs the 'textual' package (an optional dependency).")
        return 1
    except ClipError as exc:
        print(f"tstudio: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"tstudio: {exc}", file=sys.stderr)
        return 1
    try:
        app.run()
    except KeyboardInterrupt:
        return 130
    except ClipError as exc:
        print(f"tstudio: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"tstudio: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_gallery(Path(sys.argv[1] if len(sys.argv) > 1 else ".")))
