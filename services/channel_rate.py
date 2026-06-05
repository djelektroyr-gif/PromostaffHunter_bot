"""Извлечение почасовой ставки из текста вакансии (для фильтра канала)."""

from __future__ import annotations

import re

_HOURLY_PATTERNS = (
    re.compile(r"(\d{3,4})\s*[₽р]\s*/\s*ч(?:ас)?", re.I),
    re.compile(r"(\d{3,4})\s*/\s*ч(?:ас|\.|\b)", re.I),
    re.compile(r"(\d{3,4})\s*(?:руб|₽|р\.?)\s*/?\s*(?:ч(?:ас)?|ч\.?)", re.I),
    re.compile(r"ставка\s*(\d{3,4})\s*[₽р]?\s*/?\s*ч", re.I),
    re.compile(r"(\d{3,4})\s*р\.?\s*/\s*ч", re.I),
)


def extract_hourly_rate_rub(text: str) -> int | None:
    """Максимальная найденная почасовая ставка в ₽/ч или None."""
    if not text:
        return None
    found: list[int] = []
    for pattern in _HOURLY_PATTERNS:
        for match in pattern.finditer(text):
            try:
                value = int(match.group(1))
            except (ValueError, IndexError):
                continue
            if 200 <= value <= 5000:
                found.append(value)
    return max(found) if found else None
