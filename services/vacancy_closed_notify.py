"""Доставка уведомлений «вакансия закрыта» с учётом тихих часов и «занят»."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import (
    add_closed_notice_pending,
    get_subscriber_filter_prefs_effective,
    get_vacancy_push_row,
    list_closed_notice_pending,
    remove_closed_notice_pending,
)
from services.push_notify import is_push_blocked
from services.vacancy_closed_notice import format_closed_vacancy_notice_html

logger = logging.getLogger(__name__)

SEND_DELAY_SEC = 0.08

_CATEGORY_EMOJI = {
    "promoter": "📢", "hostess": "👩‍💼", "wardrobe": "🧥", "animator": "🎭",
    "helper": "👷", "loader": "📦", "waiter": "🍽️", "driver": "🚐",
    "security": "🛡️", "parking": "🚗", "supervisor": "👨‍💼",
}
_CATEGORY_NAME = {
    "promoter": "Промоутер", "hostess": "Хостес", "wardrobe": "Гардеробщик",
    "animator": "Аниматор", "helper": "Хелпер", "loader": "Грузчик",
    "waiter": "Официант", "driver": "Водитель", "security": "Охранник",
    "parking": "Парковщик", "supervisor": "Супервайзер",
}


def build_closed_vacancy_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Мои отклики", callback_data="resp_list_0")],
    ])


def build_closed_vacancy_notice(vacancy_id: str) -> tuple[str, InlineKeyboardMarkup]:
    row = get_vacancy_push_row(vacancy_id)
    if not row:
        text = (
            "🔒 <b>Вакансия закрыта</b>\n\n"
            "Смена, на которую вы откликались или получали push, больше не актуальна.\n"
            "Подробности — в «📨 Мои отклики»."
        )
        return text, build_closed_vacancy_markup()
    message_text = row[0]
    source_chat_title = row[2]
    address = row[4]
    category_code = row[5] or "promoter"
    text = format_closed_vacancy_notice_html(
        category_emoji=_CATEGORY_EMOJI.get(category_code, "📌"),
        category_name=_CATEGORY_NAME.get(category_code, category_code),
        body=message_text,
        address=address,
        source_chat_title=source_chat_title,
    )
    return text, build_closed_vacancy_markup()


async def send_closed_notice(bot: Bot, user_id: int, vacancy_id: str) -> bool:
    text, markup = build_closed_vacancy_notice(vacancy_id)
    try:
        await bot.send_message(
            user_id,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:
        logger.error("Не удалось уведомить пользователя %s о закрытии %s: %s", user_id, vacancy_id, e)
        return False


def should_defer_closed_notice(user_id: int) -> bool:
    prefs = get_subscriber_filter_prefs_effective(user_id)
    if not prefs:
        return False
    return is_push_blocked(prefs)


async def deliver_closed_vacancy_notices(bot: Bot, closed_data: list) -> None:
    sent = 0
    deferred = 0
    for vacancy_id, user_ids in closed_data:
        if not vacancy_id or not user_ids:
            continue
        for uid in user_ids:
            if should_defer_closed_notice(uid):
                if add_closed_notice_pending(uid, vacancy_id):
                    deferred += 1
                continue
            if await send_closed_notice(bot, uid, vacancy_id):
                sent += 1
            await asyncio.sleep(SEND_DELAY_SEC)
    if sent or deferred:
        logger.info("closed vacancy notices: sent=%d deferred=%d", sent, deferred)


async def send_pending_closed_notices(bot: Bot, user_id: int) -> int:
    """Отправляет отложенные «закрыта» после выхода из тихих часов / «занят»."""
    vacancy_ids = list_closed_notice_pending(user_id)
    if not vacancy_ids:
        return 0
    sent = 0
    for vacancy_id in vacancy_ids:
        if await send_closed_notice(bot, user_id, vacancy_id):
            remove_closed_notice_pending(user_id, vacancy_id)
            sent += 1
            await asyncio.sleep(SEND_DELAY_SEC)
    if sent:
        logger.info("pending closed notices sent user=%s count=%d", user_id, sent)
    return sent
