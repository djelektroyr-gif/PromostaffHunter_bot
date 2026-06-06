"""Фоновые напоминания и downgrade истёкшего Premium/Trial."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import PREMIUM_RENEWAL_REMIND_DAYS, SUBSCRIPTION_PRICE_RUB, SUBSCRIPTION_PAY_URL
from datetime import timezone

from db import (
    _parse_paid_until,
    downgrade_expired_premium,
    list_expired_premium_user_ids,
    list_premium_renewal_reminder_candidates,
    mark_premium_renewal_warned,
)

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

PREMIUM_CHECK_INTERVAL_SEC = 3600
PREMIUM_NOTIFY_DELAY_SEC = 0.12


def format_premium_renewal_reminder(
    *,
    days_left: int,
    trial_used: bool,
    paid_until,
) -> str:
    until = _parse_paid_until(paid_until)
    until_line = ""
    if until:
        until_line = until.astimezone(timezone.utc).strftime("%d.%m.%Y")
    if trial_used and days_left <= 7:
        heading = "⏳ *Пробный Premium скоро закончится*"
        tail = (
            "После окончания push отключится, *категории сбросятся* — на Free останется "
            "одна категория в «⚙️ Настройки».\n"
            f"Сохранить несколько категорий и push — Premium *{SUBSCRIPTION_PRICE_RUB} ₽/мес* "
            "(кнопка ниже или «💎 Подписка»)."
        )
    else:
        heading = "⏳ *Premium скоро закончится*"
        tail = (
            "Push и фильтр метро отключатся; без оплаты категории сбросятся.\n"
            "Продлите заранее — кнопка ниже или «💎 Подписка» в меню."
        )
    when = f"Осталось *{days_left}* дн."
    if until_line:
        when += f" (до {until_line})"
    return f"{heading}\n\n{when}\n\n{tail}"


def build_renewal_reminder_markup(*, is_trial_period: bool) -> InlineKeyboardMarkup:
    rows = []
    if SUBSCRIPTION_PAY_URL:
        label = "💳 Оплатить Premium" if is_trial_period else "💳 Продлить Premium"
        rows.append([InlineKeyboardButton(text=label, url=SUBSCRIPTION_PAY_URL)])
    cb = "subscription_request" if is_trial_period else "subscription_renew"
    label = "📩 Запросить Premium" if is_trial_period else "📩 Запросить продление"
    rows.append([InlineKeyboardButton(text=label, callback_data=cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def process_premium_renewal_reminders(bot: Bot, *, within_days: int | None = None) -> int:
    days = PREMIUM_RENEWAL_REMIND_DAYS if within_days is None else within_days
    if days <= 0:
        return 0
    sent = 0
    for row in list_premium_renewal_reminder_candidates(days):
        user_id = row["user_id"]
        paid_until = row["paid_until"]
        text = format_premium_renewal_reminder(
            days_left=row["days_left"],
            trial_used=row["trial_used"],
            paid_until=paid_until,
        )
        is_trial = row["trial_used"] and row["days_left"] <= 7
        markup = build_renewal_reminder_markup(is_trial_period=is_trial)
        try:
            await bot.send_message(
                user_id,
                text,
                parse_mode="Markdown",
                reply_markup=markup,
            )
            mark_premium_renewal_warned(user_id, paid_until)
            sent += 1
        except Exception as e:
            err = str(e).lower()
            if "bot was blocked" in err or "user is deactivated" in err:
                logger.info("premium reminder skip user=%s: blocked/deactivated", user_id)
            else:
                logger.warning("premium reminder user=%s: %s", user_id, e)
        await asyncio.sleep(PREMIUM_NOTIFY_DELAY_SEC)
    if sent:
        logger.info("Premium renewal reminders sent: %d", sent)
    return sent


async def process_expired_premium_downgrades(bot: Bot) -> int:
    notified = 0
    for user_id in list_expired_premium_user_ids():
        msg = downgrade_expired_premium(user_id)
        if not msg:
            continue
        try:
            await bot.send_message(user_id, msg, parse_mode="Markdown")
            notified += 1
        except Exception as e:
            err = str(e).lower()
            if "bot was blocked" in err or "user is deactivated" in err:
                logger.info("premium expired notify skip user=%s: blocked/deactivated", user_id)
            else:
                logger.warning("premium expired notify user=%s: %s", user_id, e)
        await asyncio.sleep(PREMIUM_NOTIFY_DELAY_SEC)
    if notified:
        logger.info("Premium expired proactive notifications: %d", notified)
    return notified


async def premium_scheduler_loop(bot: Bot):
    """Раз в час: напоминание за N дней и downgrade + уведомление об истечении."""
    await asyncio.sleep(30)
    while True:
        try:
            await process_expired_premium_downgrades(bot)
            await process_premium_renewal_reminders(bot)
        except Exception as e:
            logger.exception("premium_scheduler_loop: %s", e)
        await asyncio.sleep(PREMIUM_CHECK_INTERVAL_SEC)
