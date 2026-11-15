"""Headless tests for the gallery's modal dialogs, driven via run_test().

Textual is an optional dependency, so these tests skip (rather than fail) when
it is absent. Each dialog is pushed by a tiny host App and driven with the
pilot; every scenario runs through asyncio.run, so no event loop is left
behind and the suite stays free of "Task was destroyed" warnings.
"""

from __future__ import annotations

import asyncio
import unittest

try:  # Textual is optional for the CLI; skip (rather than fail) if it is absent.
    import textual  # noqa: F401
    _HAS_TEXTUAL = True
except ImportError:
    _HAS_TEXTUAL = False

from tstudio import ClipError
from tstudio.gallery import branding


_UNSET = object()


def _host(factory):
    """A one-screen App that pushes `factory()` and records what it dismisses."""
    from textual.app import App

    class Host(App):
        def __init__(self) -> None:
            super().__init__()
            self.result = _UNSET

        def on_mount(self) -> None:
            self.push_screen(factory(), self._record)

        def _record(self, value) -> None:
            self.result = value

    return Host()


def _screen_text(pilot) -> str:
    """Every Static line on the current screen, joined with newlines."""
    from textual.widgets import Static

    lines: list[str] = []
    for node in pilot.app.screen.query(Static):
        render = node.render
        if callable(render):
            render = render()
        lines.append(render if isinstance(render, str) else str(render))
    return "\n".join(lines)


def _offenders(pilot, width: int, height: int) -> list[str]:
    """Widgets whose region pokes outside a width x height terminal."""
    out: list[str] = []
    for node in pilot.app.screen.walk_children():
        if type(node).__name__ == "Tooltip":
            continue  # Textual parks tooltips off-screen deliberately
        region = node.region
        if region.width == 0 and region.height == 0:
            continue
        if region.y + region.height > height or region.x + region.width > width:
            out.append(f"{type(node).__name__}#{node.id} {region}")
    return out


