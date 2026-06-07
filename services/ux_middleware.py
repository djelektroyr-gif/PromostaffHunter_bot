"""Middleware: «печатает…» на всё время обработки update в личке."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

from services.chat_feedback import typing_keepalive


def _private_chat_id(event: TelegramObject) -> int | None:
    if isinstance(event, CallbackQuery):
        msg = event.message
        if msg and msg.chat.type == ChatType.PRIVATE:
            return msg.chat.id
        return None
    if isinstance(event, Message):
        if (
            event.chat.type == ChatType.PRIVATE
            and event.from_user
            and not event.from_user.is_bot
        ):
            return event.chat.id
        return None
    return None


class ChatActivityMiddleware(BaseMiddleware):
    """Держит «печатает…», пока выполняется handler — callback и любой текст/команда в личке."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        bot: Bot = data["bot"]
        chat_id = _private_chat_id(event)
        if chat_id is None:
            return await handler(event, data)
        async with typing_keepalive(bot, chat_id):
            return await handler(event, data)
