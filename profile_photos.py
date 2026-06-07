"""Хранение фото профиля на persistent volume и отправка с fallback."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile

from config import get_shared_dir

logger = logging.getLogger(__name__)

PHOTO_CHECK_INTERVAL_SEC = int(os.getenv("PHOTO_CHECK_INTERVAL_SEC", str(24 * 3600)))


def _bothost_data_dir() -> str | None:
    explicit = os.getenv("DATA_DIR", "/app/data").strip()
    if explicit and os.path.isdir(explicit):
        return explicit
    return None


def resolve_user_photos_dir() -> str:
    """
    Каталог для jpg профилей. Bothost:
    - /app/shared/user_photos — если включено «Общее хранилище»;
    - /app/data/user_photos — persistent volume (не из git);
    - иначе user_photos рядом с cwd (локальная разработка).
    """
    explicit = os.getenv("USER_PHOTOS_DIR", "").strip()
    if explicit:
        return explicit

    shared = get_shared_dir()
    if shared:
        return os.path.join(shared, "user_photos")

    data_dir = _bothost_data_dir()
    if data_dir:
        return os.path.join(data_dir, "user_photos")

    return os.path.join(os.getcwd(), "user_photos")


def get_user_photos_dir() -> str:
    path = resolve_user_photos_dir()
    os.makedirs(path, exist_ok=True)
    return path


def profile_photo_path(user_id: int) -> str:
    return os.path.join(get_user_photos_dir(), f"{user_id}.jpg")


def _legacy_user_photo_dirs() -> list[str]:
    dirs = [
        os.path.join(os.getcwd(), "user_photos"),
        "/app/user_photos",
    ]
    data_dir = _bothost_data_dir()
    if data_dir:
        dirs.append(os.path.join(data_dir, "user_photos"))
    shared = get_shared_dir()
    if shared:
        dirs.append(os.path.join(shared, "user_photos"))
    current = get_user_photos_dir()
    return [d for d in dirs if d and d != current]


def migrate_legacy_user_photos() -> int:
    """Перенос *.jpg из старых каталогов в актуальный persistent path."""
    target = get_user_photos_dir()
    moved = 0
    for legacy in _legacy_user_photo_dirs():
        if not os.path.isdir(legacy):
            continue
        for name in os.listdir(legacy):
            if not name.endswith(".jpg"):
                continue
            src = os.path.join(legacy, name)
            dst = os.path.join(target, name)
            if os.path.isfile(dst):
                continue
            try:
                shutil.copy2(src, dst)
                moved += 1
                logger.info("User photo migrated: %s → %s", src, dst)
            except OSError as e:
                logger.warning("User photo migrate failed %s: %s", src, e)
    return moved


def reconcile_photo_storage_paths() -> int:
    """Обновить photo_storage_path в БД, если файл лежит в новом каталоге."""
    from db import get_subscribers_with_photos, update_subscriber_photo_storage

    fixed = 0
    for row in get_subscribers_with_photos():
        user_id = row["user_id"]
        storage_path = row.get("photo_storage_path")
        canonical = profile_photo_path(user_id)
        if storage_path and os.path.isfile(storage_path):
            continue
        if os.path.isfile(canonical):
            update_subscriber_photo_storage(
                user_id,
                row.get("photo_file_id"),
                canonical,
            )
            fixed += 1
    return fixed


def log_user_photos_status() -> None:
    photos_dir = get_user_photos_dir()
    try:
        count = len([n for n in os.listdir(photos_dir) if n.endswith(".jpg")])
    except OSError:
        count = 0
    logger.info("User photos dir: %s (%d jpg)", photos_dir, count)
    if count == 0:
        logger.info(
            "User photos: на Bothost смотрите %s или /app/shared/user_photos, не git data/",
            photos_dir,
        )


async def persist_user_photo(bot: Bot, user_id: int, file_id: str) -> tuple[str | None, str | None]:
    """
    Скачивает фото в persistent user_photos/{user_id}.jpg.
    Возвращает (storage_path, file_id).
    """
    if not file_id:
        return None, None
    dest = profile_photo_path(user_id)
    try:
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, dest)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            logger.info("User photo saved user=%s path=%s", user_id, dest)
            return dest, file_id
        logger.warning("User photo empty after download user=%s path=%s", user_id, dest)
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


def prepare_user_photos_storage() -> None:
    """Старт: миграция с эфемерных путей + синхронизация путей в БД."""
    migrate_legacy_user_photos()
    fixed = reconcile_photo_storage_paths()
    if fixed:
        logger.info("User photo DB paths reconciled: %d", fixed)
    log_user_photos_status()
