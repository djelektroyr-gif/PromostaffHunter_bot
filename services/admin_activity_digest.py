"""Текст дайджеста активности для админа."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape as escape_html

from db import get_activity_digest_data
from parser import format_parser_status_line, get_parser_status_snapshot
from services.bot_events import DIGEST_EVENT_LABELS
from services.channel_policy import msk_now


def _msk_window(hours: int = 24) -> tuple[datetime, datetime]:
    now_msk = msk_now()
    since_msk = now_msk - timedelta(hours=hours)
    since_utc = since_msk.astimezone(timezone.utc)
    return since_utc, now_msk


def build_activity_digest_html(*, hours: int = 24, title_prefix: str = "📊") -> str:
    since_utc, now_msk = _msk_window(hours)
    data = get_activity_digest_data(since_utc=since_utc)
    events = data["events"]
    stats = data["stats"]
    period_label = f"за {hours} ч" if hours != 24 else "за 24 ч"
    parser_line = format_parser_status_line(get_parser_status_snapshot())
    lines = [
        f"<b>{title_prefix} Hunter {period_label}</b> "
        f"({now_msk.strftime('%d.%m.%Y %H:%M')} МСК)",
        "",
        "<b>Активность</b>",
        f"• Активных пользователей: {data['active_users_seen']}",
        f"• Уникальных в событиях: {data['active_users_events']}",
        "<i>Счётчики ниже — сумма действий; один человек может дать несколько /start.</i>",
        f"• /start: {events.get('start', 0)}",
        f"• Анкета готова: {events.get('reg_complete', 0)}",
        f"• Заказчик: {events.get('reg_employer_complete', 0)}",
        f"• Категории выбраны: {events.get('reg_categories_done', 0)}",
        f"• Лента: {events.get('feed_open', 0)}",
        f"• Открыли вакансию: {events.get('vac_open', 0)}",
        f"• Отклики: {data['responses']}",
        "",
        "<b>Обращения</b>",
        f"• Поддержка: {data['support']}",
        f"• Жалобы: {data['complaints']}",
        f"• Ошибки handler: {events.get('handler_error', 0)}",
        "",
        "<b>Всего в базе</b>",
        f"• Подписчиков: {stats['subscribers']} · профилей: {stats['full_profiles']}",
        f"• Откликов: {stats['responses']} · вакансий открытых: {stats['pending_vacancies']}",
        f"• Поддержка в очереди: {stats['pending_support']} · жалобы: {stats['pending_complaints']}",
        "",
        f"<b>Парсер</b>: {escape_html(parser_line)}",
    ]
    extra = [
        (k, v) for k, v in events.items()
        if k not in DIGEST_EVENT_LABELS and k not in (
            "start", "reg_complete", "reg_employer_complete", "reg_categories_done",
            "feed_open", "vac_open", "handler_error", "reg_validation_fail",
        ) and v
    ]
    if extra:
        lines.append("")
        lines.append("<b>Прочее</b>")
        for key, count in sorted(extra, key=lambda x: -x[1])[:6]:
            lines.append(f"• {escape_html(key)}: {count}")
    return "\n".join(lines)


def build_admin_totals_html() -> str:
    """Сводка «всего» — бывший дашборд /admin."""
    from db import count_pending_premium_requests, count_premium_subscribers, get_admin_stats

    stats = get_admin_stats()
    parser = get_parser_status_snapshot()
    premium = count_premium_subscribers()
    return (
        f"<b>👑 Сводка Hunter</b>\n\n"
        f"<b>Парсер</b>\n{escape_html(format_parser_status_line(parser))}\n\n"
        f"<b>База</b>\n"
        f"• Подписчиков: {stats['subscribers']} (💎 {premium})\n"
        f"• Полных профилей: {stats['full_profiles']}\n"
        f"• Откликов: {stats['responses']}\n"
        f"• Вакансий открытых: {stats['pending_vacancies']} / всего {stats['total_vacancies']}\n"
        f"• Жалоб: {stats['pending_complaints']} · поддержка: {stats['pending_support']}\n"
        f"• Premium ожидают: {count_pending_premium_requests()}"
    )
