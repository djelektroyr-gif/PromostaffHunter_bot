"""Forum topics в личке с ботом (Bot API 9.4+)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramBadRequest

from db import delete_user_topic_thread, get_user_topic_thread_id, save_user_topic_thread

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

_TOPIC_TITLES = dict(TOPIC_DEFINITIONS)


def is_forum_thread_missing_error(exc: BaseException) -> bool:
    """Telegram: «message thread not found» — тема удалена или устарел thread_id в БД."""
    if not isinstance(exc, TelegramBadRequest):
        return False
    err = str(exc).lower()
    return "thread" in err and "not found" in err


async def recreate_user_topic(bot: Bot, user_id: int, topic_key: str) -> int | None:
    """Сбрасывает устаревший thread_id и создаёт тему заново."""
    title = _TOPIC_TITLES.get(topic_key, topic_key)
    delete_user_topic_thread(user_id, topic_key)
    try:
        topic = await bot.create_forum_topic(chat_id=user_id, name=title)
        thread_id = int(topic.message_thread_id)
        save_user_topic_thread(user_id, topic_key, thread_id)
        logger.info("forum topic recreated user=%s key=%s thread=%s", user_id, topic_key, thread_id)
        return thread_id
    except Exception as e:
        logger.warning("recreate_user_topic failed user=%s key=%s: %s", user_id, topic_key, e)
        return None


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
