"""Одна актуальная вакансия в General — предыдущие уходят в топик «Вакансии»."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup

from db import (
    clear_general_vacancy_pin,
    get_general_vacancy_pin,
    get_user_topic_thread_id,
    set_general_vacancy_pin,
)
from services.forum_topics import TOPIC_VACANCIES, topic_message_kwargs

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)


async def _send_html_card(
    bot: Bot,
    user_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    *,
    topic_key: str | None,
) -> int | None:
    extra = topic_message_kwargs(user_id, topic_key) if topic_key else {}
    try:
        msg = await bot.send_message(
            user_id,
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
            **extra,
        )
        return msg.message_id
    except TelegramBadRequest as e:
        err = str(e).lower()
        if extra.get("message_thread_id") and ("thread" in err or "topic" in err or "not found" in err):
            logger.warning("forum_vacancy_pin: topic miss user=%s key=%s", user_id, topic_key)
            msg = await bot.send_message(
                user_id,
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return msg.message_id
        if "parse" in err:
            plain = re.sub(r"<[^>]*>", "", text)
            msg = await bot.send_message(
                user_id,
                plain,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
                **extra,
            )
            return msg.message_id
        raise


async def archive_general_vacancy_to_topic(
    bot: Bot,
    user_id: int,
    *,
    rebuild_keyboard,
) -> None:
    """Переносит закреплённую в General карточку в топик «Вакансии» и удаляет из General."""
    pin = get_general_vacancy_pin(user_id)
    if not pin:
        return
    message_id = pin["message_id"]
    vacancy_id = pin["vacancy_id"]
    card_text = pin["card_text"]
    if get_user_topic_thread_id(user_id, TOPIC_VACANCIES):
        try:
            keyboard = rebuild_keyboard(vacancy_id)
            await _send_html_card(
                bot,
                user_id,
                card_text,
                keyboard,
                topic_key=TOPIC_VACANCIES,
            )
        except Exception as e:
            logger.warning("archive vacancy to topic user=%s vac=%s: %s", user_id, vacancy_id, e)
    try:
        await bot.delete_message(chat_id=user_id, message_id=message_id)
    except TelegramBadRequest as e:
        if "message to delete not found" not in str(e).lower():
            logger.debug("delete general vacancy pin user=%s msg=%s: %s", user_id, message_id, e)
    clear_general_vacancy_pin(user_id)


async def send_vacancy_push_pinned_general(
    bot: Bot,
    user_id: int,
    vacancy_id: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    *,
    rebuild_keyboard,
    ensure_topics,
) -> bool:
    """Push: одна карточка в General; предыдущая → топик «Вакансии»."""
    await ensure_topics(user_id)
    await archive_general_vacancy_to_topic(bot, user_id, rebuild_keyboard=rebuild_keyboard)
    msg_id = await _send_html_card(bot, user_id, text, reply_markup, topic_key=None)
    if msg_id is None:
        return False
    set_general_vacancy_pin(user_id, msg_id, vacancy_id, text)
    return True
