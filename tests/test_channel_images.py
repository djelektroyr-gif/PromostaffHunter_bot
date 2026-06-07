"""Тесты маппинга картинок канала и send_photo fallback."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.channel_images import (
    PROMO_IMAGE_BY_VARIANT,
    VACANCY_IMAGE_BY_CATEGORY,
    get_channel_images_dir,
    resolve_promo_image_path,
    resolve_vacancy_image_path,
    send_channel_post,
)


@pytest.mark.parametrize(
    ("category_code", "expected_name"),
    [
        ("loader", "vacancy-loader.png"),
        ("helper", "vacancy-helper.png"),
        ("promoter", "vacancy-promoter.png"),
        ("supervisor", "vacancy-supervisor.png"),
        ("wardrobe", "vacancy-wardrobe.png"),
        ("parking", "vacancy-parking.png"),
        ("hostess", "vacancy-default.png"),
        ("unknown_cat", "vacancy-default.png"),
    ],
)
def test_resolve_vacancy_image_path(category_code, expected_name):
    path = resolve_vacancy_image_path(category_code)
    assert path is not None
    assert path.name == expected_name
    assert path.is_file()


@pytest.mark.parametrize(
    ("variant_index", "expected_name"),
    [
        (0, "promo-categories.png"),
        (1, "promo-subscribe.png"),
        (2, "promo-premium.png"),
        (3, "promo-categories.png"),  # wrap
    ],
)
def test_resolve_promo_image_path(variant_index, expected_name):
    path = resolve_promo_image_path(variant_index)
    assert path is not None
    assert path.name == expected_name
    assert path.is_file()


def test_channel_images_dir_has_all_mapped_files():
    images_dir = get_channel_images_dir()
    for filename in VACANCY_IMAGE_BY_CATEGORY.values():
        assert (images_dir / filename).is_file(), filename
    assert (images_dir / "vacancy-default.png").is_file()
    for filename in PROMO_IMAGE_BY_VARIANT:
        assert (images_dir / filename).is_file(), filename


def test_send_channel_post_with_photo(tmp_path):
    photo = tmp_path / "test.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")
    bot = MagicMock()
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=1))
    bot.send_message = AsyncMock()

    asyncio.run(send_channel_post(
        bot,
        -100123,
        text="<b>hi</b>",
        reply_markup=None,
        photo_path=photo,
    ))

    bot.send_photo.assert_awaited_once()
    bot.send_message.assert_not_awaited()


def test_send_channel_post_fallback_text_when_no_file():
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=2))

    missing = Path("/nonexistent/channel-image.png")
    asyncio.run(send_channel_post(
        bot,
        -100123,
        text="plain",
        photo_path=missing,
    ))

    bot.send_message.assert_awaited_once()
    bot.send_photo.assert_not_awaited()
