"""Tests for tstudio.theme: one brand theme, no Textual blue anywhere."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from tstudio import theme as theme_module
from tstudio.gallery import branding


def _generated_tokens(t) -> dict[str, str]:
    """Expand a Theme into the tokens Textual resolves in CSS."""
    from textual.design import ColorSystem

    fields = ("primary", "secondary", "warning", "error", "success", "accent",
              "foreground", "background", "surface", "panel", "boost", "dark",
              "luminosity_spread", "text_alpha")
    system = ColorSystem(**{f: getattr(t, f) for f in fields})
    return system.generate()


class ThemeTests(unittest.TestCase):
    def test_the_theme_is_named_tstudio(self) -> None:
        self.assertEqual(theme_module.tstudio_theme().name, "tstudio")
        self.assertEqual(theme_module.TSTUDIO_THEME_NAME, "tstudio")

    def test_colours_come_from_branding(self) -> None:
        t = theme_module.tstudio_theme()
        self.assertEqual(t.primary, branding.AMBER)
        self.assertEqual(t.accent, branding.AMBER)
        self.assertEqual(t.success, branding.GREEN)
        self.assertEqual(t.error, branding.RED)

    def test_primary_is_not_textual_blue(self) -> None:
        self.assertNotEqual(theme_module.tstudio_theme().primary.lower(), "#0178d4")

    def test_no_generated_token_is_textual_blue(self) -> None:
        tokens = _generated_tokens(theme_module.tstudio_theme())
        self.assertTrue(tokens, "expected the theme to generate tokens")
        for name, value in tokens.items():
            self.assertNotIn("#0178D4", str(value).upper(),
                             f"token {name!r} is still Textual blue")

        from textual.theme import BUILTIN_THEMES

        builtin = _generated_tokens(BUILTIN_THEMES["textual-dark"])
        self.assertTrue(any("#0178D4" in str(value).upper()
                            for value in builtin.values()),
                        "expected the builtin textual-dark theme to contain the blue")

    def test_textual_is_not_imported_at_module_level(self) -> None:
        source = Path(theme_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith("textual"),
                                     f"top-level import of {alias.name!r}")
            elif isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("textual"),
                                 f"top-level import from {node.module!r}")


class ApplyThemeTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_theme_twice_does_not_raise(self) -> None:
        from textual.app import App

        class _App(App):
            pass

        app = _App()
        async with app.run_test():
            theme_module.apply_theme(app)
            self.assertEqual(app.theme, "tstudio")
            theme_module.apply_theme(app)
            self.assertEqual(app.theme, "tstudio")


if __name__ == "__main__":
    unittest.main()
