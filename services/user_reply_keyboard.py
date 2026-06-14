"""Reply-меню подписчика: persistent + General topic (desktop + forum topics)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, ReplyKeyboardMarkup

from config import FORUM_TOPICS_ENABLED
from services.chat_feedback import GENERAL_TOPIC_THREAD_ID
from services.forum_topics import is_forum_thread_missing_error

if TYPE_CHECKING:
    from aiogram import Bot


def with_persistent_keyboard(keyboard: ReplyKeyboardMarkup) -> ReplyKeyboardMarkup:
    """is_persistent — меню не сворачивается на Telegram Desktop (Bot API 6.7+)."""
    if keyboard.is_persistent:
        return keyboard
    payload = keyboard.model_dump(exclude_none=True)
    payload["is_persistent"] = True
    return ReplyKeyboardMarkup(**payload)


def reply_keyboard_delivery_kwargs() -> dict:
    """Forum-личка: reply-меню в General (thread_id=1), иначе desktop его не показывает."""
    if FORUM_TOPICS_ENABLED:
        return {"message_thread_id": GENERAL_TOPIC_THREAD_ID}
    return {}


async def bot_send_user_reply_keyboard(
    bot: Bot,
    chat_id: int,
    text: str,
    keyboard: ReplyKeyboardMarkup,
    **extra,
):
    kwargs = {
        **extra,
        "reply_markup": with_persistent_keyboard(keyboard),
        **reply_keyboard_delivery_kwargs(),
    }
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "text must be non-empty" in err or "message text is empty" in err:
            return await bot.send_message(chat_id, "·", **kwargs)
        if kwargs.get("message_thread_id") and is_forum_thread_missing_error(e):
            kwargs.pop("message_thread_id", None)
            return await bot.send_message(chat_id, text, **kwargs)
        raise


async def answer_user_reply_keyboard(
    bot: Bot,
    message: Message,
    text: str,
    keyboard: ReplyKeyboardMarkup,
    **extra,
):
    """Ответ с reply-меню; при forum topics — в General, не в топик «Вакансии»."""
    if FORUM_TOPICS_ENABLED:
        return await bot_send_user_reply_keyboard(
            bot, message.chat.id, text, keyboard, **extra,
        )
    return await message.answer(
        text,
        reply_markup=with_persistent_keyboard(keyboard),
        **extra,
    )


async def refresh_user_reply_keyboard(
    bot: Bot,
    chat_id: int,
    keyboard: ReplyKeyboardMarkup,
) -> None:
    """Тихо вернуть reply-меню после ленты/inline-сценариев (desktop)."""
    try:
        await bot_send_user_reply_keyboard(
            bot,
            chat_id,
            "·",
            keyboard,
            disable_notification=True,
        )
    except TelegramBadRequest:
        pass
