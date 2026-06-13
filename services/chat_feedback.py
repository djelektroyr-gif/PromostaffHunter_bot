"""Индикатор «печатает…» и keepalive для долгих операций в чате."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

TYPING_REFRESH_SEC = 4.0
# Короткий минимум, чтобы «печатает…» успевало появиться на быстрых кнопках меню.
MIN_MENU_TYPING_SEC = 0.35
# В личке с forum topics typing без thread_id не виден; General = 1 (Bot API quirk).
GENERAL_TOPIC_THREAD_ID = 1


def effective_typing_thread_id(message_thread_id: int | None) -> int | None:
    """thread_id для sendChatAction: в forum-личке без id — General (1)."""
    from config import FORUM_TOPICS_ENABLED

    if message_thread_id is not None:
        return message_thread_id
    if FORUM_TOPICS_ENABLED:
        return GENERAL_TOPIC_THREAD_ID
    return None


async def send_typing(
    bot: Bot,
    chat_id: int,
    action: str = "typing",
    *,
    message_thread_id: int | None = None,
) -> None:
    thread_id = effective_typing_thread_id(message_thread_id)
    try:
        kwargs: dict = {}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        await bot.send_chat_action(chat_id, action, **kwargs)
    except Exception:
        if thread_id is not None and thread_id != GENERAL_TOPIC_THREAD_ID:
            try:
                await bot.send_chat_action(chat_id, action)
            except Exception:
                pass


def thread_id_from_message(message: Message | None) -> int | None:
    if not message:
        return None
    if getattr(message, "is_topic_message", False) and message.message_thread_id:
        return message.message_thread_id
    return None


def topic_thread_id(user_id: int, topic_key: str) -> int | None:
    from config import FORUM_TOPICS_ENABLED

    if not FORUM_TOPICS_ENABLED:
        return None
    from db import get_user_topic_thread_id

    return get_user_topic_thread_id(user_id, topic_key) or None


def message_answer_injects_thread_id(message: Message | None) -> bool:
    """aiogram Message.answer() сам задаёт message_thread_id в forum-топике — не дублировать в **kwargs."""
    return bool(message and getattr(message, "is_topic_message", False))


@asynccontextmanager
async def typing_keepalive(
    bot: Bot,
    chat_id: int,
    action: str = "typing",
    *,
    message_thread_id: int | None = None,
):
    """Держит «печатает…» на экране, пока идёт долгая операция (>5 с)."""
    stop = asyncio.Event()

    async def _loop() -> None:
        while not stop.is_set():
            await send_typing(bot, chat_id, action, message_thread_id=message_thread_id)
            try:
                await asyncio.wait_for(stop.wait(), timeout=TYPING_REFRESH_SEC)
            except asyncio.TimeoutError:
                continue

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
