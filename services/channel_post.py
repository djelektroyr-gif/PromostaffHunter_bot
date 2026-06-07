"""Кросс-пост превью вакансий в канал @promostaff_agency_job."""

from __future__ import annotations

import logging
from html import escape as escape_html
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardMarkup

from config import BOT_USERNAME, CHANNEL_CROSSPOST_ENABLED, HUNTER_CHANNEL_ID
from db import (
    is_vacancy_channel_posted,
    mark_vacancy_channel_posted,
    release_vacancy_channel_post,
    try_reserve_vacancy_channel_post,
)
from services.channel_policy import evaluate_channel_crosspost, format_skip_reason
from services.telegram_buttons import styled_inline_button
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
        [styled_inline_button(
            "✋ Подробнее в боте",
            url=f"https://t.me/{bot_user}?start=vac_{vacancy_id}",
            style="success",
        )],
        [styled_inline_button(
            "📤 Разместить вакансию",
            url=f"https://t.me/{bot_user}?start=employer",
            style="primary",
        )],
    ])


async def post_vacancy_preview_to_channel(
    bot: Bot,
    *,
    vacancy_id: str,
    category_code: str,
    category_name: str,
    category_emoji: str,
    body: str,
    source: str,
    freshness: str,
    force: bool = False,
) -> bool:
    if not CHANNEL_CROSSPOST_ENABLED or not HUNTER_CHANNEL_ID:
        return False
    already = is_vacancy_channel_posted(vacancy_id)
    allowed, reason = evaluate_channel_crosspost(
        category_code,
        body,
        force=force,
        already_posted=already and not force,
    )
    if not allowed:
        logger.info(
            "Channel skip vacancy_id=%s cat=%s reason=%s",
            vacancy_id,
            category_code,
            format_skip_reason(reason),
        )
        return False
    if not force and not try_reserve_vacancy_channel_post(vacancy_id, category_code):
        logger.info("Channel skip vacancy_id=%s — slot reserved or already posted", vacancy_id)
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
        msg = await bot.send_message(
            HUNTER_CHANNEL_ID,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
        snippet = sanitize_vacancy_public_body(body or "", max_len=120)
        mark_vacancy_channel_posted(
            vacancy_id,
            category_code=category_code,
            message_id=msg.message_id,
            preview_text=snippet,
        )
        logger.info("Channel cross-post vacancy_id=%s cat=%s", vacancy_id, category_code)
        return True
    except Exception as e:
        if not force:
            release_vacancy_channel_post(vacancy_id)
        logger.exception("channel cross-post %s: %s", vacancy_id, e)
        return False
