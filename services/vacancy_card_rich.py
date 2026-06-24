"""Rich HTML карточки вакансии (sendRichMessage, Bot API 10.1)."""

from __future__ import annotations

from html import escape as escape_html

from services.vacancy_card import (
    VacancyCardInput,
    _BOT_PREVIEW_NO_CONTACT_CTA,
    _append_phone_apply_notice,
    _extract_headline,
    _extract_task_hint,
    _location_line,
    _merge_enrichment,
    _rate_line,
    _sanitized_lines,
    _shift_schedule_line,
    build_vacancy_full_html,
    build_vacancy_preview_html,
)
from services.vacancy_public_text import sanitize_vacancy_public_body


def _table_row(label: str, value: str) -> str:
    return (
        f"<tr><td><b>{escape_html(label)}</b></td>"
        f"<td>{escape_html(value)}</td></tr>"
    )


def _facts_table(inp: VacancyCardInput) -> str | None:
    ctx = _merge_enrichment(inp)
    rows: list[str] = []
    location = _location_line(ctx)
    if location:
        rows.append(_table_row("📍 Адрес", location))
    shift_when = _shift_schedule_line(ctx)
    if shift_when:
        rows.append(_table_row("🕐 Смена", shift_when))
    rate = _rate_line(ctx)
    if rate:
        rows.append(_table_row("💰 Ставка", rate))
    if not rows:
        return None
    return "<table>" + "".join(rows) + "</table>"


def _footer_lines(inp: VacancyCardInput, *, show_employer_contact: bool = False) -> list[str]:
    if not show_employer_contact:
        return [_BOT_PREVIEW_NO_CONTACT_CTA]
    lines_out: list[str] = []
    _append_phone_apply_notice(lines_out, inp)
    return lines_out


def build_vacancy_preview_rich_html(
    inp: VacancyCardInput,
    *,
    show_published_at: bool = True,
    show_employer_contact: bool = False,
) -> str:
    """Компактная rich-карточка: заголовок, таблица фактов, подсказка."""
    ctx = _merge_enrichment(inp)
    parts: list[str] = [
        f"<h3>{escape_html(ctx.category_emoji)} "
        f"<b>{escape_html(ctx.category_name)}</b> · {escape_html(ctx.freshness)}</h3>",
    ]
    if show_published_at and ctx.published_at and ctx.published_at not in ("сейчас", "—"):
        parts.append(f"<p><i>Опубликовано: {escape_html(ctx.published_at)}</i></p>")

    sanitized = _sanitized_lines(ctx.body)
    headline = _extract_headline(ctx.body, sanitized)
    if headline:
        parts.append(f"<p><b>{escape_html(headline)}</b></p>")

    table = _facts_table(ctx)
    if table:
        parts.append(table)

    task = _extract_task_hint(sanitized, headline)
    if task and task != (headline or ""):
        parts.append(f"<p>{escape_html(task)}</p>")

    footer = _footer_lines(ctx, show_employer_contact=show_employer_contact)
    if footer:
        parts.extend(footer)
    elif len(parts) == 1:
        parts.append("<p>Подробности — по кнопке «Открыть вакансию».</p>")

    parts.append("<footer>PromoStaff Hunter</footer>")
    return "\n".join(parts)


def _format_public_body_rich_html(description: str) -> str:
    lines = [ln.strip() for ln in description.splitlines() if ln.strip()]
    if not lines:
        return ""
    parts = ["<p><b>Описание</b></p>"]
    for line in lines[:45]:
        parts.append(f"<p>{escape_html(line)}</p>")
    return "\n".join(parts)


def build_vacancy_full_rich_html(
    inp: VacancyCardInput,
    *,
    show_employer_contact: bool = False,
) -> str:
    """Полная rich-карточка: факты + видимый текст (без details — Telegram Rich их не раскрывает)."""
    ctx = _merge_enrichment(inp)
    parts: list[str] = [
        f"<h3>{escape_html(ctx.category_emoji)} "
        f"<b>{escape_html(ctx.category_name)}</b> · {escape_html(ctx.freshness)}</h3>",
    ]
    if ctx.published_at and ctx.published_at not in ("сейчас", "—"):
        parts.append(f"<p><i>Опубликовано: {escape_html(ctx.published_at)}</i></p>")

    table = _facts_table(ctx)
    if table:
        parts.append(table)

    description = sanitize_vacancy_public_body(ctx.body or "", max_len=3500)
    body_html = _format_public_body_rich_html(description)
    if body_html:
        parts.append(body_html)
    else:
        parts.append(
            "<p>Описание уточняется — нажмите «Откликнуться», чтобы связаться.</p>"
        )

    footer = _footer_lines(ctx, show_employer_contact=show_employer_contact)
    if footer:
        parts.extend(footer)

    parts.append("<footer>PromoStaff Hunter</footer>")
    return "\n".join(parts)


def build_vacancy_card_html_fallback(
    inp: VacancyCardInput,
    *,
    expanded: bool,
    show_employer_contact: bool = False,
) -> str:
    if expanded:
        return build_vacancy_full_html(inp, show_employer_contact=show_employer_contact)
    return build_vacancy_preview_html(
        inp,
        show_employer_contact=show_employer_contact,
    )


def build_vacancy_card_rich_html(
    inp: VacancyCardInput,
    *,
    expanded: bool,
    show_employer_contact: bool = False,
) -> str:
    if expanded:
        return build_vacancy_full_rich_html(inp, show_employer_contact=show_employer_contact)
    return build_vacancy_preview_rich_html(
        inp,
        show_employer_contact=show_employer_contact,
    )
