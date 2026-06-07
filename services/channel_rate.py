"""Извлечение почасовой ставки из текста вакансии (для фильтра канала)."""

from __future__ import annotations

import re

_HOURLY_PATTERNS = (
    re.compile(r"(\d{3,4})\s*[₽р]\s*/\s*ч(?:ас)?", re.I),
    re.compile(r"(\d{3,4})\s*/\s*ч(?:ас|\.|\b)", re.I),
    re.compile(r"(\d{3,4})\s*(?:руб|₽|р\.?)\s*/?\s*(?:ч(?:ас)?|ч\.?)", re.I),
    re.compile(r"ставка\s*(\d{3,4})\s*[₽р]?\s*/?\s*ч", re.I),
    re.compile(r"(\d{3,4})\s*р\.?\s*/\s*ч", re.I),
    re.compile(r"(\d{3,4})\s*(?:руб|₽|р\.?)\.?\s*час", re.I),
)

_SHIFT_PATTERNS = (
    re.compile(r"(\d{3,5})\s*[₽р]\s*/\s*смен", re.I),
    re.compile(r"(\d{3,5})\s*(?:руб|₽|р\.?)\s*/?\s*смен", re.I),
    re.compile(r"(\d{3,5})\s*[₽р]?\s*за\s+смен", re.I),
    re.compile(r"ставка\s*(\d{3,5})\s*[₽р]?(?:\s|$|/|\.)", re.I),
    re.compile(r"(\d{3,5})\s*(?:руб|₽|р\.?)\s*\+\s*костюм", re.I),
)

_MIN_HOURS_PATTERNS = (
    re.compile(r"минимал(?:ка|ь)\s*[:\s]?\s*(\d{1,2})\s*ч", re.I),
    re.compile(r"мин\.?\s*(\d{1,2})\s*ч", re.I),
    re.compile(r"(\d{1,2})\s*ч(?:ас(?:а|ов)?)?\s*мин", re.I),
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


def extract_shift_rate_rub(text: str) -> int | None:
    if not text:
        return None
    found: list[int] = []
    for pattern in _SHIFT_PATTERNS:
        for match in pattern.finditer(text):
            try:
                value = int(match.group(1))
            except (ValueError, IndexError):
                continue
            if 1000 <= value <= 100000:
                found.append(value)
    return max(found) if found else None


def extract_min_hours(text: str) -> int | None:
    if not text:
        return None
    for pattern in _MIN_HOURS_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                hours = int(match.group(1))
            except (ValueError, IndexError):
                continue
            if 1 <= hours <= 16:
                return hours
    range_match = re.search(r"с\s*(\d{1,2})[:.]\d{2}\s*до\s*(\d{1,2})[:.]\d{2}", text, re.I)
    if range_match:
        try:
            start_h, end_h = int(range_match.group(1)), int(range_match.group(2))
            if end_h > start_h:
                return end_h - start_h
        except ValueError:
            pass
    return None
