"""The tstudio gallery screen: places, files and a preview column in one terminal app."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from tstudio import ClipError
from tstudio.gallery import branding
from tstudio.gallery.entries import Entry, filter_entries, folder_summary, list_folder
from tstudio.gallery.favourites import Favourites

_ICONS = {"folder": "📁", "video": "🎬", "image": "🖼", "other": "▫"}


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
    """Cut text to width cells, adding a leading … when it was cut."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return "…" + text[-(width - 1):]


def _name_cell(entry: Entry, fav: Favourites, width: int) -> str:
    """Icon, name and star, truncated to the column width."""
    icon = "📁" if entry.is_parent else _ICONS.get(entry.kind, "▫")
    star = " ★" if fav.is_favourite(entry.path) else ""
    return _truncate(icon + " " + entry.name + star, width)


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


def build_gallery(folder: Path, favourites: Favourites | None = None) -> object:
    """Build and return the GalleryApp. Imports textual here, never at module level."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.widgets import DataTable, Input, ListItem, ListView, Static

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

        def __init__(self, folder: Path, favourites: Favourites) -> None:
            super().__init__()
            self.folder = folder
            self.favourites = favourites
            self.show_hidden = False
            self.query_text = ""
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
                    yield Static("select a file")
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
            self._load_folder(self.folder, quiet=False)

        def _refresh_header(self) -> None:
            header = self.query_one("#header", Static)
            header.update(Text("\n".join(branding.header_lines(self.size.width)), style=branding.AMBER))

        def on_resize(self) -> None:
            self._refresh_header()

        # -- listing ---------------------------------------------------------

        def _load_folder(self, folder: Path, *, quiet: bool = True) -> bool:
            """Load folder into the table. Returns False and shows the error when it fails."""
            try:
                entries = list_folder(folder, show_hidden=self.show_hidden)
            except ClipError as exc:
                self._set_status(str(exc), error=True)
                return False
            self.folder = folder
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
            # _populate resets the cursor, so put it back where the user left it
            table = self.query_one("#files", DataTable)
            row = table.cursor_row
            self._populate(self._entries)
            if 0 <= row < table.row_count:
                table.move_cursor(row=row, column=0)

        def action_toggle_hidden(self) -> None:
            self.show_hidden = not self.show_hidden
            self._load_folder(self.folder)

        def action_focus_filter(self) -> None:
            self.query_one("#filter", Input).focus()

        def action_go_up(self) -> None:
            if self.folder.parent == self.folder:
                return  # filesystem root: stay quietly
            self._load_folder(self.folder.parent)

        def action_focus_left(self) -> None:
            self._focus_places()

        def action_focus_right(self) -> None:
            self._focus_table()

        def action_reload(self) -> None:
            self._load_folder(self.folder)

        def on_key(self, event) -> None:
            """Enter opens a folder; Escape leaves the filter; single letters stay out of the filter."""
            filter_input = self.query_one("#filter", Input)
            if self.focused is filter_input:
                if event.key == "escape":
                    filter_input.value = ""
                    self.query_text = ""
                    self._populate(self._entries)
                    self._focus_table()
                    event.stop()
                elif event.key == "enter":
                    self.query_text = filter_input.value
                    self._populate(self._entries)
                    self._focus_table()
                    event.stop()
                else:
                    event.stop()
                return
            if event.key == "enter":
                entry = self._selected_entry()
                if entry is not None and entry.kind == "folder":
                    self.query_text = ""
                    self._load_folder(entry.path)
                event.stop()

        def on_input_changed(self, event) -> None:
            """Live filtering while the filter input has focus."""
            if event.input.id == "filter":
                self.query_text = event.value
                self._populate(self._entries)

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
