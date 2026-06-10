"""Контакт заказчика: телефон без deeplink, подсказки для карточки и отклика."""

from __future__ import annotations

import re

_PHONE_APPLY_HINT = (
    "Отклик по телефону: нажмите «✅ Откликнуться», скопируйте текст отклика "
    "и отправьте заказчику по номеру из объявления."
)


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


def employer_contact_needs_manual_apply(contact: str | None) -> bool:
    """Нет автоматической кнопки «открыть чат» в Telegram."""
    if not contact:
        return False
    if is_phone_only_employer_contact(contact):
        return True
    return contact.strip().startswith("tg://user?id=")


def format_phone_display(contact: str) -> str:
    """+7 (925) 480-78-51 — Telegram чаще делает такой номер кликабельным."""
    digits = normalize_ru_phone_digits(contact)
    if not digits or len(digits) != 11:
        return contact.strip()
    return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def phone_vacancy_notice_html() -> str:
    return f"ℹ️ <i>{_PHONE_APPLY_HINT}</i>"


def phone_response_instructions_markdown() -> str:
    return (
        "_Telegram не даёт боту кнопку «открыть чат по номеру». "
        "Нажмите на номер выше (если он стал ссылкой) или найдите заказчика в Telegram по номеру из объявления. "
        "Скопируйте текст отклика ниже и отправьте заказчику._"
    )


def resolve_effective_employer_contact(saved: str | None, vacancy_text: str | None) -> str | None:
    from parser import pick_employer_contact_for_response

    return pick_employer_contact_for_response(saved, vacancy_text)
