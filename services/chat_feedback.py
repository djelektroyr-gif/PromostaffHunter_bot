"""Индикатор «печатает…» и keepalive для долгих операций в чате."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram import Bot

TYPING_REFRESH_SEC = 4.0


async def send_typing(bot: Bot, chat_id: int, action: str = "typing") -> None:
    try:
        await bot.send_chat_action(chat_id, action)
    except Exception:
        pass


@asynccontextmanager
async def typing_keepalive(bot: Bot, chat_id: int, action: str = "typing"):
    """Держит «печатает…» на экране, пока идёт долгая операция (>5 с)."""
    stop = asyncio.Event()

    async def _loop() -> None:
        while not stop.is_set():
            await send_typing(bot, chat_id, action)
            try:
                await asyncio.wait_for(stop.wait(), timeout=TYPING_REFRESH_SEC)
            except asyncio.TimeoutError:
                continue

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
