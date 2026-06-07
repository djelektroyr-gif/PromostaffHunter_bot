"""Промо-посты в канал: подписка на бота по расписанию."""

from __future__ import annotations

import logging
from html import escape as escape_html
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardMarkup

from config import BOT_USERNAME, CHANNEL_CROSSPOST_ENABLED, HUNTER_CHANNEL_ID
from db import (
    get_channel_promo_times,
    is_channel_promo_enabled,
    is_promo_sent_for_msk_date,
    record_channel_post,
    release_promo_slot,
    try_reserve_promo_slot,
)
from services.channel_images import resolve_promo_image_path, send_channel_post
from services.channel_policy import msk_now
from services.channel_promo_texts import pick_promo_text
from services.telegram_buttons import styled_inline_button

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

def build_channel_promo_keyboard() -> InlineKeyboardMarkup:
    bot_user = (BOT_USERNAME or "PromostaffHunter_bot").lstrip("@")
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled_inline_button(
            "🚀 Открыть бота",
            url=f"https://t.me/{bot_user}?start=from_channel",
            style="success",
        )],
        [styled_inline_button(
            "💎 Premium и категории",
            url=f"https://t.me/{bot_user}?start=subscribe",
            style="primary",
        )],
        [styled_inline_button(
            "📤 Разместить вакансию",
            url=f"https://t.me/{bot_user}?start=employer",
        )],
    ])


async def post_channel_promo(bot: Bot, *, slot: str | None = None, variant_index: int | None = None, manual: bool = False) -> bool:
    if not CHANNEL_CROSSPOST_ENABLED or not HUNTER_CHANNEL_ID:
        return False
    if not manual and not is_channel_promo_enabled():
        return False
    now = msk_now()
    promo_slot = slot or now.strftime("%H:%M")
    sent_date = now.strftime("%Y-%m-%d")
    if slot and not manual and is_promo_sent_for_msk_date(promo_slot, sent_date):
        return False
    reserved = False
    if slot and not manual:
        if not try_reserve_promo_slot(promo_slot, sent_date):
            return False
        reserved = True
    times = get_channel_promo_times()
    idx = variant_index
    if idx is None:
        idx = times.index(promo_slot) if promo_slot in times else 0
    text = pick_promo_text(idx)
    markup = build_channel_promo_keyboard()
    photo_path = resolve_promo_image_path(idx)
    try:
        msg = await send_channel_post(
            bot,
            HUNTER_CHANNEL_ID,
            text=text,
            reply_markup=markup,
            photo_path=photo_path,
        )
        if slot and not manual and reserved:
            pass  # already reserved before send
        post_key = (
            f"promo:manual:{msg.message_id}"
            if manual
            else f"promo:{sent_date}:{promo_slot}"
        )
        record_channel_post(
            post_key,
            post_kind="promo",
            category_code="promo",
            message_id=msg.message_id,
            preview_text=text[:120],
        )
        logger.info("Channel promo posted slot=%s date=%s", promo_slot, sent_date)
        return True
    except Exception as e:
        if reserved:
            release_promo_slot(promo_slot, sent_date)
        logger.exception("channel promo slot=%s: %s", promo_slot, e)
        return False


def promo_slot_due_now(now=None) -> str | None:
    """Слот HH:MM если сейчас минута запуска промо (±0 мин)."""
    if not is_channel_promo_enabled():
        return None
    now = now or msk_now()
    current = now.strftime("%H:%M")
    if current in get_channel_promo_times():
        sent_date = now.strftime("%Y-%m-%d")
        if not is_promo_sent_for_msk_date(current, sent_date):
            return current
    return None


async def channel_promo_scheduler_loop(bot: Bot):
    import asyncio

    while True:
        try:
            slot = promo_slot_due_now()
            if slot:
                await post_channel_promo(bot, slot=slot)
        except Exception as e:
            logger.exception("channel_promo_scheduler: %s", e)
        await asyncio.sleep(55)
