#!/usr/bin/env python3
"""Точечная правка promo-ecosystem-no-30-chats: экран телефона + круглый логотип."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS_CURSOR = Path(
    r"C:\Users\Яр\.cursor\projects\c-Users-Documents-GitHub-promostaff-bot\assets"
)
BASE_NAME = "promo-ecosystem-no-30-chats.png"
OUT_NAME = "promo-ecosystem-launch.png"
LOGO_NAME = "promostaff-hunter-logo.png"

# Контентная область экрана (без шапки «PROMOSTAFF Hunter бот»)
PHONE_CONTENT_BOX = (872, 352, 1088, 592)
PHONE_CARD_BOX = (884, 368, 1076, 560)
# Pill-логотип (пиксельный bbox оригинала)
OLD_LOGO_BOX = (1045, 815, 1510, 992)
LOGO_CORNER_SAMPLE = (1280, 920)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _rounded_rect(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    radius: int,
    fill: tuple[int, ...],
    outline: tuple[int, ...] | None = None,
    width: int = 1,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def draw_vacancy_card(size: tuple[int, int]) -> Image.Image:
    w, h = size
    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)

    _rounded_rect(draw, (0, 0, w - 1, h - 1), 14, (255, 255, 255, 255), (220, 224, 230, 255), 2)

    title_font = _font(max(13, w // 17), bold=True)
    body_font = _font(max(11, w // 22))
    btn_font = _font(max(10, w // 26), bold=True)

    y = 10
    draw.text((12, y), "👷 Хелпер · на завтра", fill=(30, 30, 30, 255), font=title_font)
    y += 28
    draw.text((12, y), "Нужны 16 хелперов", fill=(20, 20, 20, 255), font=body_font)
    y += 22
    draw.text((12, y), "📍 Краснопресненская наб.", fill=(60, 60, 60, 255), font=body_font)
    y += 20
    draw.text((12, y), "Помощь на демонтаже", fill=(90, 90, 90, 255), font=body_font)

    btn_h = max(24, h // 7)
    btn_y = h - btn_h - 10
    half_w = (w - 30) // 2
    _rounded_rect(draw, (10, btn_y, 10 + half_w, btn_y + btn_h), 8, (46, 166, 90, 255))
    _rounded_rect(draw, (16 + half_w, btn_y, w - 10, btn_y + btn_h), 8, (52, 120, 246, 255))

    draw.text((18, btn_y + 4), "Откликнуться", fill=(255, 255, 255, 255), font=btn_font)
    draw.text((22 + half_w, btn_y + 4), "На карте", fill=(255, 255, 255, 255), font=btn_font)

    return card


def patch(base_path: Path, logo_path: Path, out_path: Path) -> None:
    base = Image.open(base_path).convert("RGBA")
    w, h = base.size

    draw = ImageDraw.Draw(base)

    # 1) Экран телефона: один чистый фон + новая карточка (без 500₽/ч)
    draw.rounded_rectangle(PHONE_CONTENT_BOX, radius=10, fill=(236, 240, 245, 255))
    x0, y0, x1, y1 = PHONE_CARD_BOX
    card_w, card_h = x1 - x0, y1 - y0
    card = draw_vacancy_card((card_w, card_h))
    base.alpha_composite(card, (x0, y0))

    # 2) Закрыть старый pill-логотип цветом фона из угла
    corner_rgb = base.convert("RGB").getpixel(LOGO_CORNER_SAMPLE)
    draw.rounded_rectangle(OLD_LOGO_BOX, radius=22, fill=(*corner_rgb, 255))

    # 3) Настоящий круглый логотип — один раз, без дублирования текста
    logo = Image.open(logo_path).convert("RGBA")
    logo_size = int(min(w, h) * 0.21)
    logo = logo.resize((logo_size, logo_size), Image.Resampling.LANCZOS)
    margin = int(min(w, h) * 0.028)
    lx = w - logo_size - margin
    ly = h - logo_size - margin
    base.alpha_composite(logo, (lx, ly))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(out_path, format="PNG", optimize=True)
    print(f"Saved: {out_path} ({w}x{h})")


def main() -> int:
    base_path = ASSETS_CURSOR / BASE_NAME
    if not base_path.is_file():
        base_path = ROOT / "assets" / "channel_images" / BASE_NAME
    logo_path = ROOT / "assets" / "channel_images" / LOGO_NAME
    out_path = ROOT / "assets" / "channel_images" / OUT_NAME

    if not base_path.is_file():
        print(f"Base missing: {base_path}", file=sys.stderr)
        return 1
    if not logo_path.is_file():
        print(f"Logo missing: {logo_path}", file=sys.stderr)
        return 1

    patch(base_path, logo_path, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
