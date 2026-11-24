from __future__ import annotations

import re
from dataclasses import dataclass


CUSTOM_EMOJI_ID_RE = re.compile(r"^\d{10,24}$")


@dataclass(frozen=True)
class ButtonIcon:
    fallback: str
    custom_emoji_id: str


# Verified public IDs from the TgAndroidIcons-compatible catalog:
# https://github.com/abbosahmad/telegram-premium-emojis
# Every ID can be replaced through the environment.
DEFAULT_BUTTON_ICONS: dict[str, ButtonIcon] = {
    "mode": ButtonIcon("⚙️", "5258420634785947640"),
    "fast": ButtonIcon("⚡️", "5843553939672274145"),
    "new": ButtonIcon("📄", "5258477770735885832"),
    "failed": ButtonIcon("📁", "5875206779196935950"),
    "cancel": ButtonIcon("❌", "5258226313285607065"),
    "skip": ButtonIcon("⬅️", "5877536313623711363"),
    "success": ButtonIcon("✅", "5920052658743283381"),
    "info": ButtonIcon("ℹ️", "5258503720928288433"),
}


class ButtonTheme:
    """Resolve button icons without duplicating custom and Unicode emoji."""

    def __init__(self, *, enabled: bool, overrides: str = "") -> None:
        self.enabled = enabled
        self.icons = dict(DEFAULT_BUTTON_ICONS)
        self.invalid_overrides: list[str] = []
        self._apply_overrides(overrides)

    def _apply_overrides(self, raw: str) -> None:
        for item in re.split(r"[,;]", raw):
            item = item.strip()
            if not item:
                continue
            key, separator, custom_id = item.partition("=")
            key = key.strip().lower()
            custom_id = custom_id.strip()
            if (
                not separator
                or key not in self.icons
                or not CUSTOM_EMOJI_ID_RE.fullmatch(custom_id)
            ):
                self.invalid_overrides.append(item)
                continue
            current = self.icons[key]
            self.icons[key] = ButtonIcon(current.fallback, custom_id)

    def icon_id(self, key: str) -> str | None:
        icon = self.icons.get(key)
        if not self.enabled or icon is None:
            return None
        return icon.custom_emoji_id

    def key_for_id(self, custom_emoji_id: str | None) -> str | None:
        if not custom_emoji_id:
            return None
        for key, icon in self.icons.items():
            if icon.custom_emoji_id == custom_emoji_id:
                return key
        return None

    def label(self, key: str, text: str) -> str:
        icon = self.icons.get(key)
        if icon is None or self.icon_id(key) is not None:
            return text
        return f"{icon.fallback} {text}"
