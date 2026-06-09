"""Единый формат карточки вакансии: превью (YouDo-style) и полное описание."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape as escape_html

MSK_TZ = timezone(timedelta(hours=3))
from services.vacancy_enrichment import enrich_vacancy_text, resolve_map_address, extract_shift_date_token
from services.vacancy_public_text import sanitize_vacancy_public_body

_HEADLINE_RE = re.compile(
    r"(?:^|\n)\s*((?:нужн\w*|требу\w*|ищ\w*)\s+\d+\s+[^\n]{3,60})",
    re.I | re.MULTILINE,
)
_HEADCOUNT_INLINE_RE = re.compile(
    r"(?:нужн\w*|требу\w*)\s+\d+\s+х[еэ]лпер\w*",
    re.I,
)
_RATE_LINE_RE = re.compile(r"\d+\s*₽|руб|р/ч|р\.?/?\s*ч", re.I)
_ADDRESS_LINE_RE = re.compile(
    r"(?:^|\n)\s*(?:📍|🚇|м\.|метро|адрес|локация|место)\s*[:\-]?\s*([^\n]{4,80})",
    re.I,
)


@dataclass
class VacancyCardInput:
    category_code: str
    category_name: str
    category_emoji: str
    body: str
    freshness: str
    published_at: str = ""
    address: str | None = None
    address_normalized: str | None = None
    geo_tags: list[str] | None = None
    rate_hourly: int | None = None
    rate_shift: int | None = None
    min_hours: int | None = None
    shift_date: str | None = None
    shift_time_start: str | None = None


def _parse_geo_tags(raw) -> list[str] | None:
    if not raw:
        return None
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(x) for x in data if x]
        except json.JSONDecodeError:
            return None
    return None


def _merge_enrichment(inp: VacancyCardInput) -> VacancyCardInput:
    """Дозаполняет поля из текста, если в БД пусто."""
    enriched = enrich_vacancy_text(
        inp.body or "",
        legacy_address=inp.address,
    )
    shift_date = inp.shift_date or enriched.shift_date or extract_shift_date_token(inp.body or "")
    geo = inp.geo_tags or enriched.geo_tags
    best_addr = resolve_map_address(
        body=inp.body or "",
        address=inp.address,
        address_normalized=inp.address_normalized or enriched.address_normalized,
    )
    return VacancyCardInput(
        category_code=inp.category_code,
        category_name=inp.category_name,
        category_emoji=inp.category_emoji,
        body=inp.body,
        freshness=inp.freshness,
        published_at=inp.published_at,
        address=inp.address,
        address_normalized=best_addr,
        geo_tags=geo,
        rate_hourly=inp.rate_hourly if inp.rate_hourly is not None else enriched.rate_hourly,
        rate_shift=inp.rate_shift if inp.rate_shift is not None else enriched.rate_shift,
        min_hours=inp.min_hours if inp.min_hours is not None else enriched.min_hours,
        shift_date=shift_date,
        shift_time_start=inp.shift_time_start or enriched.shift_time_start,
    )


def format_vacancy_published_at(raw_dt) -> str:
    """Время поста → «дд.мм.гггг чч:мм МСК» для превью."""
    dt = _coerce_db_datetime(raw_dt)
    if dt is None:
        return "сейчас" if raw_dt else ""
    return dt.astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M МСК")


def _coerce_db_datetime(raw_dt):
    if raw_dt is None:
        return None
    if isinstance(raw_dt, datetime):
        if raw_dt.tzinfo is None:
            return raw_dt.replace(tzinfo=timezone.utc)
        return raw_dt.astimezone(timezone.utc)
    if isinstance(raw_dt, date):
        return datetime.combine(raw_dt, datetime.min.time()).replace(tzinfo=timezone.utc)
    if isinstance(raw_dt, str):
        s = raw_dt.strip()
        if not s:
            return None
        for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
            try:
                return datetime.strptime(s[:size], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def card_input_from_push_row(
    row,
    *,
    freshness: str,
    published_at: str = "",
    category_name: str,
    category_emoji: str,
    category_code: str | None = None,
) -> VacancyCardInput:
    code = category_code or (row[5] if row else "promoter") or "promoter"
    pub_display = published_at or (format_vacancy_published_at(row[8]) if row else "")
    geo_raw = row[16] if row and len(row) > 16 else None
    base = VacancyCardInput(
        category_code=code,
        category_name=category_name,
        category_emoji=category_emoji,
        body=(row[0] if row else "") or "",
        freshness=freshness,
        published_at=pub_display,
        address=row[4] if row else None,
        address_normalized=row[13] if row and len(row) > 13 else None,
        geo_tags=_parse_geo_tags(geo_raw),
        rate_hourly=row[17] if row and len(row) > 17 else None,
        rate_shift=row[18] if row and len(row) > 18 else None,
        shift_date=row[20] if row and len(row) > 20 else None,
        shift_time_start=row[21] if row and len(row) > 21 else None,
    )
    return _merge_enrichment(base)


def card_input_from_order(
    order: dict,
    *,
    freshness: str,
    published_at: str,
    category_code: str,
    category_name: str,
    category_emoji: str,
) -> VacancyCardInput:
    base = VacancyCardInput(
        category_code=category_code,
        category_name=category_name,
        category_emoji=category_emoji,
        body=order.get("message_text") or "",
        freshness=freshness,
        published_at=published_at,
        address=order.get("address"),
        address_normalized=order.get("address_normalized"),
        geo_tags=_parse_geo_tags(order.get("geo_tags")),
        rate_hourly=order.get("rate_hourly"),
        rate_shift=order.get("rate_shift"),
        min_hours=order.get("min_hours"),
        shift_date=order.get("shift_date"),
        shift_time_start=order.get("shift_time_start"),
    )
    return _merge_enrichment(base)


def card_input_from_feed_vac(
    vac: dict,
    *,
    freshness: str,
    published_at: str,
    category_name: str,
    category_emoji: str,
) -> VacancyCardInput:
    code = vac.get("category_code") or "promoter"
    pub_display = published_at or format_vacancy_published_at(
        vac.get("published_at") or vac.get("found_at"),
    )
    base = VacancyCardInput(
        category_code=code,
        category_name=category_name,
        category_emoji=category_emoji,
        body=vac.get("text") or "",
        freshness=freshness,
        published_at=pub_display,
        address=vac.get("address"),
        address_normalized=vac.get("address_normalized"),
        geo_tags=_parse_geo_tags(vac.get("geo_tags")),
        rate_hourly=vac.get("rate_hourly"),
        rate_shift=vac.get("rate_shift"),
        shift_date=vac.get("shift_date"),
        shift_time_start=vac.get("shift_time_start"),
    )
    return _merge_enrichment(base)


def _extract_headline(body: str, sanitized_lines: list[str]) -> str | None:
    if not body:
        return None
    m = _HEADCOUNT_INLINE_RE.search(body)
    if m:
        return m.group(0).strip().capitalize()
    m = _HEADLINE_RE.search(body)
    if m:
        return m.group(1).strip()
    for line in sanitized_lines:
        low = line.lower()
        if len(line) < 8 or len(line) > 90:
            continue
        if _RATE_LINE_RE.search(line):
            continue
        if any(k in low for k in ("адрес", "метро", "локация", "заявк", "контакт", "телефон")):
            continue
        return line
    return sanitized_lines[0] if sanitized_lines else None


def _extract_task_hint(sanitized_lines: list[str], headline: str | None) -> str | None:
    head_key = (headline or "").strip().lower()
    for line in sanitized_lines:
        low = line.strip().lower()
        if not low or low == head_key:
            continue
        if low in head_key or head_key in low:
            continue
        if _RATE_LINE_RE.search(line) or _ADDRESS_LINE_RE.search(line):
            continue
        if any(k in low for k in ("заявк", "контакт", "пишите", "напишите", "фио", "возраст")):
            continue
        return line.strip()[:100]
    return None


def _location_line(inp: VacancyCardInput) -> str | None:
    if inp.address_normalized:
        return inp.address_normalized.strip()
    if inp.address and inp.address.strip():
        return inp.address.strip()[:90]
    if inp.geo_tags:
        return inp.geo_tags[0]
    m = _ADDRESS_LINE_RE.search(inp.body or "")
    if m:
        return m.group(1).strip()
    return None


def _rate_line(inp: VacancyCardInput) -> str | None:
    from services.vacancy_rate import format_vacancy_rate_line

    ctx = _merge_enrichment(inp)
    return format_vacancy_rate_line(
        body=ctx.body,
        rate_hourly=ctx.rate_hourly,
        rate_shift=ctx.rate_shift,
        min_hours=ctx.min_hours,
    )


def _sanitized_lines(body: str) -> list[str]:
    clean = sanitize_vacancy_public_body(body or "", max_len=2000)
    return [ln.strip() for ln in clean.splitlines() if ln.strip()]


def _shift_schedule_line(inp: VacancyCardInput) -> str | None:
    """Когда смена: «на завтра, с 10:00»."""
    parts: list[str] = []
    if inp.shift_date == "today":
        parts.append("на сегодня")
    elif inp.shift_date == "tomorrow":
        parts.append("на завтра")
    body_low = (inp.body or "").lower()
    if not parts:
        if "на завтра" in body_low or re.search(r"\bзавтра\b", body_low):
            parts.append("на завтра")
        elif "на сегодня" in body_low or re.search(r"\bсегодня\b", body_low):
            parts.append("на сегодня")
    if inp.shift_time_start:
        parts.append(f"с {inp.shift_time_start}")
    if not parts:
        return None
    return ", ".join(parts)


def build_vacancy_preview_html(inp: VacancyCardInput, *, show_published_at: bool = True) -> str:
    """Компактная карточка — канал, push, лента.

    Канал: роль · свежесть → заголовок → адрес → смена → ставка → задача.
    Бот: то же + строка «Опубликовано: …» сразу под шапкой.
    """
    ctx = _merge_enrichment(inp)
    lines_out: list[str] = [
        f"{ctx.category_emoji} <b>{escape_html(ctx.category_name)}</b> · {escape_html(ctx.freshness)}",
    ]
    if show_published_at and ctx.published_at and ctx.published_at not in ("сейчас", "—"):
        lines_out.append(f"🕐 <i>Опубликовано: {escape_html(ctx.published_at)}</i>")

    sanitized = _sanitized_lines(ctx.body)
    headline = _extract_headline(ctx.body, sanitized)
    if headline:
        lines_out.append(escape_html(headline))

    location = _location_line(ctx)
    if location:
        lines_out.append(f"📍 {escape_html(location)}")

    shift_when = _shift_schedule_line(ctx)
    if shift_when:
        lines_out.append(f"🕐 {escape_html(shift_when)}")

    rate = _rate_line(ctx)
    if rate:
        lines_out.append(f"💰 {escape_html(rate)}")

    task = _extract_task_hint(sanitized, headline)
    if task and task != (headline or ""):
        lines_out.append(escape_html(task))

    if len(lines_out) == 1:
        lines_out.append("Подробности — по кнопке «Открыть вакансию».")
    return "\n".join(lines_out)


def build_vacancy_full_html(inp: VacancyCardInput) -> str:
    """Полное описание — после «Открыть вакансию» или deep link из канала."""
    ctx = _merge_enrichment(inp)
    lines_out: list[str] = [
        f"{ctx.category_emoji} <b>{escape_html(ctx.category_name)}</b> · {escape_html(ctx.freshness)}",
    ]
    if ctx.published_at and ctx.published_at not in ("сейчас", "—"):
        lines_out.append(f"🕐 <i>Опубликовано: {escape_html(ctx.published_at)}</i>")

    description = sanitize_vacancy_public_body(ctx.body or "", max_len=3500)
    if description:
        lines_out.append("")
        lines_out.append(escape_html(description))
    else:
        lines_out.append("")
        lines_out.append("Описание уточняется — нажмите «Откликнуться», чтобы связаться.")

    extras: list[str] = []
    location = _location_line(ctx)
    if location:
        extras.append(f"📍 {escape_html(location)}")
    if ctx.shift_time_start:
        extras.append(f"🕐 Начало: {escape_html(ctx.shift_time_start)}")
    rate = _rate_line(ctx)
    if rate:
        extras.append(f"💰 {escape_html(rate)}")
    if extras:
        lines_out.append("")
        lines_out.extend(extras)

    return "\n".join(lines_out)
