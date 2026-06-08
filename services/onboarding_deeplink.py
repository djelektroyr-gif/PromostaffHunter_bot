"""Онбординг по deep link: только vac_* (не ref_* — категория там неизвестна)."""

from __future__ import annotations

from db import get_all_categories, get_user_categories, toggle_user_category


def apply_vacancy_deeplink_category_preselect(
    user_id: int,
    category_code: str | None,
    *,
    free_limit: int,
) -> str:
    """
    Предвыбор категории по вакансии из канала (vac_*).
    Не назначает молча — только отмечает в picker; пользователь жмёт «Завершить выбор».
    """
    if not category_code or get_user_categories(user_id):
        return ""
    valid = {c["code"] for c in get_all_categories()}
    if category_code not in valid:
        return ""
    _codes, blocked = toggle_user_category(
        user_id,
        category_code,
        free_limit=free_limit,
    )
    if blocked:
        return ""
    names = {c["code"]: c["name"] for c in get_all_categories()}
    name = names.get(category_code, category_code)
    return (
        f"📌 Вы перешли из канала по вакансии — роль *{name}* уже отмечена.\n"
        "Проверьте и нажмите «✅ Завершить выбор»."
    )
