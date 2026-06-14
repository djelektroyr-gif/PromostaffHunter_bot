"""Контакт заказчика: телефон без deeplink, подсказки для карточки и отклика."""

from __future__ import annotations

import re
from html import escape as escape_html

_PHONE_APPLY_HINT = (
    "Отклик по телефону: нажмите «✅ Откликнуться», скопируйте текст отклика "
    "и отправьте заказчику по номеру из объявления."
)

_TG_USER_ID_RE = re.compile(r"tg://user\?id=(\d+)", re.I)
_MD_TG_USER_RE = re.compile(r"\[([^\]]+)\]\(tg://user\?id=(\d+)\)", re.I)


def normalize_ru_phone_digits(contact: str | None) -> str | None:
    """11 цифр 7XXXXXXXXXX."""
    digits = re.sub(r"\D", "", contact or "")
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits.startswith("7"):
        return digits
    return None


def is_phone_only_employer_contact(contact: str | None) -> bool:
    """Контакт — только номер (не @, не t.me, не форма, не wa.me)."""
    if not contact:
        return False
    c = contact.strip()
    lower = c.lower()
    if c.startswith("@") or c.startswith("tg://"):
        return False
    if lower.startswith("http://") or lower.startswith("https://"):
        return False
    return normalize_ru_phone_digits(c) is not None


def is_tg_user_id_contact(contact: str | None) -> bool:
    if not contact:
        return False
    return bool(_TG_USER_ID_RE.match(contact.strip()))


def parse_tg_user_id(contact: str | None) -> str | None:
    if not contact:
        return None
    m = _TG_USER_ID_RE.match(contact.strip())
    return m.group(1) if m else None


def extract_tg_user_display_name(vacancy_text: str | None, user_id: str) -> str | None:
    """Имя из [Станислав](tg://user?id=…) в тексте вакансии."""
    if not vacancy_text or not user_id:
        return None
    for pattern in (
        re.compile(rf"\[([^\]]+)\]\(tg://user\?id={re.escape(user_id)}[^)]*\)", re.I),
        re.compile(rf"👉\s*\[([^\]]+)\]\(tg://user\?id={re.escape(user_id)}", re.I),
    ):
        m = pattern.search(vacancy_text)
        if m:
            name = m.group(1).strip()
            if name:
                return name
    return None


def coalesce_employer_contact_for_deeplink(
    contact: str | None,
    *,
    poster_username: str | None = None,
) -> str | None:
    """Если в БД tg://user, но есть @username автора — даём кнопку «Открыть чат»."""
    c = (contact or "").strip() or None
    if not c:
        return None
    if is_tg_user_id_contact(c) and poster_username:
        uname = poster_username.strip().lstrip("@")
        if re.fullmatch(r"[a-zA-Z0-9_]{5,32}", uname):
            return f"@{uname}"
    return c


def employer_contact_needs_manual_apply(contact: str | None) -> bool:
    """Нет автоматической кнопки «открыть чат» в Telegram."""
    if not contact:
        return False
    if is_phone_only_employer_contact(contact):
        return True
    return is_tg_user_id_contact(contact)


def format_phone_display(contact: str) -> str:
    """+7 (925) 480-78-51 — Telegram чаще делает такой номер кликабельным."""
    digits = normalize_ru_phone_digits(contact)
    if not digits or len(digits) != 11:
        return contact.strip()
    return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def phone_vacancy_notice_html() -> str:
    return f"ℹ️ <i>{_PHONE_APPLY_HINT}</i>"


def tg_user_vacancy_notice_html(contact: str, vacancy_text: str | None = None) -> str:
    """Кликабельная ссылка на заказчика в HTML-карточке."""
    uid = parse_tg_user_id(contact)
    if not uid:
        return ""
    label = extract_tg_user_display_name(vacancy_text, uid) or "Написать заказчику"
    return (
        f'👉 <a href="tg://user?id={uid}">{escape_html(label)}</a>\n'
        f"ℹ️ <i>Нажмите «✅ Откликнуться» — бот пришлёт черновик и ссылку на заказчика.</i>"
    )


def username_vacancy_contact_html(contact: str) -> str:
    """Публичный @username заказчика в карточке (контакт из объявления)."""
    c = (contact or "").strip()
    if not c.startswith("@"):
        return ""
    uname = c[1:]
    if not re.fullmatch(r"[a-zA-Z0-9_]{5,32}", uname):
        return ""
    return (
        f'👉 <a href="https://t.me/{escape_html(uname)}">{escape_html(c)}</a>\n'
        f"ℹ️ <i>Нажмите «✅ Откликнуться» — бот пришлёт черновик и ссылку на заказчика.</i>"
    )


_ARROW_CONTACT_RE = re.compile(
    r"👉\s+([^@\n]{3,60}?)(?:\s*$|\n)",
    re.MULTILINE,
)


def extract_arrow_display_contact(vacancy_text: str | None) -> str | None:
    """Имя/организация из строки «👉 Glavgruz Admin …» без @."""
    if not vacancy_text:
        return None
    for raw_line in vacancy_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("👉"):
            continue
        if "@" in line or "tg://" in line.lower() or "t.me/" in line.lower():
            continue
        m = _ARROW_CONTACT_RE.match(line)
        if m:
            label = m.group(1).strip(" ·|—–-")
            if len(label) >= 3:
                return label
    return None


def display_contact_vacancy_html(label: str) -> str:
    clean = (label or "").strip()
    if not clean:
        return ""
    return (
        f"👉 {escape_html(clean)}\n"
        f"ℹ️ <i>Нажмите «✅ Откликнуться» — бот пришлёт черновик и ссылку на заказчика.</i>"
    )


def phone_response_instructions_markdown() -> str:
    return (
        "_Telegram не даёт боту кнопку «открыть чат по номеру». "
        "Нажмите на номер выше (если он стал ссылкой) или найдите заказчика в Telegram по номеру из объявления. "
        "Скопируйте текст отклика ниже и отправьте заказчику._"
    )


def tg_user_response_instructions_markdown(
    contact: str,
    *,
    vacancy_text: str | None = None,
    draft_text: str,
) -> str:
    """Черновик отклика: кликабельная ссылка tg://user в тексте сообщения."""
    uid = parse_tg_user_id(contact)
    if not uid:
        return ""
    label = extract_tg_user_display_name(vacancy_text, uid) or "заказчику"
    return (
        f"\n\n👨‍💼 Нажмите на имя — откроется чат: [{label}](tg://user?id={uid})\n"
        "_Кнопки «Открыть чат» у бота нет (ограничение Telegram). "
        "Скопируйте черновик ниже и вставьте в сообщение заказчику._\n\n"
        f"```\n{draft_text}\n```"
    )


def resolve_effective_employer_contact(saved: str | None, vacancy_text: str | None) -> str | None:
    from parser import pick_employer_contact_for_response

    return pick_employer_contact_for_response(saved, vacancy_text)
