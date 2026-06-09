"""Обновляет last_seen_at при активности в личке."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

from config import YOUR_USER_ID
from db import touch_subscriber_last_seen


def _activity_user_id(event: TelegramObject) -> int | None:
    if isinstance(event, CallbackQuery):
        msg = event.message
        if event.from_user and msg and msg.chat.type == ChatType.PRIVATE:
            return event.from_user.id
        return None
    if isinstance(event, Message):
        if (
            event.chat.type == ChatType.PRIVATE
            and event.from_user
            and not event.from_user.is_bot
        ):
            return event.from_user.id
    return None


class SubscriberActivityMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = _activity_user_id(event)
        if user_id and user_id != YOUR_USER_ID:
            try:
                touch_subscriber_last_seen(user_id)
            except Exception:
                pass
        return await handler(event, data)
