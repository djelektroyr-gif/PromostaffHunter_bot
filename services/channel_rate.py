"""Извлечение почасовой ставки из текста вакансии (для фильтра канала)."""

from __future__ import annotations

import re

_EU_THOUSANDS_RE = re.compile(r"\b(\d{1,3}(?:\.\d{3})+)\b")

_PROJECT_RATE_PATTERNS = (
    re.compile(r"оплат\w*\s+за\s+проект\s*[:\s]*(\d[\d\s.,]*)\s*(?:руб|₽|р)", re.I),
    re.compile(r"(\d[\d\s.,]*)\s*(?:руб|₽|р\.?)\s*за\s+проект", re.I),
)

_TRIPLE_RATE_RE = re.compile(
    r"(\d{3,4})\s*/\s*(\d{1,2})\s*/\s*(\d{3,5})",
    re.I,
)
# 500/4 — ₽/ч и минимум часов без суммы смены (частый формат в чатах грузчиков).
_DUAL_RATE_RE = re.compile(
    r"(\d{3,4})\s*/\s*(\d{1,2})(?!\s*/\s*\d)",
    re.I,
)

_HOURLY_PATTERNS = (
    re.compile(r"(\d{3,4})\s*[₽р]\s*/\s*ч(?:ас)?", re.I),
    re.compile(r"(\d{3,4})\s*/\s*ч(?:ас|\.|\b)", re.I),
    re.compile(r"(\d{3,4})\s*(?:руб|₽|р\.?)\s*/?\s*(?:ч(?:ас)?|ч\.?)", re.I),
    re.compile(r"(\d{3,4})\s*р/ч", re.I),
    re.compile(r"(\d{3,4})\s*руб\.?\s*/?\s*ч(?:ас)?\s+по\s+окончан", re.I),
    re.compile(r"ставка\s*(\d{3,4})\s*[₽р]?\s*/?\s*ч", re.I),
    re.compile(r"(\d{3,4})\s*р\.?\s*/\s*ч", re.I),
    re.compile(r"(\d{3,4})\s*(?:руб|₽|р\.?)\.?\s*час", re.I),
)

_SHIFT_PATTERNS = (
    re.compile(r"(\d{3,5})\s*[₽р]\s*/\s*смен", re.I),
    re.compile(r"(\d{3,5})\s*(?:руб|₽|р\.?)\s*/?\s*смен", re.I),
    re.compile(r"(\d{3,5})\s*[₽р]?\s*за\s+смен", re.I),
    re.compile(r"(\d{3,5})\s*р\s*\+\s*костюм", re.I),
    re.compile(r":\s*(\d{3,5})\s*р(?:\.|\b)", re.I),
    re.compile(r"(\d{3,5})\s*р(?:\.|\b)(?:\s*\+|\s*за|\s*$)", re.I),
    re.compile(r"ставка\s*(\d{3,5})\s*[₽р]?(?:\s|$|/|\.)", re.I),
    re.compile(r"(\d{3,5})\s*(?:руб|₽|р\.?)\s*\+\s*костюм", re.I),
)

_MIN_HOURS_PATTERNS = (
    re.compile(r"минимал(?:ка|ь)\s*[:\s]?\s*(\d{1,2})\s*ч", re.I),
    re.compile(r"мин\.?\s*(\d{1,2})\s*ч", re.I),
    re.compile(r"(\d{1,2})\s*ч(?:ас(?:а|ов)?)?\s*мин", re.I),
)


def _normalize_amount_text(text: str) -> str:
    """5.400 руб → 5400 руб — европейский разделитель тысяч в постах."""
    if not text:
        return text

    def repl(match: re.Match[str]) -> str:
        return match.group(1).replace(".", "")

    return _EU_THOUSANDS_RE.sub(repl, text)


def _parse_rub_amount(raw: str) -> int | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    try:
        value = int(digits)
    except ValueError:
        return None
    if 1000 <= value <= 100_000:
        return value
    return None


def extract_hourly_rate_rub(text: str) -> int | None:
    """Максимальная найденная почасовая ставка в ₽/ч или None."""
    if not text:
        return None
    text = _normalize_amount_text(text)
    found: list[int] = []
    triple = _TRIPLE_RATE_RE.search(text)
    if triple:
        try:
            hourly = int(triple.group(1))
            if 200 <= hourly <= 5000:
                found.append(hourly)
        except (ValueError, IndexError):
            pass
    for match in _DUAL_RATE_RE.finditer(text):
        try:
            hourly = int(match.group(1))
        except (ValueError, IndexError):
            continue
        if 200 <= hourly <= 5000:
            found.append(hourly)
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
    text = _normalize_amount_text(text)
    found: list[int] = []
    for pattern in _PROJECT_RATE_PATTERNS:
        for match in pattern.finditer(text):
            value = _parse_rub_amount(match.group(1))
            if value is not None:
                found.append(value)
    triple = _TRIPLE_RATE_RE.search(text)
    if triple:
        try:
            shift = int(triple.group(3))
            if 1000 <= shift <= 100000:
                found.append(shift)
        except (ValueError, IndexError):
            pass
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
    text = _normalize_amount_text(text)
    triple = _TRIPLE_RATE_RE.search(text)
    if triple:
        try:
            hours = int(triple.group(2))
            if 1 <= hours <= 16:
                return hours
        except (ValueError, IndexError):
            pass
    dual = _DUAL_RATE_RE.search(text)
    if dual:
        try:
            hours = int(dual.group(2))
            if 1 <= hours <= 16:
                return hours
        except (ValueError, IndexError):
            pass
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
