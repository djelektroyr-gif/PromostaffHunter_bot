"""Кросс-пост превью вакансий в канал @promostaff_agency_job."""

from __future__ import annotations

import logging
from html import escape as escape_html
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import BOT_USERNAME, CHANNEL_CROSSPOST_ENABLED, HUNTER_CHANNEL_ID
from db import is_vacancy_channel_posted, mark_vacancy_channel_posted
from services.vacancy_public_text import sanitize_vacancy_public_body

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)


def build_channel_preview_text(
    *,
    category_name: str,
    category_emoji: str,
    body: str,
    source: str,
    freshness: str,
) -> str:
    del source  # не светим чужую группу в канале
    snippet = sanitize_vacancy_public_body(body or "", max_len=320)
    if not snippet:
        snippet = "Подробности и отклик — в боте (кнопка ниже)."
    return (
        f"{category_emoji} <b>{escape_html(category_name)}</b> · {escape_html(freshness)}\n\n"
        f"{escape_html(snippet)}"
    )


def build_channel_preview_keyboard(vacancy_id: str) -> InlineKeyboardMarkup:
    bot_user = (BOT_USERNAME or "PromostaffHunter_bot").lstrip("@")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Подробнее в боте",
            url=f"https://t.me/{bot_user}?start=vac_{vacancy_id}",
        )],
        [InlineKeyboardButton(
            text="Разместить вакансию",
            url=f"https://t.me/{bot_user}?start=employer",
        )],
    ])


async def post_vacancy_preview_to_channel(
    bot: Bot,
    *,
    vacancy_id: str,
    category_name: str,
    category_emoji: str,
    body: str,
    source: str,
    freshness: str,
    force: bool = False,
) -> bool:
    if not CHANNEL_CROSSPOST_ENABLED or not HUNTER_CHANNEL_ID:
        return False
    if not force and is_vacancy_channel_posted(vacancy_id):
        return False
    text = build_channel_preview_text(
        category_name=category_name,
        category_emoji=category_emoji,
        body=body,
        source=source,
        freshness=freshness,
    )
    markup = build_channel_preview_keyboard(vacancy_id)
    try:
        await bot.send_message(
            HUNTER_CHANNEL_ID,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        mark_vacancy_channel_posted(vacancy_id)
        logger.info("Channel cross-post vacancy_id=%s", vacancy_id)
        return True
    except Exception as e:
        logger.exception("channel cross-post %s: %s", vacancy_id, e)
        return False
