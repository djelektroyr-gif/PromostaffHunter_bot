"""Наложение официального логотипа Promostaff Hunter на PNG канала."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

LOGO_FILENAME = "promostaff-hunter-logo.png"
SKIP_FILENAMES = frozenset({LOGO_FILENAME})

# Круглый логотип — только если на картинке НЕТ надписи PROMOSTAFF (техработы, обновление).
# Вакансии и промо с текстом на форме/экране — без склейки (дублирование бренда).
LOGO_OVERLAY_FILENAMES = frozenset({
    "promo-maintenance.png",
    "promo-update-premium-filters.png",
})


def should_apply_channel_logo(filename: str) -> bool:
    """True — накладывать promostaff-hunter-logo.png."""
    return filename in LOGO_OVERLAY_FILENAMES


def composite_channel_logo(
    image_path: Path,
    *,
    logo_path: Path,
    size_ratio: float = 0.13,
    margin_ratio: float = 0.028,
    overwrite: bool = True,
    force: bool = False,
) -> bool:
    """Накладывает круглый логотип в правый нижний угол. True если файл обновлён."""
    if not force and not should_apply_channel_logo(image_path.name):
        return False
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("Pillow required: pip install Pillow") from e

    if not image_path.is_file():
        logger.warning("Skip missing image: %s", image_path)
        return False
    if image_path.name in SKIP_FILENAMES:
        return False
    if not logo_path.is_file():
        raise FileNotFoundError(f"Logo not found: {logo_path}")

    base = Image.open(image_path).convert("RGBA")
    logo = Image.open(logo_path).convert("RGBA")
    w, h = base.size
    logo_size = max(48, int(min(w, h) * size_ratio))
    logo = logo.resize((logo_size, logo_size), Image.Resampling.LANCZOS)
    margin = max(8, int(min(w, h) * margin_ratio))
    x = w - logo_size - margin
    y = h - logo_size - margin
    base.paste(logo, (x, y), logo)
    out_path = image_path if overwrite else image_path.with_stem(image_path.stem + "-logo")
    base.convert("RGB").save(out_path, format="PNG", optimize=True)
    return True


def apply_logo_to_directory(
    images_dir: Path,
    *,
    logo_path: Path | None = None,
    pattern: str = "*.png",
    force: bool = False,
) -> tuple[int, int]:
    """Склейка только для файлов из LOGO_OVERLAY_FILENAMES. Returns (ok, skipped)."""
    logo = logo_path or (images_dir / LOGO_FILENAME)
    ok = skipped = 0
    for path in sorted(images_dir.glob(pattern)):
        if path.name in SKIP_FILENAMES:
            skipped += 1
            continue
        if not force and not should_apply_channel_logo(path.name):
            skipped += 1
            continue
        if composite_channel_logo(path, logo_path=logo, force=force):
            ok += 1
            logger.info("Logo applied: %s", path.name)
    return ok, skipped