@unittest.skipUnless(_HAS_TEXTUAL, "Textual required")
class TestConfirmScreen(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def _confirm(self, **kwargs):
        from tstudio.gallery.screens import build_confirm_screen

        def factory():
            return build_confirm_screen(
                kwargs.get("title", "Discard the trim?"),
                kwargs.get("body", ["clip.mp4", "unsaved in/out points"]),
                kwargs.get("confirm_label", "Discard"),
                danger=kwargs.get("danger", False),
            )

        return _host(factory)

    def test_confirm_button_returns_true(self) -> None:
        async def scenario():
            async with self._confirm().run_test() as pilot:
                await pilot.pause()
                pilot.app.screen.query_one("#confirm").press()
                await pilot.pause()
                self.assertIs(pilot.app.result, True)

        self._run(scenario())

    def test_cancel_button_returns_false(self) -> None:
        async def scenario():
            async with self._confirm().run_test() as pilot:
                await pilot.pause()
                pilot.app.screen.query_one("#cancel").press()
                await pilot.pause()
                self.assertIs(pilot.app.result, False)

        self._run(scenario())

    def test_escape_returns_false(self) -> None:
        async def scenario():
            async with self._confirm().run_test() as pilot:
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                self.assertIs(pilot.app.result, False)

        self._run(scenario())

    def test_a_dangerous_dialog_focuses_cancel(self) -> None:
        async def scenario():
            async with self._confirm(danger=True).run_test() as pilot:
                await pilot.pause()
                focused = pilot.app.focused
                self.assertIsNotNone(focused, "the dialog focused nothing")
                self.assertEqual(focused.id, "cancel")
                self.assertEqual(str(focused.label), "Cancel")

        self._run(scenario())

    def test_a_safe_dialog_focuses_the_confirm_button(self) -> None:
        async def scenario():
            async with self._confirm(danger=False).run_test() as pilot:
                await pilot.pause()
                focused = pilot.app.focused
                self.assertIsNotNone(focused, "the dialog focused nothing")
                self.assertEqual(focused.id, "confirm")
                self.assertEqual(str(focused.label), "Discard")

        self._run(scenario())

    def test_enter_activates_the_focused_button(self) -> None:
        async def scenario():
            async with self._confirm(danger=True).run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")  # Cancel has focus when danger
                await pilot.pause()
                self.assertIs(pilot.app.result, False)

        self._run(scenario())

    def test_body_lines_and_title_are_shown(self) -> None:
        async def scenario():
            async with self._confirm().run_test() as pilot:
                await pilot.pause()
                text = _screen_text(pilot)
                self.assertIn("Discard the trim?", text)
                self.assertIn("clip.mp4", text)
                self.assertIn("unsaved in/out points", text)

        self._run(scenario())

    def test_danger_frames_the_dialog_in_red(self) -> None:
        async def scenario():
            async with self._confirm(danger=True).run_test() as pilot:
                await pilot.pause()
                dialog = pilot.app.screen.query_one("#dialog")
                kind, colour = dialog.styles.border_top
                self.assertEqual(kind, "round")
                self.assertEqual(colour.hex.lower(), branding.RED.lower())
                btn = pilot.app.screen.query_one("#confirm")
                self.assertEqual(btn.variant, "error")

        self._run(scenario())

    def test_a_safe_dialog_is_framed_in_amber(self) -> None:
        async def scenario():
            async with self._confirm().run_test() as pilot:
                await pilot.pause()
                dialog = pilot.app.screen.query_one("#dialog")
                kind, colour = dialog.styles.border_top
                self.assertEqual(kind, "round")
                self.assertEqual(colour.hex.lower(), branding.AMBER.lower())
                self.assertEqual(
                    pilot.app.screen.query_one("#confirm").variant, "primary")

        self._run(scenario())

    def test_the_background_behind_the_dialog_is_dimmed(self) -> None:
        async def scenario():
            async with self._confirm().run_test() as pilot:
                await pilot.pause()
                screen = pilot.app.screen
                self.assertLess(screen.styles.background.a, 1.0,
                                "the modal screen is not translucent")
                self.assertEqual(screen.styles.align_horizontal, "center")
                self.assertEqual(screen.styles.align_vertical, "middle")

        self._run(scenario())

    def test_an_empty_title_is_a_cliperror(self) -> None:
        from tstudio.gallery.screens import build_confirm_screen

        with self.assertRaises(ClipError):
            build_confirm_screen("  ", ["x"], "Go")
        with self.assertRaises(ClipError):
            build_confirm_screen("Title", ["x"], "")


@unittest.skipUnless(_HAS_TEXTUAL, "Textual required")
class TestDeleteScreen(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def _delete(self):
        from tstudio.gallery.screens import build_delete_screen

        return _host(lambda: build_delete_screen(
            "sunset-timelapse.mp4", "41.2 MB", "0:38"))

    def test_delete_wording_never_says_permanent(self) -> None:
        async def scenario():
            async with self._delete().run_test() as pilot:
                await pilot.pause()
                text = _screen_text(pilot)
                self.assertIn("sunset-timelapse.mp4", text)
                self.assertIn("can be restored", text)
                self.assertIn("Move to Recycle Bin?", text)
                lowered = text.lower()
                self.assertNotIn("permanently", lowered)
                self.assertNotIn("permanent", lowered)
                label = str(pilot.app.screen.query_one("#confirm").label)
                self.assertEqual(label, "Move to Recycle Bin")
                self.assertNotIn("permanent", label.lower())

        self._run(scenario())

    def test_delete_shows_the_size_and_length_together(self) -> None:
        async def scenario():
            async with self._delete().run_test() as pilot:
                await pilot.pause()
                self.assertIn("41.2 MB · 0:38", _screen_text(pilot))

        self._run(scenario())

    def test_delete_defaults_to_cancel(self) -> None:
        async def scenario():
            async with self._delete().run_test() as pilot:
                await pilot.pause()
                self.assertEqual(pilot.app.focused.id, "cancel")
                pilot.app.screen.query_one("#confirm").press()
                await pilot.pause()
                self.assertIs(pilot.app.result, True)

        self._run(scenario())

    def test_a_nameless_file_is_a_cliperror(self) -> None:
        from tstudio.gallery.screens import build_delete_screen

        with self.assertRaises(ClipError):
            build_delete_screen("   ", "1 KB", "0:01")


@unittest.skipUnless(_HAS_TEXTUAL, "Textual required")
class TestShareScreen(unittest.TestCase):
    TARGETS = [
        ("clipboard", "Copy to clipboard"),
        ("folder", "Reveal in file manager"),
        ("mail", "Attach to a new mail"),
    ]
    STEPS = [
        "The clip is copied, not moved.",
        "Nothing is uploaded anywhere.",
    ]

    def _run(self, coro):
        return asyncio.run(coro)

    def _share(self):
        from tstudio.gallery.screens import build_share_screen

        return _host(lambda: build_share_screen(list(self.TARGETS),
                                                list(self.STEPS)))

    def test_share_returns_the_chosen_target_name(self) -> None:
        async def scenario():
            async with self._share().run_test() as pilot:
                await pilot.pause()
                self.assertEqual(pilot.app.focused.id, "targets")
                await pilot.press("down")      # clipboard -> folder
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(pilot.app.result, "folder")

        self._run(scenario())

    def test_share_returns_the_first_target_on_a_plain_enter(self) -> None:
        async def scenario():
            async with self._share().run_test() as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(pilot.app.result, "clipboard")

        self._run(scenario())

    def test_share_returns_none_on_escape(self) -> None:
        async def scenario():
            async with self._share().run_test() as pilot:
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsNone(pilot.app.result)

        self._run(scenario())

    def test_share_lists_every_target_label(self) -> None:
        from textual.widgets import OptionList

        async def scenario():
            async with self._share().run_test() as pilot:
                await pilot.pause()
                options = pilot.app.screen.query_one("#targets", OptionList)
                self.assertEqual(options.option_count, len(self.TARGETS))
                shown = [str(options.get_option_at_index(i).prompt)
                         for i in range(options.option_count)]
                for _name, label in self.TARGETS:
                    self.assertIn(label, shown)

        self._run(scenario())

    def test_share_shows_the_steps_under_the_list(self) -> None:
        from textual.widgets import OptionList

        async def scenario():
            async with self._share().run_test() as pilot:
                await pilot.pause()
                text = _screen_text(pilot)
                for step in self.STEPS:
                    self.assertIn(step, text)
                options = pilot.app.screen.query_one("#targets", OptionList)
                steps = pilot.app.screen.query_one("#steps")
                self.assertGreaterEqual(steps.region.y, options.region.y)

        self._run(scenario())

    def test_no_targets_is_a_cliperror(self) -> None:
        from tstudio.gallery.screens import build_share_screen

        with self.assertRaises(ClipError):
            build_share_screen([], ["nothing to do"])


@unittest.skipUnless(_HAS_TEXTUAL, "Textual required")
class TestDialogLayout(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def _factories(self):
        from tstudio.gallery.screens import (
            build_confirm_screen,
            build_delete_screen,
            build_share_screen,
        )

        return {
            "confirm": lambda: build_confirm_screen(
                "Overwrite the existing file?",
                ["holiday-cut.mp4 already exists in this folder",
                 "The old file is replaced."],
                "Overwrite"),
            "delete": lambda: build_delete_screen(
                "a-rather-long-clip-name-from-a-phone.mp4", "412.8 MB", "12:04"),
            "share": lambda: build_share_screen(
                [("clipboard", "Copy to clipboard"),
                 ("folder", "Reveal in file manager"),
                 ("mail", "Attach to a new mail")],
                ["The clip is copied, not moved.",
                 "Nothing is uploaded anywhere."]),
        }

    def test_every_dialog_fits_in_a_100x34_terminal(self) -> None:
        for name, factory in self._factories().items():
            with self.subTest(dialog=name):
                async def scenario(factory=factory, name=name):
                    async with _host(factory).run_test(size=(100, 34)) as pilot:
                        await pilot.pause()
                        offenders = _offenders(pilot, 100, 34)
                        self.assertEqual(
                            offenders, [],
                            f"widgets off-screen at 100x34 ({name}): "
                            + "; ".join(offenders))
                        dialog = pilot.app.screen.query_one("#dialog")
                        self.assertEqual(dialog.region.width, 54)
                        self.assertGreater(dialog.region.x, 0,
                                           "the dialog is not centred")

                self._run(scenario())


if __name__ == "__main__":
    unittest.main()
