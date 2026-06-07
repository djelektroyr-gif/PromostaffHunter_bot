"""Тексты автопромo: bundle в assets/, правки в БД или дефолты."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from db import (
    clear_channel_promo_texts_override,
    get_channel_promo_texts_from_db,
    set_channel_promo_texts_in_db,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Bothost: /app/data не из git. Тексты версионируем в assets/.
BUNDLE_PROMO_TEXTS_FILE = _PROJECT_ROOT / "assets" / "channel_promo_texts.json"
LEGACY_PROMO_TEXTS_FILE = _PROJECT_ROOT / "data" / "channel_promo_texts.json"

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


def get_promo_texts_file_candidates() -> list[Path]:
    override = os.getenv("CHANNEL_PROMO_TEXTS_FILE", "").strip()
    paths: list[Path] = []
    if override:
        paths.append(Path(override))
    paths.extend([BUNDLE_PROMO_TEXTS_FILE, LEGACY_PROMO_TEXTS_FILE])
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            out.append(path)
    return out


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


def _load_promo_variants_from_path(path: Path) -> list[str] | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("channel_promo_texts file read failed (%s): %s", path, e)
        return None
    variants = _parse_variants_payload(parsed)
    if not variants:
        logger.warning("channel_promo_texts file empty or invalid: %s", path)
        return None
    return variants


def load_promo_variants_from_file() -> list[str] | None:
    for path in get_promo_texts_file_candidates():
        variants = _load_promo_variants_from_path(path)
        if variants:
            return variants
    return None


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
    """Прочитать JSON из assets/data и сохранить в БД."""
    variants = load_promo_variants_from_file()
    if not variants:
        return None, "Файл не найден или пуст (assets/channel_promo_texts.json)"
    return save_promo_variants_to_db(variants), ""


def reset_promo_texts_to_file_or_defaults() -> tuple[list[str], str]:
    """Убрать правки из БД — дальше bundle-файл или встроенные дефолты."""
    clear_channel_promo_texts_override()
    source = get_promo_texts_source()
    return get_promo_variants(), source


def promo_texts_file_hint() -> str:
    return "assets/channel_promo_texts.json"


def log_promo_texts_status() -> None:
    source = get_promo_texts_source()
    variants = get_promo_variants()
    bundle_exists = BUNDLE_PROMO_TEXTS_FILE.is_file()
    logger.info(
        "Channel promo texts: source=%s variants=%d bundle=%s",
        source,
        len(variants),
        bundle_exists,
    )
    if source == "default" and not bundle_exists:
        logger.warning(
            "Channel promo texts: bundle missing at %s — using built-in defaults",
            BUNDLE_PROMO_TEXTS_FILE,
        )


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
        f"<i>Тексты из git: `{promo_texts_file_hint()}`. "
        f"На Bothost `data/` не подтягивается — правьте assets в репо или админку. "
        f"HTML: &lt;b&gt;, &lt;a href=\"…\"&gt;.</i>"
    )
    return "\n".join(lines)
