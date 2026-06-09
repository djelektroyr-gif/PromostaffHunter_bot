"""Middleware: «печатает…» на всё время обработки update в личке."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from services.chat_feedback import MIN_MENU_TYPING_SEC, thread_id_from_message, typing_keepalive
from services.forum_topics import TOPIC_RESPONSES, TOPIC_SUPPORT, TOPIC_VACANCIES

# Кнопки reply-меню подписчика — минимальная задержка, чтобы индикатор был заметен.
_SUBSCRIBER_MENU_BUTTONS = frozenset({
    "🔍 Посмотреть новые вакансии",
    "📨 Мои отклики",
    "⚙️ Настройки",
    "📋 Категории",
    "📌 Категории вакансий",
    "🎯 Фильтры Premium",
    "📍 Станции метро",
    "📍 Мои районы",
    "◀️ В главное меню",
    "💎 Подписка",
    "👤 Мои данные",
    "📞 Мои контакты",
    "📖 Как пользоваться",
    "❓ Поддержка",
    "📋 Мои категории",
    "✏️ Изменить категории",
})

# Ответ уходит в forum-топик — typing показываем там же, иначе пользователь его не видит.
_MENU_BUTTON_TOPIC: dict[str, str] = {
    "❓ Поддержка": TOPIC_SUPPORT,
    "📨 Мои отклики": TOPIC_RESPONSES,
    "🔍 Посмотреть новые вакансии": TOPIC_VACANCIES,
}


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


def _event_user(event: TelegramObject) -> User | None:
    if isinstance(event, (Message, CallbackQuery)):
        return event.from_user
    return None


def _resolve_message_thread_id(event: TelegramObject, user_id: int) -> int | None:
    from config import FORUM_TOPICS_ENABLED
    from db import get_user_topic_thread_id

    if isinstance(event, Message):
        text = (event.text or "").strip()
        if FORUM_TOPICS_ENABLED and text in _MENU_BUTTON_TOPIC:
            thread_id = get_user_topic_thread_id(user_id, _MENU_BUTTON_TOPIC[text])
            if thread_id:
                return thread_id
        return thread_id_from_message(event)

    if isinstance(event, CallbackQuery):
        msg = event.message
        thread_id = thread_id_from_message(msg)
        if thread_id is not None:
            return thread_id
        data = (event.data or "").strip()
        if FORUM_TOPICS_ENABLED:
            if data.startswith(("feed_", "vac_page_", "vac_open_", "vac_collapse_")):
                return get_user_topic_thread_id(user_id, TOPIC_VACANCIES) or None
            if data.startswith("resp_"):
                return get_user_topic_thread_id(user_id, TOPIC_RESPONSES) or None
        return None

    return None


def _wants_min_typing(event: TelegramObject) -> bool:
    if isinstance(event, Message):
        text = (event.text or "").strip()
        if text in _SUBSCRIBER_MENU_BUTTONS:
            return True
        if text.startswith("/"):
            return True
    return False


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

        user = _event_user(event)
        thread_id = _resolve_message_thread_id(event, user.id) if user else None
        min_typing = _wants_min_typing(event)

        async with typing_keepalive(bot, chat_id, message_thread_id=thread_id):
            started = time.monotonic()
            result = await handler(event, data)
            if min_typing:
                elapsed = time.monotonic() - started
                if elapsed < MIN_MENU_TYPING_SEC:
                    await asyncio.sleep(MIN_MENU_TYPING_SEC - elapsed)
            return result
