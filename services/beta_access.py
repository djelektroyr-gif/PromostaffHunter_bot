"""Мягкий запуск: только честный текст в UI, лимиты Free/Premium не меняем."""

from __future__ import annotations

from config import FREE_CATEGORY_LIMIT, PRODUCT_SOFT_LAUNCH
from db import is_user_premium


def is_soft_launch_ui() -> bool:
    """Меньше агрессивного upsell, но лимиты остаются."""
    return PRODUCT_SOFT_LAUNCH


def effective_free_category_limit() -> int:
    return FREE_CATEGORY_LIMIT


def vacancy_push_enabled_for_user(user_id: int) -> bool:
    return is_user_premium(user_id)
