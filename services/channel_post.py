"""Кросс-пост превью вакансий в канал @promostaff_agency_job."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardMarkup

from config import BOT_USERNAME, HUNTER_CHANNEL_ID
from services.channel_gate import is_vacancy_channel_autopost_enabled
from db import (
    is_vacancy_channel_posted,
    mark_vacancy_channel_posted,
    release_vacancy_channel_post,
    try_reserve_vacancy_channel_post,
)
from services.channel_images import (
    next_vacancy_image_variant_index,
    resolve_vacancy_image_path,
    send_channel_post,
)
from services.channel_policy import evaluate_channel_crosspost, format_skip_reason
from services.telegram_buttons import styled_inline_button
from services.vacancy_card import VacancyCardInput, _CHANNEL_NO_CONTACT_CTA, build_vacancy_preview_html, card_input_from_push_row
from services.vacancy_public_text import sanitize_vacancy_public_body

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)


def build_channel_preview_text(
    *,
    category_name: str,
    category_emoji: str,
    category_code: str = "promoter",
    body: str,
    source: str,
    freshness: str,
    address: str | None = None,
    address_normalized: str | None = None,
    geo_tags: list[str] | None = None,
    rate_hourly: int | None = None,
    rate_shift: int | None = None,
    shift_date: str | None = None,
    shift_time_start: str | None = None,
) -> str:
    del source  # не светим чужую группу в канале
    inp = VacancyCardInput(
        category_code=category_code,
        category_name=category_name,
        category_emoji=category_emoji,
        body=body or "",
        freshness=freshness,
        address=address,
        address_normalized=address_normalized,
        geo_tags=geo_tags,
        rate_hourly=rate_hourly,
        rate_shift=rate_shift,
        shift_date=shift_date,
        shift_time_start=shift_time_start,
    )
    return build_vacancy_preview_html(
        inp,
        show_published_at=False,
        show_employer_contact=False,
        no_contact_cta=_CHANNEL_NO_CONTACT_CTA,
    )


def build_channel_preview_keyboard(vacancy_id: str) -> InlineKeyboardMarkup:
    bot_user = (BOT_USERNAME or "PromostaffHunter_bot").lstrip("@")
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled_inline_button(
            "📋 Открыть в боте",
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
    if not is_vacancy_channel_autopost_enabled():
        return False
    from db import get_vacancy_push_row
    from parser import build_vacancy_dedupe_key, detect_duplicate_type

    row = get_vacancy_push_row(vacancy_id)
    author_contact = (row[3] if row else None) or ""
    if not force:
        dup_key = build_vacancy_dedupe_key(body or "", author_contact)
        dup_type = detect_duplicate_type(
            body or "",
            author_contact,
            dup_key,
            category_code,
            None,
            exclude_id=vacancy_id,
        )
        if dup_type:
            logger.info(
                "Channel skip vacancy_id=%s cat=%s content_duplicate=%s",
                vacancy_id,
                category_code,
                dup_type,
            )
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
    if row:
        inp = card_input_from_push_row(
            row,
            freshness=freshness,
            category_name=category_name,
            category_emoji=category_emoji,
            category_code=category_code,
        )
        text = build_vacancy_preview_html(
            inp,
            show_published_at=False,
            show_employer_contact=False,
        )
    else:
        text = build_channel_preview_text(
            category_name=category_name,
            category_emoji=category_emoji,
            category_code=category_code,
            body=body,
            source=source,
            freshness=freshness,
        )
    markup = build_channel_preview_keyboard(vacancy_id)
    variant_idx = next_vacancy_image_variant_index(category_code)
    photo_path = resolve_vacancy_image_path(
        category_code,
        vacancy_id,
        variant_index=variant_idx,
    )
    try:
        msg = await send_channel_post(
            bot,
            HUNTER_CHANNEL_ID,
            text=text,
            reply_markup=markup,
            photo_path=photo_path,
        )
        snippet = sanitize_vacancy_public_body(body or "", max_len=120)
        mark_vacancy_channel_posted(
            vacancy_id,
            category_code=category_code,
            message_id=msg.message_id,
            preview_text=snippet,
        )
        logger.info(
            "Channel cross-post vacancy_id=%s cat=%s image=%s",
            vacancy_id,
            category_code,
            photo_path.name if photo_path else None,
        )
        return True
    except Exception as e:
        if not force:
            release_vacancy_channel_post(vacancy_id)
        logger.exception("channel cross-post %s: %s", vacancy_id, e)
        return False
