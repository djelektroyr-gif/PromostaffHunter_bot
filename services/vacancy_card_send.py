"""Доставка карточки вакансии: Rich Message (10.1) с fallback на HTML."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from config import FORUM_TOPICS_ENABLED, RICH_VACANCY_CARDS_ENABLED
from db import get_user_topic_thread_id
from services.vacancy_card import VacancyCardInput
from services.vacancy_card_rich import (
    build_vacancy_card_html_fallback,
    build_vacancy_card_rich_html,
)

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)


def _plain_from_html(text: str) -> str:
    return re.sub(r"<[^>]*>", "", text)


async def _bot_send_with_retry(bot: Bot, chat_id: int, **kwargs) -> Message:
    for attempt in range(2):
        try:
            return await bot.send_message(chat_id, **kwargs)
        except TelegramRetryAfter as e:
            if attempt == 0:
                await asyncio.sleep(int(e.retry_after) + 1)
                continue
            raise
        except Exception as e:
            err = str(e).lower()
            if attempt == 0 and "retry after" in err:
                m = re.search(r"retry after (\d+)", err)
                wait = int(m.group(1)) + 1 if m else 3
                await asyncio.sleep(wait)
                continue
            raise
    raise RuntimeError("send_message retry exhausted")


async def _ensure_topic(chat_id: int, topic_key: str | None, bot: Bot) -> dict:
    if not topic_key or not FORUM_TOPICS_ENABLED:
        return {}
    if not get_user_topic_thread_id(chat_id, topic_key):
        from services.forum_topics import ensure_user_forum_topics

        try:
            await ensure_user_forum_topics(bot, chat_id)
        except Exception as exc:
            logger.warning("forum topics setup user=%s: %s", chat_id, exc)
    from services.forum_topics import topic_message_kwargs

    return topic_message_kwargs(chat_id, topic_key)


async def _send_html_vacancy_card(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    extra: dict,
) -> Message:
    try:
        return await _bot_send_with_retry(
            bot,
            chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
            **extra,
        )
    except TelegramBadRequest as e:
        if "parse" in str(e).lower():
            return await _bot_send_with_retry(
                bot,
                chat_id,
                text=_plain_from_html(text),
                reply_markup=reply_markup,
                disable_web_page_preview=True,
                **extra,
            )
        raise


async def send_vacancy_card_message(
    bot: Bot,
    chat_id: int,
    inp: VacancyCardInput,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    expanded: bool = False,
    topic_key: str | None = "vacancies",
    html_suffix: str | None = None,
) -> Message:
    """Отправка карточки: rich при возможности, иначе обычный HTML."""
    extra = await _ensure_topic(chat_id, topic_key, bot)

    rich_html = build_vacancy_card_rich_html(inp, expanded=expanded)
    if html_suffix:
        rich_html += html_suffix
    html_fallback = build_vacancy_card_html_fallback(inp, expanded=expanded)
    if html_suffix:
        html_fallback += html_suffix

    if RICH_VACANCY_CARDS_ENABLED:
        from services.telegram_rich_message import send_rich_message_html

        thread_id = extra.get("message_thread_id")
        try:
            return await send_rich_message_html(
                bot,
                chat_id,
                rich_html,
                message_thread_id=thread_id,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as exc:
            err = str(exc).lower()
            if extra.get("message_thread_id") and ("thread" in err or "topic" in err or "not found" in err):
                logger.warning(
                    "send_vacancy_card rich: тема недоступна user=%s, fallback general",
                    chat_id,
                )
                extra = {}
                try:
                    return await send_rich_message_html(
                        bot,
                        chat_id,
                        rich_html,
                        reply_markup=reply_markup,
                    )
                except TelegramBadRequest as exc2:
                    logger.warning("sendRichMessage failed, HTML fallback: %s", exc2)
            else:
                logger.warning("sendRichMessage failed, HTML fallback: %s", exc)
        except Exception as exc:
            logger.warning("sendRichMessage error, HTML fallback: %s", exc)

    try:
        return await _send_html_vacancy_card(bot, chat_id, html_fallback, reply_markup, extra)
    except TelegramBadRequest as e:
        err = str(e).lower()
        if extra.get("message_thread_id") and ("thread" in err or "topic" in err or "not found" in err):
            logger.warning("send_vacancy_card: тема недоступна user=%s, fallback в общий чат", chat_id)
            return await _send_html_vacancy_card(bot, chat_id, html_fallback, reply_markup, {})
        raise


async def edit_vacancy_card_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    inp: VacancyCardInput,
    reply_markup: InlineKeyboardMarkup | None,
    *,
    expanded: bool,
) -> bool:
    rich_html = build_vacancy_card_rich_html(inp, expanded=expanded)
    html_fallback = build_vacancy_card_html_fallback(inp, expanded=expanded)

    if RICH_VACANCY_CARDS_ENABLED:
        from services.telegram_rich_message import edit_message_rich_html

        try:
            await edit_message_rich_html(
                bot,
                chat_id,
                message_id,
                rich_html,
                reply_markup=reply_markup,
            )
            return True
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                return True
            logger.warning("editMessageText rich failed, HTML fallback: %s", exc)
        except Exception as exc:
            logger.warning("editMessageText rich error, HTML fallback: %s", exc)

    try:
        await bot.edit_message_text(
            html_fallback,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return True
    except TelegramBadRequest as e:
        if "parse" in str(e).lower():
            await bot.edit_message_text(
                _plain_from_html(html_fallback),
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return True
        if "not modified" in str(e).lower():
            return True
        raise
