"""Картинки для постов в канал: вакансии по category_code, промо по индексу варианта."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import FSInputFile

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Bothost: /app/data — persistent volume, NOT from git. Static PNG live in assets/.
_DEFAULT_CHANNEL_IMAGES_DIR = _PROJECT_ROOT / "assets" / "channel_images"


def get_channel_images_dir() -> Path:
    override = os.getenv("CHANNEL_IMAGES_DIR", "").strip()
    if override:
        return Path(override)
    return _DEFAULT_CHANNEL_IMAGES_DIR


VACANCY_IMAGE_BY_CATEGORY: dict[str, str] = {
    "loader": "vacancy-loader.png",
    "helper": "vacancy-helper.png",
    "promoter": "vacancy-promoter.png",
    "supervisor": "vacancy-supervisor.png",
    "wardrobe": "vacancy-wardrobe.png",
    "parking": "vacancy-parking.png",
}

DEFAULT_VACANCY_IMAGE = "vacancy-default.png"

PROMO_IMAGE_BY_VARIANT: list[str] = [
    "promo-categories.png",   # 0 — «Вакансии под вашу роль»
    "promo-subscribe.png",    # 1 — «Подпишитесь на бота»
    "promo-premium.png",      # 2 — «Ищете смену?» / Premium
]


def _resolve_image(filename: str) -> Path | None:
    path = get_channel_images_dir() / filename
    if path.is_file():
        return path
    logger.warning("Channel image missing: %s", path)
    return None


def resolve_vacancy_image_path(category_code: str) -> Path | None:
    filename = VACANCY_IMAGE_BY_CATEGORY.get(category_code, DEFAULT_VACANCY_IMAGE)
    path = _resolve_image(filename)
    if path is not None:
        return path
    if filename != DEFAULT_VACANCY_IMAGE:
        return _resolve_image(DEFAULT_VACANCY_IMAGE)
    return None


def resolve_promo_image_path(variant_index: int) -> Path | None:
    if not PROMO_IMAGE_BY_VARIANT:
        return None
    filename = PROMO_IMAGE_BY_VARIANT[variant_index % len(PROMO_IMAGE_BY_VARIANT)]
    return _resolve_image(filename)


def log_channel_images_status() -> None:
    """Стартовая диагностика: на Bothost data/ не синхронизируется с git."""
    images_dir = get_channel_images_dir()
    if not images_dir.is_dir():
        logger.warning(
            "Channel images dir missing: %s (posts will be text-only)",
            images_dir,
        )
        return
    pngs = sorted(images_dir.glob("*.png"))
    logger.info(
        "Channel images: %s (%d png)",
        images_dir,
        len(pngs),
    )
    if len(pngs) < len(VACANCY_IMAGE_BY_CATEGORY) + len(PROMO_IMAGE_BY_VARIANT):
        logger.warning(
            "Channel images incomplete: expected at least %d files, found %d",
            len(VACANCY_IMAGE_BY_CATEGORY) + 1 + len(PROMO_IMAGE_BY_VARIANT),
            len(pngs),
        )


async def send_channel_post(
    bot: Bot,
    chat_id: int | str,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    photo_path: Path | None = None,
) -> Message:
    """Фото + caption или текст, если файла нет."""
    if photo_path and photo_path.is_file():
        logger.debug("Channel post with photo: %s", photo_path.name)
        return await bot.send_photo(
            chat_id,
            FSInputFile(photo_path),
            caption=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    if photo_path:
        logger.warning("Channel post fallback to text — photo not found: %s", photo_path)
    return await bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
