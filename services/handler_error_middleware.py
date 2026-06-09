"""Ловит необработанные ошибки handler → алерт админу + сообщение пользователю."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from services.admin_ops_alerts import notify_admin_handler_error

logger = logging.getLogger(__name__)


def _extract_user_id(event: TelegramObject) -> int | None:
    if isinstance(event, CallbackQuery) and event.from_user:
        return event.from_user.id
    if isinstance(event, Message) and event.from_user:
        return event.from_user.id
    return None


def _handler_label(data: dict[str, Any]) -> str:
    handler = data.get("handler")
    if handler is not None:
        return getattr(handler, "__name__", str(handler))
    return "unknown"


class HandlerErrorAlertMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower() or "query is too old" in str(e).lower():
                return None
            raise
        except Exception as e:
            bot: Bot = data["bot"]
            user_id = _extract_user_id(event)
            label = _handler_label(data)
            logger.exception("Handler error %s user=%s: %s", label, user_id, e)
            await notify_admin_handler_error(
                bot,
                user_id=user_id,
                handler=label,
                error_text=str(e),
            )
            await _reply_user_error(bot, event)
            return None


async def _reply_user_error(bot: Bot, event: TelegramObject) -> None:
    text = "⚠️ Временная ошибка. Попробуйте ещё раз или напишите /start"
    try:
        if isinstance(event, CallbackQuery):
            if event.message:
                await event.message.answer(text)
            await event.answer("Ошибка", show_alert=True)
        elif isinstance(event, Message):
            await event.answer(text)
    except Exception as ex:
        logger.debug("user error reply failed: %s", ex)
