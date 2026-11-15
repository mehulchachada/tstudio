"""Modal dialogs the gallery pushes: confirm, move-to-Recycle-Bin and share.

The rest of tstudio is deliberately unboxed - whitespace and a single accent
bar instead of borders everywhere. A modal is the one place a frame earns its
keep, so these dialogs are the only widgets in the app with a full border: a
round rule in the brand amber, or brand red when the action is destructive,
floating over a dimmed copy of the screen behind it.

Textual is an optional dependency, so every dialog is produced by a builder
function that imports textual on the inside. Nothing at this module's top
level touches textual, and the zero-dependency CLI keeps working without it.
"""

from __future__ import annotations

from tstudio import ClipError

# Geometry, in cells. Wide enough for a long file name on one line, narrow
# enough that the dialog still reads as a dialog in an 80-column terminal.
DIALOG_WIDTH = 54

# The exact reassurance line for the Recycle Bin flow. The code moves files to
# the Recycle Bin, so the dialog must never claim anything stronger.
RESTORE_LINE = "It goes to the Recycle Bin and can be restored."


def _dialog_css(selector: str, frame: str, *, grey: str) -> str:
    """The shared dialog CSS, with every colour substituted in exactly once.

    `selector` is the screen class's own type name: Textual scopes a widget's
    DEFAULT_CSS to that type, so the rule that centres and dims the screen has
    to name it rather than the `ModalScreen` base.
    """
    return f"""
    {selector} {{
        align: center middle;
        /* Dim whatever is behind the dialog: a translucent wash on the modal
           screen itself, which is what ModalScreen is for. */
        background: black 60%;
    }}
    #dialog {{
        width: {DIALOG_WIDTH};
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: round {frame};
    }}
    #dialog_title {{
        height: auto;
        text-style: bold;
        margin-bottom: 1;
    }}
    .dialog_line {{
        height: auto;
        color: {grey};
    }}
    #dialog_buttons {{
        height: auto;
        align-horizontal: right;
        /* The blank line that keeps the buttons off the body text. */
        margin-top: 1;
    }}
    #confirm {{
        margin-left: 2;
    }}
    #targets {{
        height: auto;
        max-height: 10;
        border: none;
        padding: 0;
        background: $surface;
    }}
    #steps {{
        height: auto;
        margin-top: 1;
    }}
    """


def build_confirm_screen(title: str, body: list[str], confirm_label: str,
                         *, danger: bool = False):
    """Build a `ModalScreen[bool]` instance: a title, body lines, two buttons.

    Dismisses `True` when the confirm button is pressed and `False` on Cancel
    or Escape. When `danger` is true the confirm button is red and - more
    importantly - Cancel takes the initial focus, so leaning on Enter can
    never carry out a destructive action by itself.
    """
    if not title.strip():
        raise ClipError("a confirmation dialog needs a title")
    if not confirm_label.strip():
        raise ClipError("a confirmation dialog needs a label for its confirm button")

    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import Button, Static

    from tstudio.gallery import branding

    lines = list(body)
    frame = branding.RED if danger else branding.AMBER

    class ConfirmScreen(ModalScreen[bool]):
        """Yes/no over a dimmed screen; `False` is always one Escape away."""

        DEFAULT_CSS = _dialog_css("ConfirmScreen", frame, grey=branding.GREY)
        AUTO_FOCUS = "#cancel" if danger else "#confirm"
        BINDINGS = [
            Binding("escape", "cancel", "Cancel", priority=True),
            Binding("enter", "activate", "Confirm"),
        ]

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Static(title, id="dialog_title", markup=False)
                for index, line in enumerate(lines):
                    yield Static(line, classes="dialog_line",
                                 id=f"body_{index}", markup=False)
                with Horizontal(id="dialog_buttons"):
                    yield Button("Cancel", id="cancel")
                    yield Button(confirm_label,
                                 variant="error" if danger else "primary",
                                 id="confirm")

        def on_mount(self) -> None:
            # AUTO_FOCUS covers this, but the focus is the whole point of the
            # danger flag, so it is set here too rather than inferred.
            self.query_one(self.AUTO_FOCUS, Button).focus()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            event.stop()
            self.dismiss(event.button.id == "confirm")

        def action_cancel(self) -> None:
            self.dismiss(False)

        def action_activate(self) -> None:
            """Enter presses whatever has focus (buttons handle it themselves)."""
            focused = self.focused
            if isinstance(focused, Button):
                focused.press()

    return ConfirmScreen()


def build_delete_screen(name: str, size_text: str, length_text: str):
    """Build the Recycle Bin confirmation for `name` - a `ModalScreen[bool]`.

    A `build_confirm_screen` with the Recycle Bin wording: the file name, its
    size and length, and the promise that the move can be undone. It says
    "Move to Recycle Bin" and never "delete permanently", because that is not
    what the code behind it does.
    """
    if not name.strip():
        raise ClipError("cannot confirm a Recycle Bin move without a file name")
    return build_confirm_screen(
        "Move to Recycle Bin?",
        [name, f"{size_text} · {length_text}", RESTORE_LINE],
        "Move to Recycle Bin",
        danger=True,
    )


def build_share_screen(targets: list[tuple[str, str]], steps: list[str]):
    """Build a `ModalScreen[str | None]`: pick one share target from a list.

    `targets` is a list of `(name, label)` pairs - the label is shown, the
    name is what the screen dismisses with. `steps` describes what happens
    once a target is chosen and sits under the list. Escape dismisses `None`.
    """
    if not targets:
        raise ClipError("the share dialog needs at least one target")

    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.screen import ModalScreen
    from textual.widgets import OptionList, Static
    from textual.widgets.option_list import Option

    from tstudio.gallery import branding

    pairs = list(targets)
    names = [name for name, _ in pairs]
    step_lines = list(steps)

    class ShareScreen(ModalScreen[str | None]):
        """A target list over a dimmed screen; dismisses the chosen name."""

        DEFAULT_CSS = _dialog_css("ShareScreen", branding.AMBER,
                                  grey=branding.GREY)
        AUTO_FOCUS = "#targets"
        BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Static(f"Share from {branding.NAME}", id="dialog_title",
                             markup=False)
                yield OptionList(
                    *[Option(label, id=name) for name, label in pairs],
                    id="targets", markup=False)
                with Vertical(id="steps"):
                    for index, line in enumerate(step_lines):
                        yield Static(line, classes="dialog_line",
                                     id=f"step_{index}", markup=False)

        def on_mount(self) -> None:
            self.query_one("#targets", OptionList).focus()

        def on_option_list_option_selected(
                self, event: OptionList.OptionSelected) -> None:
            event.stop()
            self.dismiss(event.option_id or names[event.option_index])

        def action_cancel(self) -> None:
            self.dismiss(None)

    return ShareScreen()
