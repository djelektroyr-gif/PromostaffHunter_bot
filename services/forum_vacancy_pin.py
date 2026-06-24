"""Одна актуальная вакансия в General — история в топике «Вакансии»."""

from __future__ import annotations

import asyncio
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
from services.forum_topics import (
    TOPIC_VACANCIES,
    is_forum_thread_missing_error,
    recreate_user_topic,
    topic_message_kwargs,
)

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

_user_pin_locks: dict[int, asyncio.Lock] = {}


def _user_pin_lock(user_id: int) -> asyncio.Lock:
    lock = _user_pin_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_pin_locks[user_id] = lock
    return lock


def _delivery_kwargs(user_id: int, topic_key: str | None) -> dict:
    """General в forum-личке = message_thread_id 1 (как reply-меню), иначе delete не совпадает."""
    from config import FORUM_TOPICS_ENABLED
    from services.chat_feedback import GENERAL_TOPIC_THREAD_ID

    if topic_key:
        return topic_message_kwargs(user_id, topic_key)
    if FORUM_TOPICS_ENABLED:
        return {"message_thread_id": GENERAL_TOPIC_THREAD_ID}
    return {}


async def _send_html_card(
    bot: Bot,
    user_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    *,
    topic_key: str | None,
) -> tuple[int | None, int | None]:
    """Возвращает (message_id, message_thread_id для General)."""
    extra = _delivery_kwargs(user_id, topic_key)
    try:
        msg = await bot.send_message(
            user_id,
            text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
            **extra,
        )
        return msg.message_id, extra.get("message_thread_id")
    except TelegramBadRequest as e:
        err = str(e).lower()
        if extra.get("message_thread_id") and ("thread" in err or "topic" in err or "not found" in err):
            if topic_key:
                raise
            logger.warning(
                "forum_vacancy_pin: General thread miss user=%s — fallback без thread_id",
                user_id,
            )
            msg = await bot.send_message(
                user_id,
                text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return msg.message_id, None
        if "parse" in err:
            plain = re.sub(r"<[^>]*>", "", text)
            msg = await bot.send_message(
                user_id,
                plain,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
                **extra,
            )
            return msg.message_id, extra.get("message_thread_id")
        raise


async def _try_edit_general_card(
    bot: Bot,
    user_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> bool:
    """Обновляет одну карточку в General — чистый экран без delete+send."""
    try:
        await bot.edit_message_text(
            text,
            chat_id=user_id,
            message_id=message_id,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        return True
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "not modified" in err:
            return True
        if "message to edit not found" in err or "message can't be edited" in err:
            return False
        if "parse" in err:
            plain = re.sub(r"<[^>]*>", "", text)
            try:
                await bot.edit_message_text(
                    plain,
                    chat_id=user_id,
                    message_id=message_id,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                return True
            except TelegramBadRequest:
                return False
        logger.warning(
            "edit general vacancy pin user=%s msg=%s: %s",
            user_id,
            message_id,
            e,
        )
        return False


async def _delete_general_message(
    bot: Bot,
    user_id: int,
    message_id: int,
    *,
    stored_thread_id: int | None = None,
) -> bool:
    """Удаление push-карточки. True = можно сбросить pin (удалили или уже нет)."""
    del stored_thread_id
    try:
        await bot.delete_message(chat_id=user_id, message_id=message_id)
        return True
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "message to delete not found" in err:
            return True
        logger.warning(
            "delete general vacancy pin user=%s msg=%s: %s",
            user_id,
            message_id,
            e,
        )
        return False
    except TypeError as e:
        logger.warning(
            "delete general vacancy pin user=%s msg=%s: %s",
            user_id,
            message_id,
            e,
        )
        return False


async def _clear_general_vacancy_display(bot: Bot, user_id: int) -> bool:
    """Убирает предыдущую push-карточку из General. False = pin оставлен для повтора delete."""
    pin = get_general_vacancy_pin(user_id)
    if not pin:
        return True
    if await _delete_general_message(
        bot,
        user_id,
        pin["message_id"],
        stored_thread_id=pin.get("message_thread_id"),
    ):
        clear_general_vacancy_pin(user_id)
        return True
    logger.warning(
        "general pin kept user=%s msg=%s — delete failed, retry next push",
        user_id,
        pin["message_id"],
    )
    return False


async def clear_general_vacancy_if_pinned(
    bot: Bot,
    user_id: int,
    vacancy_id: str | None = None,
) -> None:
    """После отклика / «не подходит» — убрать карточку из General."""
    pin = get_general_vacancy_pin(user_id)
    if not pin:
        return
    if vacancy_id and pin["vacancy_id"] != vacancy_id:
        return
    await _clear_general_vacancy_display(bot, user_id)


async def _append_vacancy_history(
    bot: Bot,
    user_id: int,
    vacancy_id: str,
    card_text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    if not get_user_topic_thread_id(user_id, TOPIC_VACANCIES):
        try:
            from services.forum_topics import ensure_user_forum_topics

            await ensure_user_forum_topics(bot, user_id)
        except Exception as e:
            logger.warning("vacancy history ensure topics user=%s: %s", user_id, e)
            return
        if not get_user_topic_thread_id(user_id, TOPIC_VACANCIES):
            return
    try:
        await _send_html_card(
            bot,
            user_id,
            card_text,
            reply_markup,
            topic_key=TOPIC_VACANCIES,
        )
    except TelegramBadRequest as e:
        if is_forum_thread_missing_error(e):
            new_thread = await recreate_user_topic(bot, user_id, TOPIC_VACANCIES)
            if new_thread:
                try:
                    await _send_html_card(
                        bot,
                        user_id,
                        card_text,
                        reply_markup,
                        topic_key=TOPIC_VACANCIES,
                    )
                    return
                except Exception as e2:
                    logger.warning(
                        "vacancy history topic retry user=%s vac=%s: %s",
                        user_id,
                        vacancy_id,
                        e2,
                    )
                    return
        logger.warning("vacancy history topic user=%s vac=%s: %s", user_id, vacancy_id, e)
    except Exception as e:
        logger.warning("vacancy history topic user=%s vac=%s: %s", user_id, vacancy_id, e)


async def append_vacancy_history_message(
    bot: Bot,
    user_id: int,
    vacancy_id: str,
    card_text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    """Копия push-карточки в топик «Вакансии» (история)."""
    await _append_vacancy_history(bot, user_id, vacancy_id, card_text, reply_markup)


async def _install_general_push_card(
    bot: Bot,
    user_id: int,
    vacancy_id: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> bool:
    """Одна карточка в General + pin в БД (после удаления предыдущей, если получилось)."""
    if not await _clear_general_vacancy_display(bot, user_id):
        pin = get_general_vacancy_pin(user_id)
        if pin:
            for attempt in range(2):
                if await _delete_general_message(
                    bot,
                    user_id,
                    pin["message_id"],
                    stored_thread_id=pin.get("message_thread_id"),
                ):
                    clear_general_vacancy_pin(user_id)
                    break
                if attempt == 0:
                    await asyncio.sleep(0.25)
    msg_id, thread_id = await _send_html_card(bot, user_id, text, reply_markup, topic_key=None)
    if msg_id is None:
        return False
    set_general_vacancy_pin(
        user_id,
        msg_id,
        vacancy_id,
        text,
        message_thread_id=thread_id,
    )
    return True


async def archive_general_vacancy_to_topic(
    bot: Bot,
    user_id: int,
    *,
    rebuild_keyboard,
) -> None:
    """Обратная совместимость: только очистка General."""
    del rebuild_keyboard
    await _clear_general_vacancy_display(bot, user_id)


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
    """Push: одна карточка в General; копия в «Вакансии»."""
    del rebuild_keyboard
    async with _user_pin_lock(user_id):
        await ensure_topics(user_id)
        pin = get_general_vacancy_pin(user_id)
        if pin and pin["vacancy_id"] == vacancy_id:
            if await _try_edit_general_card(
                bot, user_id, pin["message_id"], text, reply_markup
            ):
                set_general_vacancy_pin(
                    user_id,
                    pin["message_id"],
                    vacancy_id,
                    text,
                    message_thread_id=pin.get("message_thread_id"),
                )
                return True
        try:
            if not await _install_general_push_card(bot, user_id, vacancy_id, text, reply_markup):
                return False
        except Exception as e:
            logger.warning(
                "install general push user=%s vac=%s: %s",
                user_id,
                vacancy_id,
                e,
            )
            return False
        await _append_vacancy_history(bot, user_id, vacancy_id, text, reply_markup)
    return True


async def deliver_forum_vacancy_push(
    bot: Bot,
    user_id: int,
    vacancy_id: str,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
    *,
    ensure_topics,
) -> bool:
    """Единая точка Premium push: General (1 карточка) + история в «Вакансии»."""
    try:
        if await send_vacancy_push_pinned_general(
            bot,
            user_id,
            vacancy_id,
            text,
            reply_markup,
            rebuild_keyboard=None,
            ensure_topics=ensure_topics,
        ):
            return True
    except Exception as exc:
        logger.warning(
            "deliver forum push user=%s vac=%s: %s — fallback install",
            user_id,
            vacancy_id,
            exc,
        )
    async with _user_pin_lock(user_id):
        await ensure_topics(user_id)
        try:
            if not await _install_general_push_card(bot, user_id, vacancy_id, text, reply_markup):
                return False
        except Exception as exc:
            logger.warning(
                "deliver forum push fallback user=%s vac=%s: %s",
                user_id,
                vacancy_id,
                exc,
            )
            return False
        await _append_vacancy_history(bot, user_id, vacancy_id, text, reply_markup)
    return True
