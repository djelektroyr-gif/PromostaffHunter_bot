"""Картинки для постов в канал: вакансии по category_code, промо по индексу варианта."""

from __future__ import annotations

import hashlib
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

# По 3 варианта на категорию — ротация 1→2→3→1 при каждой публикации в канал
# (см. next_vacancy_image_variant_index + count_published_channel_vacancy_posts).
VACANCY_IMAGES_BY_CATEGORY: dict[str, list[str]] = {
    "loader": [
        "vacancy-loader-1.png",
        "vacancy-loader-2.png",
        "vacancy-loader-3.png",
    ],
    "helper": [
        "vacancy-helper-1.png",
        "vacancy-helper-2.png",
        "vacancy-helper-3.png",
    ],
    "promoter": [
        "vacancy-promoter-1.png",
        "vacancy-promoter-2.png",
        "vacancy-promoter-3.png",
    ],
    "supervisor": [
        "vacancy-supervisor-1.png",
        "vacancy-supervisor-2.png",
        "vacancy-supervisor-3.png",
    ],
    "wardrobe": [
        "vacancy-wardrobe-1.png",
        "vacancy-wardrobe-2.png",
        "vacancy-wardrobe-3.png",
    ],
    "parking": [
        "vacancy-parking-1.png",
        "vacancy-parking-2.png",
        "vacancy-parking-3.png",
    ],
    "hostess": [
        "vacancy-hostess-1.png",
        "vacancy-hostess-2.png",
        "vacancy-hostess-3.png",
    ],
    "animator": [
        "vacancy-animator-1.png",
        "vacancy-animator-2.png",
        "vacancy-animator-3.png",
    ],
    "waiter": [
        "vacancy-waiter-1.png",
        "vacancy-waiter-2.png",
        "vacancy-waiter-3.png",
    ],
    "driver": [
        "vacancy-driver-1.png",
        "vacancy-driver-2.png",
        "vacancy-driver-3.png",
    ],
    "security": [
        "vacancy-security-1.png",
        "vacancy-security-2.png",
        "vacancy-security-3.png",
    ],
}

DEFAULT_VACANCY_IMAGES = [
    "vacancy-default-1.png",
    "vacancy-default-2.png",
    "vacancy-default-3.png",
]

# Обратная совместимость для тестов/доков (первый вариант).
VACANCY_IMAGE_BY_CATEGORY: dict[str, str] = {
    code: files[0] for code, files in VACANCY_IMAGES_BY_CATEGORY.items()
}

DEFAULT_VACANCY_IMAGE = DEFAULT_VACANCY_IMAGES[0]

# Три карточки на промо-картинках — разные наборы, все 11 категорий бота по очереди.
# Не повторять связку «хелпер + грузчик + супервайзер» на каждом слоте.
PROMO_ROLE_TRIO_BY_VARIANT: list[tuple[str, str, str]] = [
    ("ПРОМОУТЕР", "ХОСТЕС", "АНИМАТОР"),       # слот 09:00
    ("ОФИЦИАНТ", "ВОДИТЕЛЬ", "ОХРАННИК"),     # слот 14:00
    ("ГАРДЕРОБ", "ПАРКОВЩИК", "СУПЕРВАЙЗЕР"),  # слот 20:00
]
# На вакансийных карточках отдельно: helper, loader и др. — см. vacancy-*-N.png

PROMO_IMAGE_BY_VARIANT: list[str] = [
    "promo-categories.png",   # 0 — ВАКАНСИИ ПОД ВАШУ РОЛЬ (тrio[0])
    "promo-subscribe.png",    # 1 — ПОДПИШИТЕСЬ (тrio[1])
    "promo-premium.png",      # 2 — Premium push (тrio[2])
]


def get_channel_images_dir() -> Path:
    override = os.getenv("CHANNEL_IMAGES_DIR", "").strip()
    if override:
        return Path(override)
    return _DEFAULT_CHANNEL_IMAGES_DIR


def _resolve_image(filename: str) -> Path | None:
    path = get_channel_images_dir() / filename
    if path.is_file():
        return path
    logger.warning("Channel image missing: %s", path)
    return None


def _variant_index(seed: str | None, count: int) -> int:
    if count <= 1:
        return 0
    if not seed:
        return 0
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % count


def _vacancy_image_filenames(category_code: str) -> list[str]:
    return VACANCY_IMAGES_BY_CATEGORY.get(category_code) or DEFAULT_VACANCY_IMAGES


def next_vacancy_image_variant_index(category_code: str) -> int:
    """
    Индекс обложки для следующего поста в канале: 0 → 1 → 2 → 0 …
    Считаем уже опубликованные посты этой категории (до текущего).
    """
    from db import count_published_channel_vacancy_posts

    filenames = _vacancy_image_filenames(category_code)
    if not filenames:
        return 0
    published = count_published_channel_vacancy_posts(category_code)
    return published % len(filenames)


def resolve_vacancy_image_path(
    category_code: str,
    vacancy_id: str | None = None,
    *,
    variant_index: int | None = None,
) -> Path | None:
    """
    Путь к PNG вакансии.

    variant_index — явный выбор (ротация в канале);
    иначе стабильный выбор по vacancy_id (fallback).
    """
    filenames = _vacancy_image_filenames(category_code)
    if variant_index is not None:
        idx = variant_index % len(filenames) if filenames else 0
    else:
        idx = _variant_index(vacancy_id or category_code, len(filenames))
    for offset in range(len(filenames)):
        path = _resolve_image(filenames[(idx + offset) % len(filenames)])
        if path is not None:
            return path
    return _resolve_image(DEFAULT_VACANCY_IMAGE)


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
    expected = sum(len(v) for v in VACANCY_IMAGES_BY_CATEGORY.values())
    expected += len(DEFAULT_VACANCY_IMAGES) + len(PROMO_IMAGE_BY_VARIANT)
    logger.info(
        "Channel images: %s (%d png, expected ~%d vacancy+promo)",
        images_dir,
        len(pngs),
        expected,
    )
    missing = []
    for code, files in VACANCY_IMAGES_BY_CATEGORY.items():
        for fn in files:
            if not (images_dir / fn).is_file():
                missing.append(f"{code}:{fn}")
    for fn in DEFAULT_VACANCY_IMAGES + PROMO_IMAGE_BY_VARIANT:
        if not (images_dir / fn).is_file():
            missing.append(fn)
    if missing:
        logger.warning(
            "Channel images incomplete (%d missing), e.g. %s",
            len(missing),
            ", ".join(missing[:5]),
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
