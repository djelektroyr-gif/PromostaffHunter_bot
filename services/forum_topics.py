"""Forum topics в личке с ботом (Bot API 9.4+)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from db import get_user_topic_thread_id, save_user_topic_thread

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

TOPIC_VACANCIES = "vacancies"
TOPIC_RESPONSES = "responses"
TOPIC_SUPPORT = "support"

TOPIC_DEFINITIONS: tuple[tuple[str, str], ...] = (
    (TOPIC_VACANCIES, "📬 Вакансии"),
    (TOPIC_RESPONSES, "📨 Отклики"),
    (TOPIC_SUPPORT, "❓ Поддержка"),
)


async def ensure_user_forum_topics(bot: Bot, user_id: int) -> dict[str, int]:
    """Создаёт темы, если их ещё нет. Возвращает {topic_key: message_thread_id}."""
    result: dict[str, int] = {}
    for key, title in TOPIC_DEFINITIONS:
        existing = get_user_topic_thread_id(user_id, key)
        if existing:
            result[key] = existing
            continue
        try:
            topic = await bot.create_forum_topic(chat_id=user_id, name=title)
            thread_id = topic.message_thread_id
            save_user_topic_thread(user_id, key, thread_id)
            result[key] = thread_id
        except Exception as e:
            logger.warning("createForumTopic %s user=%s: %s", key, user_id, e)
            return result if result else {}
    return result


def topic_message_kwargs(user_id: int, topic_key: str | None) -> dict:
    """kwargs для send_message: message_thread_id или {}."""
    if not topic_key:
        return {}
    thread_id = get_user_topic_thread_id(user_id, topic_key)
    if thread_id:
        return {"message_thread_id": thread_id}
    return {}


def merge_send_kwargs(caller_kwargs: dict, topic_kwargs: dict) -> dict:
    """Слияние kwargs для send_message: маршрут в топик (topic_kwargs) важнее caller."""
    return {**caller_kwargs, **topic_kwargs}
