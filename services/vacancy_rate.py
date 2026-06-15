"""Ставка в карточке и ingest: явная цифра или «по договорённости»."""

from __future__ import annotations

import re

from services.channel_rate import extract_hourly_rate_rub, extract_shift_rate_rub

_NEGOTIATED_RE = re.compile(r"(?:по\s+)?договор[её]нност", re.I)
_PROJECT_BODY_RE = re.compile(r"оплат\w*\s+за\s+проект", re.I)
_PAYMENT_RATE_RE = re.compile(
    r"(?:"
    r"\d[\d\s.,]*\s*(?:руб\.?|₽|р\.?\/?\s*ч)|"
    r"\d{3,4}\s*р/ч|"
    r"\d{3,4}\s*/\s*р\s*/?\s*ч|"
    r"\d{3,5}\s*р(?:\.|\b)(?:\s*\+|\s*за|\s*/|\s|$)|"
    r"₽\/?\s*ч|р\/\s*ч|руб\.?\s*/\s*ч|"
    r"ставка\s*[:\s]?\s*\d|минималка|"
    r"оплат\w*\s*[:\s].*\d|\d[\d\s.,]*\s*(?:₽|руб)|"
    r"\d{2,5}\s*/\s*\d+\s*/\s*\d{2,5}|"
    r"\d{3,4}\s*/\s*\d{1,2}(?!\s*/\s*\d)|"
    r"\d{3,5}\s*-\s*\d{3,5}\s*(?:₽|руб|р\b|\s|$)|"
    r"(?:заработок|доход)\s*(?:от\s*)?\d[\d\s–—\-]*(?:до|–|-|—)\s*\d+\s*(?:тыс|тысяч)|"
    r"\d[\d\s.,]*\s*(?:тыс|тысяч)\w*\s*(?:руб|₽)?\s*/\s*день|"
    r"оплат\w*[^\n]{0,40}?\d[\d\s.,]*\s*к\b|"
    r"(?:по\s+)?договор[её]нност"
    r")",
    re.I,
)


def is_negotiated_rate_text(text: str) -> bool:
    if not text:
        return False
    tl = text.lower()
    if _NEGOTIATED_RE.search(tl):
        return True
    if re.search(r"ищ\w+\s+.{0,20}водител", tl) and re.search(r"на\s+сво[её]м\s+авто", tl):
        return True
    if re.search(r"водител\w*\s*[\-–—]\s*курьер", tl) and re.search(r"личн\w*\s+авто", tl):
        return True
    if re.search(r"ваканси\w*\s*[:\s].*водител", tl):
        return True
    if "официант" in tl and re.search(r"требу\w+\s+\d+\s+официант", tl):
        return True
    return False


def has_payment_or_negotiated_rate(text: str) -> bool:
    """Ingest gate: цифра в тексте или допустима договорённость."""
    if not text:
        return False
    if _PAYMENT_RATE_RE.search(text):
        return True
    if extract_hourly_rate_rub(text) or extract_shift_rate_rub(text):
        return True
    tl = text.lower()
    for token in ("ставка", "минималка", "гонорар", "зарплат", "з/п", "оплата", "заработок"):
        if token in tl:
            return True
    return is_negotiated_rate_text(text)


def format_vacancy_rate_line(
    *,
    body: str = "",
    rate_hourly: int | None = None,
    rate_shift: int | None = None,
    min_hours: int | None = None,
) -> str | None:
    if rate_hourly:
        return f"{rate_hourly} ₽/ч"
    if rate_shift:
        if _PROJECT_BODY_RE.search(body or ""):
            return f"{rate_shift} ₽ за проект"
        suffix = f" · от {min_hours} ч" if min_hours else ""
        return f"{rate_shift} ₽/смена{suffix}"
    if is_negotiated_rate_text(body):
        return "по договорённости"
    return None
