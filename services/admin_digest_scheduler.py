"""Утренний дайджест (10:00 МСК) и проверка зависших регистраций."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from config import YOUR_USER_ID
from db import get_scheduler_flag, get_stuck_registrations, mark_reg_stuck_notified, set_scheduler_flag
from services.admin_activity_digest import build_activity_digest_html
from services.admin_ops_alerts import notify_admin_registration_stuck
from services.channel_policy import msk_now

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

ADMIN_DIGEST_HOUR_MSK = 10
CHECK_INTERVAL_SEC = 55
STUCK_CHECK_HOUR_MSK = 10


def _digest_flag_key(date_str: str) -> str:
    return f"admin_digest_sent:{date_str}"


def admin_digest_due_now(now=None) -> bool:
    now = now or msk_now()
    if now.hour != ADMIN_DIGEST_HOUR_MSK or now.minute != 0:
        return False
    date_str = now.strftime("%Y-%m-%d")
    return get_scheduler_flag(_digest_flag_key(date_str)) != date_str


async def send_admin_daily_digest(bot: Bot) -> bool:
    if not YOUR_USER_ID:
        return False
    now = msk_now()
    date_str = now.strftime("%Y-%m-%d")
    text = build_activity_digest_html(hours=24, title_prefix="☀️ Дайджест")
    try:
        await bot.send_message(
            YOUR_USER_ID,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        set_scheduler_flag(_digest_flag_key(date_str), date_str)
        logger.info("Admin daily digest sent for %s", date_str)
        return True
    except Exception as e:
        logger.exception("send_admin_daily_digest: %s", e)
        return False


async def process_stuck_registrations(bot: Bot) -> int:
    rows = get_stuck_registrations(older_than_hours=24)
    sent = 0
    for row in rows:
        await notify_admin_registration_stuck(bot, row)
        mark_reg_stuck_notified(row["user_id"])
        sent += 1
        await asyncio.sleep(0.05)
    if sent:
        logger.info("Stuck registration alerts sent: %d", sent)
    return sent


async def admin_digest_scheduler_loop(bot: Bot):
    await asyncio.sleep(30)
    while True:
        try:
            if admin_digest_due_now():
                await send_admin_daily_digest(bot)
                await process_stuck_registrations(bot)
            else:
                now = msk_now()
                if now.hour == STUCK_CHECK_HOUR_MSK and now.minute == 30:
                    flag = f"stuck_check:{now.strftime('%Y-%m-%d-%H')}"
                    if get_scheduler_flag(flag) != "1":
                        await process_stuck_registrations(bot)
                        set_scheduler_flag(flag, "1")
        except Exception as e:
            logger.exception("admin_digest_scheduler_loop: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_SEC)
