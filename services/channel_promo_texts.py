"""Тексты автопромо: файл data/channel_promo_texts.json, правки в БД или дефолты."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from db import (
    clear_channel_promo_texts_override,
    get_channel_promo_texts_from_db,
    set_channel_promo_texts_in_db,
)

logger = logging.getLogger(__name__)

PROMO_TEXTS_FILE = Path(__file__).resolve().parent.parent / "data" / "channel_promo_texts.json"

DEFAULT_PROMO_VARIANTS: list[str] = [
    (
        "<b>🎯 Вакансии под вашу роль — в боте</b>\n\n"
        "Выберите категорию (промо, хелпер, грузчик…), получайте push и "
        "откликайтесь в один тап — без лишних чатов."
    ),
    (
        "<b>📬 PromoStaff Hunter</b>\n\n"
        "Подпишитесь на бота — целевые вакансии по вашим категориям и метро. "
        "Отклик с анкетой прямо из Telegram."
    ),
    (
        "<b>👷 Ищете смену?</b>\n\n"
        "В канале — превью. В боте — полные карточки, фильтры и отклики. "
        "Premium: моментальный push по выбранным станциям метро."
    ),
]


def _normalize_variants(raw: list) -> list[str]:
    out = [str(x).strip() for x in raw if str(x).strip()]
    return out or list(DEFAULT_PROMO_VARIANTS)


def _parse_variants_payload(data) -> list[str] | None:
    if isinstance(data, list):
        return _normalize_variants(data)
    if isinstance(data, dict):
        variants = data.get("variants")
        if isinstance(variants, list):
            return _normalize_variants(variants)
    return None


def load_promo_variants_from_file() -> list[str] | None:
    if not PROMO_TEXTS_FILE.is_file():
        return None
    try:
        raw = PROMO_TEXTS_FILE.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("channel_promo_texts file read failed: %s", e)
        return None
    variants = _parse_variants_payload(parsed)
    if not variants:
        logger.warning("channel_promo_texts file: empty or invalid structure")
        return None
    return variants


def get_promo_texts_source() -> str:
    """db | file | default"""
    if get_channel_promo_texts_from_db():
        return "db"
    if load_promo_variants_from_file():
        return "file"
    return "default"


def get_promo_variants() -> list[str]:
    db_variants = get_channel_promo_texts_from_db()
    if db_variants:
        return db_variants
    file_variants = load_promo_variants_from_file()
    if file_variants:
        return file_variants
    return list(DEFAULT_PROMO_VARIANTS)


def pick_promo_text(slot_index: int) -> str:
    variants = get_promo_variants()
    return variants[slot_index % len(variants)]


def save_promo_variants_to_db(variants: list[str]) -> list[str]:
    normalized = _normalize_variants(variants)
    set_channel_promo_texts_in_db(normalized)
    return normalized


def update_promo_variant_at(index: int, text: str) -> list[str]:
    variants = list(get_promo_variants())
    while len(variants) <= index:
        variants.append(DEFAULT_PROMO_VARIANTS[len(variants) % len(DEFAULT_PROMO_VARIANTS)])
    variants[index] = text.strip()
    return save_promo_variants_to_db(variants)


def import_promo_from_file_to_db() -> tuple[list[str] | None, str]:
    """Прочитать JSON-файл и сохранить в БД. Возвращает (variants, error_message)."""
    variants = load_promo_variants_from_file()
    if not variants:
        return None, f"Файл не найден или пуст: {PROMO_TEXTS_FILE.name}"
    return save_promo_variants_to_db(variants), ""


def reset_promo_texts_to_file_or_defaults() -> tuple[list[str], str]:
    """Убрать правки из БД — дальше файл или встроенные дефолты."""
    clear_channel_promo_texts_override()
    source = get_promo_texts_source()
    return get_promo_variants(), source


def promo_texts_file_hint() -> str:
    return f"data/{PROMO_TEXTS_FILE.name}"


def format_promo_texts_admin_summary() -> str:
    from db import get_channel_promo_times

    variants = get_promo_variants()
    times = get_channel_promo_times()
    source = get_promo_texts_source()
    source_label = {
        "db": "БД (правки из бота)",
        "file": f"файл `{promo_texts_file_hint()}`",
        "default": "встроенные дефолты",
    }[source]
    lines = [
        "<b>✏️ Тексты автопромо</b>",
        f"Источник: {source_label}",
        f"Слотов по расписанию: {len(times)} ({', '.join(times)})",
        "",
        "Порядок: слот 1 → 09:00, слот 2 → 14:00, слот 3 → 20:00 (если три слота).",
        "",
    ]
    for i, text in enumerate(variants[: max(len(times), 3)]):
        slot_time = times[i] if i < len(times) else f"#{i + 1}"
        preview = text.replace("\n", " ")[:80]
        if len(text) > 80:
            preview += "…"
        lines.append(f"<b>{i + 1}. {slot_time}</b> — {preview}")
    lines.append("")
    lines.append(
        f"<i>Файл без деплоя кода: отредактируйте {promo_texts_file_hint()} "
        f"и нажмите «📂 Из файла». HTML: &lt;b&gt;, &lt;a href=\"…\"&gt;.</i>"
    )
    return "\n".join(lines)
