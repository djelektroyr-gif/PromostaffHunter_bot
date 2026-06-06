"""Текст уведомления о закрытии вакансии для пользователя."""

from __future__ import annotations

from html import escape as escape_html

from services.vacancy_public_text import sanitize_vacancy_public_body


def format_closed_vacancy_notice_html(
    *,
    category_emoji: str,
    category_name: str,
    body: str | None,
    address: str | None = None,
) -> str:
    preview = sanitize_vacancy_public_body(body or "", max_len=280)
    lines = [
        "🔒 <b>Вакансия закрыта</b>",
        "",
        "Смена, на которую вы <b>откликались</b> или получали push, больше не актуальна.",
        "",
        f"{category_emoji} <b>{escape_html(category_name)}</b>",
    ]
    if address and str(address).strip():
        lines.append(f"📍 {escape_html(str(address).strip())}")
    lines.append("")
    if preview:
        lines.append(escape_html(preview))
    else:
        lines.append("<i>Краткое описание недоступно — см. «📨 Мои отклики».</i>")
    lines.append("")
    lines.append("<i>Новый отклик не нужен. В «📨 Мои отклики» карточка помечена как закрытая.</i>")
    return "\n".join(lines)
