from __future__ import annotations

import unittest

from session_shop.button_theme import ButtonTheme


class ButtonThemeTests(unittest.TestCase):
    def test_enabled_theme_uses_custom_icon_without_unicode_duplicate(self) -> None:
        theme = ButtonTheme(enabled=True)

        self.assertEqual(theme.label("cancel", "Отменить"), "Отменить")
        self.assertEqual(theme.icon_id("cancel"), "5258226313285607065")
        self.assertEqual(
            theme.key_for_id("5258226313285607065"),
            "cancel",
        )

    def test_disabled_theme_uses_unicode_fallback(self) -> None:
        theme = ButtonTheme(enabled=False)

        self.assertEqual(theme.label("cancel", "Отменить"), "❌ Отменить")
        self.assertIsNone(theme.icon_id("cancel"))

    def test_valid_override_replaces_default_id(self) -> None:
        theme = ButtonTheme(enabled=True, overrides="cancel=1234567890123456789")

        self.assertEqual(theme.icon_id("cancel"), "1234567890123456789")
        self.assertEqual(theme.invalid_overrides, [])

    def test_invalid_overrides_are_ignored(self) -> None:
        theme = ButtonTheme(
            enabled=True,
            overrides="unknown=1234567890123456789,cancel=not-an-id",
        )

        self.assertEqual(theme.icon_id("cancel"), "5258226313285607065")
        self.assertEqual(len(theme.invalid_overrides), 2)


if __name__ == "__main__":
    unittest.main()
