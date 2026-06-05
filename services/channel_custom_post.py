"""Произвольный пост админа в канал (новости, индустрия)."""

from __future__ import annotations

import logging
import uuid
from html import escape as escape_html
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardMarkup

from config import BOT_USERNAME, CHANNEL_CROSSPOST_ENABLED, HUNTER_CHANNEL_ID
from db import record_channel_post
from services.telegram_buttons import styled_inline_button

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)


def build_custom_post_keyboard(*, with_bot_button: bool = True) -> InlineKeyboardMarkup | None:
    if not with_bot_button:
        return None
    bot_user = (BOT_USERNAME or "PromostaffHunter_bot").lstrip("@")
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled_inline_button(
            "🚀 Открыть бота",
            url=f"https://t.me/{bot_user}?start=from_channel",
            style="success",
        )],
    ])


async def post_custom_to_channel(
    bot: Bot,
    *,
    text: str,
    photo_file_id: str | None = None,
    with_bot_button: bool = True,
) -> tuple[bool, str]:
    """Публикует пост; возвращает (ok, post_id или ошибка)."""
    if not CHANNEL_CROSSPOST_ENABLED or not HUNTER_CHANNEL_ID:
        return False, "Канал не настроен"
    body = (text or "").strip()
    if not body and not photo_file_id:
        return False, "Пустой пост"
    markup = build_custom_post_keyboard(with_bot_button=with_bot_button)
    try:
        if photo_file_id:
            msg = await bot.send_photo(
                HUNTER_CHANNEL_ID,
                photo_file_id,
                caption=body or None,
                parse_mode="HTML",
                reply_markup=markup,
            )
        else:
            msg = await bot.send_message(
                HUNTER_CHANNEL_ID,
                body,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        post_id = f"custom:{uuid.uuid4().hex[:12]}"
        preview = body[:120] if body else "(фото)"
        record_channel_post(
            post_id,
            post_kind="custom",
            category_code="news",
            message_id=msg.message_id,
            preview_text=preview,
        )
        logger.info("Channel custom post id=%s message_id=%s", post_id, msg.message_id)
        return True, post_id
    except Exception as e:
        logger.exception("channel custom post: %s", e)
        return False, str(e)


def format_custom_post_preview(text: str, *, with_bot_button: bool) -> str:
    btn_line = "🟢 Кнопка «Открыть бота» — да" if with_bot_button else "⚪ Без кнопки"
    preview = (text or "").strip() or "—"
    if len(preview) > 500:
        preview = preview[:500] + "…"
    return (
        "<b>📝 Превью поста в канал</b>\n\n"
        f"{preview}\n\n"
        f"{escape_html(btn_line)}"
    )
