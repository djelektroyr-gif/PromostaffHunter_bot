"""Кросс-пост вакансии в канал — отдельно от push подписчикам."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from aiogram import Bot

from services.channel_gate import is_vacancy_channel_autopost_enabled

logger = logging.getLogger(__name__)


def schedule_vacancy_channel_crosspost(
    spawn_task: Callable[..., Any],
    bot: Bot,
    *,
    order: dict,
    vacancy_id: str,
    category_code: str,
    category_name: str,
    category_emoji: str,
    body: str,
    freshness: str,
) -> bool:
    """
    Запланировать автопост в @promostaff_agency_job.
    Не зависит от того, ушёл ли push Premium-подписчикам.
  """
    if not is_vacancy_channel_autopost_enabled():
        logger.info(
            "Channel crosspost skip vacancy_id=%s: autopost disabled (env или настройка бота)",
            vacancy_id,
        )
        return False
    from services.channel_post import post_vacancy_preview_to_channel

    spawn_task(
        post_vacancy_preview_to_channel(
            bot,
            vacancy_id=vacancy_id,
            category_code=category_code,
            category_name=category_name,
            category_emoji=category_emoji,
            body=body,
            source=order.get("chat_title") or "—",
            freshness=freshness,
        )
    )
    logger.info("Channel crosspost scheduled vacancy_id=%s cat=%s", vacancy_id, category_code)
    return True
