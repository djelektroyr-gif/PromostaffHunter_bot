"""Тесты маппинга картинок канала и send_photo fallback."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.channel_images import (
    DEFAULT_VACANCY_IMAGES,
    PROMO_IMAGE_BY_VARIANT,
    VACANCY_IMAGES_BY_CATEGORY,
    VACANCY_IMAGE_BY_CATEGORY,
    get_channel_images_dir,
    next_vacancy_image_variant_index,
    resolve_live_video_path,
    resolve_promo_image_path,
    resolve_vacancy_image_path,
    send_channel_post,
)


@pytest.mark.parametrize(
    ("category_code", "expected_name"),
    [
        ("loader", "vacancy-loader-1.png"),
        ("helper", "vacancy-helper-1.png"),
        ("promoter", "vacancy-promoter-1.png"),
        ("supervisor", "vacancy-supervisor-1.png"),
        ("wardrobe", "vacancy-wardrobe-1.png"),
        ("parking", "vacancy-parking-1.png"),
        ("hostess", "vacancy-hostess-1.png"),
        ("driver", "vacancy-driver-1.png"),
        ("unknown_cat", "vacancy-default-1.png"),
    ],
)
def test_resolve_vacancy_image_path(category_code, expected_name):
    path = resolve_vacancy_image_path(category_code, "vac_test_123")
    assert path is not None
    assert path.is_file()
    allowed = VACANCY_IMAGES_BY_CATEGORY.get(category_code) or DEFAULT_VACANCY_IMAGES
    assert path.name in allowed


def test_resolve_vacancy_image_rotates_by_vacancy_id():
    p1 = resolve_vacancy_image_path("loader", "vac_a")
    p2 = resolve_vacancy_image_path("loader", "vac_b")
    assert p1 is not None and p2 is not None
    assert p1.name in VACANCY_IMAGES_BY_CATEGORY["loader"]
    assert p2.name in VACANCY_IMAGES_BY_CATEGORY["loader"]


def test_resolve_vacancy_image_path_by_variant_index():
    p0 = resolve_vacancy_image_path("loader", variant_index=0)
    p1 = resolve_vacancy_image_path("loader", variant_index=1)
    p2 = resolve_vacancy_image_path("loader", variant_index=2)
    assert p0 is not None and p1 is not None and p2 is not None
    assert p0.name == "vacancy-loader-1.png"
    assert p1.name == "vacancy-loader-2.png"
    assert p2.name == "vacancy-loader-3.png"


def test_next_vacancy_image_variant_index_rotates(monkeypatch):
    counts = {"loader": 0}

    def fake_count(category_code: str) -> int:
        return counts.get(category_code, 0)

    monkeypatch.setattr(
        "db.count_published_channel_vacancy_posts",
        fake_count,
    )
    assert next_vacancy_image_variant_index("loader") == 0
    counts["loader"] = 1
    assert next_vacancy_image_variant_index("loader") == 1
    counts["loader"] = 2
    assert next_vacancy_image_variant_index("loader") == 2
    counts["loader"] = 3
    assert next_vacancy_image_variant_index("loader") == 0


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
    for code, filenames in VACANCY_IMAGES_BY_CATEGORY.items():
        for filename in filenames:
            assert (images_dir / filename).is_file(), f"{code}:{filename}"
    for filename in DEFAULT_VACANCY_IMAGES:
        assert (images_dir / filename).is_file(), filename
    for filename in PROMO_IMAGE_BY_VARIANT:
        assert (images_dir / filename).is_file(), filename
    assert VACANCY_IMAGE_BY_CATEGORY["loader"] == "vacancy-loader-1.png"


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


def test_resolve_live_video_path_pairs_png(tmp_path):
    photo = tmp_path / "vacancy-promoter-1.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert resolve_live_video_path(photo) is None
    video = tmp_path / "vacancy-promoter-1.mp4"
    video.write_bytes(b"fake-mp4")
    assert resolve_live_video_path(photo) == video


def test_send_channel_post_live_photo(monkeypatch, tmp_path):
    monkeypatch.setenv("CHANNEL_LIVE_PHOTO_ENABLED", "1")
    import importlib
    import config

    importlib.reload(config)

    photo = tmp_path / "vacancy-promoter-1.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")
    video = tmp_path / "vacancy-promoter-1.mp4"
    video.write_bytes(b"fake-mp4")

    bot = MagicMock()
    bot.send_live_photo = AsyncMock(return_value=MagicMock(message_id=3))
    bot.send_photo = AsyncMock()
    bot.send_message = AsyncMock()

    asyncio.run(send_channel_post(
        bot,
        -100123,
        text="<b>live</b>",
        photo_path=photo,
    ))

    bot.send_live_photo.assert_awaited_once()
    bot.send_photo.assert_not_awaited()


def test_send_channel_post_live_photo_fallback_on_error(monkeypatch, tmp_path):
    monkeypatch.setenv("CHANNEL_LIVE_PHOTO_ENABLED", "1")
    import importlib
    import config

    importlib.reload(config)

    photo = tmp_path / "vacancy-promoter-1.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")
    video = tmp_path / "vacancy-promoter-1.mp4"
    video.write_bytes(b"fake-mp4")

    bot = MagicMock()
    bot.send_live_photo = AsyncMock(side_effect=RuntimeError("api"))
    bot.send_photo = AsyncMock(return_value=MagicMock(message_id=4))
    bot.send_message = AsyncMock()

    asyncio.run(send_channel_post(
        bot,
        -100123,
        text="fallback",
        photo_path=photo,
    ))

    bot.send_live_photo.assert_awaited_once()
    bot.send_photo.assert_awaited_once()
