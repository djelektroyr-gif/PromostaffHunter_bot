"""Публичное описание вакансии: без @, телефонов и шумовой шапки чужих групп."""

from __future__ import annotations

import re

# Целые строки — служебная шапка ботов/групп (TitanGruz и аналоги).
_BOILERPLATE_LINE_RES = (
    re.compile(r"^\s*⭐️?\s*vip\s*⭐️?", re.I),
    re.compile(r"^\s*№\s*\d+\s*👉", re.I),
    re.compile(r"^\s*создано заказов\s*:", re.I),
    re.compile(r"^\s*зарегистрирован\s*:", re.I),
    re.compile(r"^\s*бот для отправки", re.I),
    re.compile(r"^\s*https?://t\.me/\S+\s*$", re.I),
    # Шапка «название группы-источника» в тексте парсера (📢 Грузчики МОСКВА и т.п.)
    re.compile(r"^\s*📢\s+\S", re.I),
)

_CONTACT_PATTERNS = (
    re.compile(r"@([a-zA-Z0-9_]{5,32})"),
    re.compile(r"\]\(tg://user\?id=\d+\)", re.I),
    re.compile(r"tg://(?:user\?id=\d+|resolve\?domain=[a-zA-Z0-9_]+)", re.I),
    re.compile(r"https?://(?:t\.me|wa\.me|api\.whatsapp\.com)/\S+", re.I),
    re.compile(r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"),
)

# Строка по сути только призыв написать / позвонить.
_CONTACT_CTA_LINE = re.compile(
    r"^\s*(?:☎️?\s*)?(?:"
    r"пишите|напишите|звоните|заявки?|записаться|контакт|связь|whatsapp|ватсап|"
    r"для отклика|связь по заявке"
    r")\b",
    re.I,
)


def _is_boilerplate_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    for pat in _BOILERPLATE_LINE_RES:
        if pat.search(stripped):
            return True
    if stripped.startswith("@") and " " not in stripped:
        return True
    return False


def _strip_contacts_from_line(line: str) -> str:
    s = line
    for pat in _CONTACT_PATTERNS:
        s = pat.sub("", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" ·|—–-")


def _line_is_contact_only(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    cleaned = _strip_contacts_from_line(stripped)
    if not cleaned:
        return True
    if cleaned.startswith("@") or cleaned.startswith("+"):
        return True
    # «✍️записаться:» после удаления @
    bare = re.sub(r"[^\w\s:]", "", cleaned, flags=re.UNICODE).strip().lower()
    if bare in ("записаться", "записаться:", "связь", "контакт", "пишите", "напишите"):
        return True
    if _CONTACT_CTA_LINE.match(cleaned):
        return True
    if stripped.startswith("☎") and len(cleaned) < 4:
        return True
    return len(cleaned) < 3


def _matches_source_title(line: str, source_chat_title: str | None) -> bool:
    title = (source_chat_title or "").strip()
    if not title:
        return False
    stripped = line.strip()
    bare = re.sub(r"^\s*📢\s*", "", stripped, flags=re.I).strip()
    return bare == title or title in stripped


def sanitize_vacancy_public_body(
    text: str,
    *,
    max_len: int = 500,
    source_chat_title: str | None = None,
) -> str:
    """Описание для канала и push-карточек: без контактов и шапки группы."""
    if not text:
        return ""
    kept: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _is_boilerplate_line(line) or _matches_source_title(line, source_chat_title):
            continue
        cleaned = _strip_contacts_from_line(line)
        if _line_is_contact_only(cleaned):
            continue
        kept.append(cleaned)
    body = "\n".join(kept)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if len(body) > max_len:
        body = body[: max_len - 1].rstrip() + "…"
    return body
