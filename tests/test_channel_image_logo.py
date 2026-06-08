"""Tests for channel image logo compositing."""

from pathlib import Path

import pytest

pytest.importorskip("PIL")

from services.channel_image_logo import (
    LOGO_OVERLAY_FILENAMES,
    apply_logo_to_directory,
    composite_channel_logo,
    should_apply_channel_logo,
    LOGO_FILENAME,
)


def test_should_apply_channel_logo_whitelist():
    assert should_apply_channel_logo("promo-maintenance.png") is True
    assert should_apply_channel_logo("promo-update-premium-filters.png") is True
    assert should_apply_channel_logo("promo-categories.png") is False
    assert should_apply_channel_logo("vacancy-loader-1.png") is False


def test_composite_channel_logo_skips_non_whitelist(tmp_path):
    from PIL import Image

    logo = tmp_path / LOGO_FILENAME
    base = tmp_path / "promo-categories.png"
    Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(logo)
    Image.new("RGB", (400, 400), (120, 180, 220)).save(base)
    assert composite_channel_logo(base, logo_path=logo) is False


def test_composite_channel_logo_whitelist(tmp_path):
    from PIL import Image

    logo = tmp_path / LOGO_FILENAME
    base = tmp_path / "promo-maintenance.png"
    Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(logo)
    Image.new("RGB", (400, 400), (120, 180, 220)).save(base)
    assert composite_channel_logo(base, logo_path=logo) is True
    out = Image.open(base)
    assert out.size == (400, 400)


def test_apply_logo_to_directory_respects_whitelist(tmp_path):
    from PIL import Image

    logo = tmp_path / LOGO_FILENAME
    Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(logo)
    for name in ("promo-maintenance.png", "vacancy-helper-1.png", LOGO_FILENAME):
        Image.new("RGB", (200, 200), (100, 100, 100)).save(tmp_path / name)

    ok, skipped = apply_logo_to_directory(tmp_path, logo_path=logo)
    assert ok == 1
    assert skipped >= 2
    assert len(LOGO_OVERLAY_FILENAMES) >= 2
