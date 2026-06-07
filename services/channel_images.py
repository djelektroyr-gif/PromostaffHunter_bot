"""Картинки для постов в канал: вакансии по category_code, промо по индексу варианта."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import FSInputFile

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)

CHANNEL_IMAGES_DIR = Path(__file__).resolve().parent.parent / "data" / "channel_images"

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
    path = CHANNEL_IMAGES_DIR / filename
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
        return await bot.send_photo(
            chat_id,
            FSInputFile(photo_path),
            caption=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    return await bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
