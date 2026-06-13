"""Проверка доступных методов Bot API в установленной версии aiogram."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot


def bot_has_method(bot: Bot, method_name: str) -> bool:
    return callable(getattr(bot, method_name, None))


def supports_message_draft(bot: Bot) -> bool:
    return bot_has_method(bot, "send_message_draft")


def supports_live_photo(bot: Bot) -> bool:
    return bot_has_method(bot, "send_live_photo")
