"""Хранение фото профиля на диске (shared volume) и отправка с fallback."""
import asyncio
import logging
import os

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile

from config import get_shared_dir

logger = logging.getLogger(__name__)

PHOTO_CHECK_INTERVAL_SEC = int(os.getenv("PHOTO_CHECK_INTERVAL_SEC", str(24 * 3600)))


def get_user_photos_dir() -> str:
    shared = get_shared_dir()
    base = shared if shared else os.getcwd()
    path = os.path.join(base, "user_photos")
    os.makedirs(path, exist_ok=True)
    return path


def profile_photo_path(user_id: int) -> str:
    return os.path.join(get_user_photos_dir(), f"{user_id}.jpg")


async def persist_user_photo(bot: Bot, user_id: int, file_id: str) -> tuple[str | None, str | None]:
    """
    Скачивает фото в shared/user_photos/{user_id}.jpg.
    Возвращает (storage_path, file_id).
    """
    if not file_id:
        return None, None
    dest = profile_photo_path(user_id)
    try:
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, dest)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            return dest, file_id
    except Exception as e:
        logger.warning("persist_user_photo user=%s: %s", user_id, e)
    return None, file_id


async def telegram_file_id_valid(bot: Bot, file_id: str) -> bool:
    if not file_id:
        return False
    try:
        await bot.get_file(file_id)
        return True
    except TelegramBadRequest:
        return False
    except Exception as e:
        logger.warning("telegram_file_id_valid: %s", e)
        return False


async def refresh_file_id_from_storage(bot: Bot, user_id: int, storage_path: str) -> str | None:
    """Перезаливает файл с диска в Telegram и возвращает новый file_id."""
    if not storage_path or not os.path.isfile(storage_path):
        return None
    try:
        msg = await bot.send_photo(user_id, FSInputFile(storage_path), caption="🔄 Служебное: обновление фото профиля")
        file_id = msg.photo[-1].file_id if msg.photo else None
        try:
            await bot.delete_message(user_id, msg.message_id)
        except Exception:
            pass
        return file_id
    except Exception as e:
        logger.warning("refresh_file_id_from_storage user=%s: %s", user_id, e)
        return None


async def send_profile_photo(
    bot: Bot,
    chat_id: int,
    profile: dict,
    *,
    caption: str | None = None,
    parse_mode: str | None = None,
    **kwargs,
):
    """
    Отправка фото профиля: сначала файл с диска, иначе photo_file_id.
    """
    storage_path = profile.get("photo_storage_path")
    file_id = profile.get("photo_file_id")
    common = {"caption": caption, "parse_mode": parse_mode, **kwargs}

    if storage_path and os.path.isfile(storage_path):
        try:
            return await bot.send_photo(chat_id, FSInputFile(storage_path), **common)
        except Exception as e:
            logger.warning("send_profile_photo disk chat=%s: %s", chat_id, e)

    if file_id:
        try:
            return await bot.send_photo(chat_id, file_id, **common)
        except TelegramBadRequest as e:
            logger.warning("send_profile_photo file_id chat=%s: %s", chat_id, e)
            if storage_path and os.path.isfile(storage_path):
                return await bot.send_photo(chat_id, FSInputFile(storage_path), **common)
            raise
    return None


async def run_photo_health_check(bot: Bot, notify_callback):
    """
    Проверка file_id и напоминания о давно не обновлённом фото.
    notify_callback(user_id, text) — async отправка пользователю.
    """
    from db import (
        clear_subscriber_photo,
        get_subscribers_with_photos,
        update_subscriber_photo_storage,
    )

    for row in get_subscribers_with_photos():
        user_id = row["user_id"]
        file_id = row.get("photo_file_id")
        storage_path = row.get("photo_storage_path")

        if file_id and not await telegram_file_id_valid(bot, file_id):
            new_id = None
            if storage_path and os.path.isfile(storage_path):
                new_id = await refresh_file_id_from_storage(bot, user_id, storage_path)
            if new_id:
                update_subscriber_photo_storage(user_id, new_id, storage_path)
                logger.info("photo file_id refreshed user=%s", user_id)
            else:
                clear_subscriber_photo(user_id)
                await notify_callback(
                    user_id,
                    "📷 *Фото профиля устарело*\n\n"
                    "Telegram больше не отдаёт старое фото. "
                    "Загрузите новое в «👤 Мои данные» → «📷 Фото».",
                )


async def photo_health_loop(bot: Bot, notify_callback):
    await asyncio.sleep(60)
    while True:
        try:
            await run_photo_health_check(bot, notify_callback)
        except Exception:
            logger.exception("photo_health_loop")
        await asyncio.sleep(PHOTO_CHECK_INTERVAL_SEC)
