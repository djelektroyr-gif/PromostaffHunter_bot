"""sendRichMessage / editMessageText+rich_message (Bot API 10.1, raw через aiogram session)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiogram.methods.base import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, Message

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)


class SendRichMessage(TelegramMethod[Message]):
    __api_method__ = "sendRichMessage"
    __returning__ = Message


class EditMessageRich(TelegramMethod[Message]):
    __api_method__ = "editMessageText"
    __returning__ = Message


class SendRichMessageDraft(TelegramMethod[bool]):
    __api_method__ = "sendRichMessageDraft"
    __returning__ = bool


def input_rich_message_html(html: str) -> dict[str, Any]:
    return {"html": html}


async def send_rich_message_html(
    bot: Bot,
    chat_id: int | str,
    html: str,
    *,
    message_thread_id: int | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": input_rich_message_html(html),
    }
    if message_thread_id is not None:
        kwargs["message_thread_id"] = message_thread_id
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    return await bot(SendRichMessage(**kwargs))


async def edit_message_rich_html(
    bot: Bot,
    chat_id: int | str,
    message_id: int,
    html: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | bool:
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "rich_message": input_rich_message_html(html),
    }
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    return await bot(EditMessageRich(**kwargs))


async def send_rich_message_draft_html(
    bot: Bot,
    chat_id: int,
    draft_id: int,
    html: str,
    *,
    message_thread_id: int | None = None,
) -> bool:
    kwargs: dict[str, Any] = {
        "chat_id": chat_id,
        "draft_id": draft_id,
        "rich_message": input_rich_message_html(html),
    }
    if message_thread_id is not None:
        kwargs["message_thread_id"] = message_thread_id
    try:
        return await bot(SendRichMessageDraft(**kwargs))
    except Exception as exc:
        logger.debug("sendRichMessageDraft failed: %s", exc)
        return False


async def send_user_rich_message_html(
    bot: Bot,
    user_id: int,
    html: str,
    *,
    topic_key: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    from services.vacancy_card_send import _ensure_topic

    extra = await _ensure_topic(user_id, topic_key, bot)
    thread_id = extra.get("message_thread_id")
    try:
        return await send_rich_message_html(
            bot,
            user_id,
            html,
            message_thread_id=thread_id,
            reply_markup=reply_markup,
        )
    except Exception as exc:
        if thread_id is not None:
            logger.warning("send_user_rich_message: topic fallback user=%s: %s", user_id, exc)
            return await send_rich_message_html(
                bot,
                user_id,
                html,
                reply_markup=reply_markup,
            )
        raise
