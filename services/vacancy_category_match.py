"""Сопоставление вакансии с подписками: primary + secondary scores."""

from __future__ import annotations

from services.category_scores import parse_category_scores_json

SECONDARY_CATEGORY_MIN_SCORE = 6


def vacancy_matching_user_categories(
    vacancy: dict,
    user_category_codes: list[str] | set[str],
    *,
    min_secondary_score: int = SECONDARY_CATEGORY_MIN_SCORE,
) -> list[str]:
    """Коды из user_category_codes, под которые подходит вакансия."""
    if not user_category_codes:
        return []
    wanted = set(user_category_codes)
    matched: list[str] = []
    primary = vacancy.get("category_code")
    if primary in wanted:
        matched.append(primary)
    scores = parse_category_scores_json(vacancy.get("category_scores_json"))
    for code in wanted:
        if code in matched:
            continue
        if scores.get(code, 0) >= min_secondary_score:
            matched.append(code)
    return matched


def vacancy_category_codes(
    vacancy: dict,
    *,
    min_secondary_score: int = SECONDARY_CATEGORY_MIN_SCORE,
) -> set[str]:
    codes = set()
    primary = vacancy.get("category_code")
    if primary:
        codes.add(primary)
    for code, score in parse_category_scores_json(vacancy.get("category_scores_json")).items():
        if score >= min_secondary_score:
            codes.add(code)
    return codes
