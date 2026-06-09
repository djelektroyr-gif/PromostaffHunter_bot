"""Digest после тихих часов / «занят» и фоновая проверка выхода из паузы."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import (
    clear_push_digest_pending,
    count_push_digest_pending,
    get_subscriber_filter_prefs_effective,
    get_subscriber_filter_prefs_raw,
    list_active_premium_user_ids,
    patch_subscriber_notify_prefs,
)
from services.filter_prefs import normalize_prefs
from services.push_notify import is_push_blocked, parse_paused_until

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

PUSH_DIGEST_CHECK_INTERVAL_SEC = 60
PUSH_DIGEST_NOTIFY_DELAY_SEC = 0.08


def format_push_digest_message(count: int) -> str:
    n = max(count, 0)
    if n == 1:
        body = "опубликована *1* подходящая вакансия"
    elif 2 <= n <= 4:
        body = f"опубликовано *{n}* подходящие вакансии"
    else:
        body = f"опубликовано *{n}* подходящих вакансий"
    return (
        f"🔔 *Push снова включён*\n\n"
        f"Пока уведомления были выключены, {body} — смотрите в ленте."
    )


def build_push_digest_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Открыть ленту", callback_data="feed_pick_mode")],
    ])


async def send_push_digest_if_pending(bot: Bot, user_id: int) -> bool:
    """Отправляет digest и очищает очередь. False если нечего отправлять."""
    prefs = get_subscriber_filter_prefs_effective(user_id)
    if not prefs:
        clear_push_digest_pending(user_id)
        return False
    prefs = normalize_prefs(prefs)
    if not prefs.get("notify", {}).get("digest_after_pause", True):
        clear_push_digest_pending(user_id)
        return False
    count = count_push_digest_pending(user_id)
    if count <= 0:
        return False
    try:
        from config import FORUM_TOPICS_ENABLED
        from services.forum_topics import TOPIC_VACANCIES, topic_message_kwargs

        extra = {}
        if FORUM_TOPICS_ENABLED:
            extra = topic_message_kwargs(user_id, TOPIC_VACANCIES)
        await bot.send_message(
            user_id,
            format_push_digest_message(count),
            parse_mode="Markdown",
            reply_markup=build_push_digest_keyboard(),
            **extra,
        )
        clear_push_digest_pending(user_id)
        return True
    except Exception as e:
        err = str(e).lower()
        if "bot was blocked" in err or "user is deactivated" in err:
            logger.info("push digest skip user=%s: blocked/deactivated", user_id)
            clear_push_digest_pending(user_id)
        else:
            logger.warning("push digest user=%s: %s", user_id, e)
        return False


async def resume_push_notifications(bot: Bot, user_id: int) -> bool:
    """Digest и отложенные «вакансия закрыта» после выхода из quiet/busy."""
    from services.vacancy_closed_notify import send_pending_closed_notices

    closed_sent = await send_pending_closed_notices(bot, user_id)
    digest_sent = await send_push_digest_if_pending(bot, user_id)
    if digest_sent:
        await asyncio.sleep(PUSH_DIGEST_NOTIFY_DELAY_SEC)
    return closed_sent > 0 or digest_sent


async def process_push_digest_transitions(bot: Bot) -> int:
    """Проверяет выход из quiet/busy и шлёт digest."""
    sent = 0
    for user_id in list_active_premium_user_ids():
        prefs = get_subscriber_filter_prefs_raw(user_id)
        if not prefs:
            continue
        prefs = normalize_prefs(prefs)
        notify = prefs.get("notify") or {}
        if notify.get("paused_until"):
            until = parse_paused_until(notify.get("paused_until"))
            if until and datetime.now(timezone.utc) >= until:
                prefs = patch_subscriber_notify_prefs(user_id, {"paused_until": None})
                notify = prefs.get("notify") or {}
        blocked = is_push_blocked(prefs)
        was_blocked = bool(notify.get("push_block_was_active"))
        if was_blocked and not blocked:
            if await resume_push_notifications(bot, user_id):
                sent += 1
        if was_blocked != blocked:
            patch_subscriber_notify_prefs(user_id, {"push_block_was_active": blocked})

    if sent:
        logger.info("Push digest messages sent: %d", sent)
    return sent


async def push_digest_scheduler_loop(bot: Bot):
    await asyncio.sleep(45)
    while True:
        try:
            await process_push_digest_transitions(bot)
        except Exception as e:
            logger.exception("push_digest_scheduler_loop: %s", e)
        await asyncio.sleep(PUSH_DIGEST_CHECK_INTERVAL_SEC)
