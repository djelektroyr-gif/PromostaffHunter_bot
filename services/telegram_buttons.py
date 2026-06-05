"""Цветные inline-кнопки Bot API 9.4+ (style на InlineKeyboardButton)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton


def styled_inline_button(
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    style: str | None = None,
) -> InlineKeyboardButton:
    kwargs: dict = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style is not None:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)
