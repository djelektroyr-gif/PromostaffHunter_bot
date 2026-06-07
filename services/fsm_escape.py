"""Выход из FSM по кнопкам reply-меню."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, TelegramObject

EscapeFn = Callable[[Message, FSMContext], Awaitable[bool]]


class UserMenuFsmEscapeMiddleware(BaseMiddleware):
    """Если пользователь в FSM и жмёт кнопку меню — отмена шага и переход в меню."""

    def __init__(self, menu_buttons: set[str], escape_fn: EscapeFn) -> None:
        self._menu_buttons = menu_buttons
        self._escape_fn = escape_fn

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        if not isinstance(event, Message) or not event.text:
            return await handler(event, data)
        state: FSMContext | None = data.get("state")
        if state is None:
            return await handler(event, data)
        if await state.get_state() is None:
            return await handler(event, data)
        if event.text not in self._menu_buttons:
            return await handler(event, data)
        if await self._escape_fn(event, state):
            return None
        return await handler(event, data)
