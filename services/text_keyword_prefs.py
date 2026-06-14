"""Premium keywords: плюс/минус фразы в тексте вакансии."""

from __future__ import annotations

import re


def parse_keyword_list(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[,;\n]+", raw.lower())
    out: list[str] = []
    for part in parts:
        token = part.strip()
        if len(token) >= 2 and token not in out:
            out.append(token)
    return out[:20]


def format_keyword_list(words: list[str], *, limit: int = 5) -> str:
    if not words:
        return "—"
    preview = ", ".join(words[:limit])
    if len(words) > limit:
        preview += f" (+{len(words) - limit})"
    return preview


def vacancy_matches_keyword_prefs(text: str, prefs: dict) -> bool:
    """True если текст проходит include/exclude фильтры."""
    keywords = prefs.get("keywords") if isinstance(prefs.get("keywords"), dict) else {}
    include = keywords.get("include") or []
    exclude = keywords.get("exclude") or []
    if not include and not exclude:
        return True
    tl = (text or "").lower()
    if exclude and any(word in tl for word in exclude if word):
        return False
    if include:
        return any(word in tl for word in include if word)
    return True
