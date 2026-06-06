"""Middleware: «печатает…» перед обработкой кнопок и пунктов меню."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Message, TelegramObject

from services.chat_feedback import send_typing


class ChatActivityMiddleware(BaseMiddleware):
    """Показывает typing до начала handler — и у пользователя, и у админа."""

    def __init__(self, user_menu_texts: set[str], admin_user_id: int) -> None:
        self._user_menu_texts = user_menu_texts
        self._admin_user_id = admin_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        bot: Bot = data["bot"]
        chat_id: int | None = None
        if isinstance(event, CallbackQuery) and event.message:
            chat_id = event.message.chat.id
        elif isinstance(event, Message) and event.text:
            uid = event.from_user.id if event.from_user else 0
            if event.text in self._user_menu_texts or uid == self._admin_user_id:
                chat_id = event.chat.id
        if chat_id is not None:
            await send_typing(bot, chat_id)
        return await handler(event, data)
