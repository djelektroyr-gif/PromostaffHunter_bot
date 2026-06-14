"""Дашборд ingest: сохранено vs отсеяно за 7 дней."""

from __future__ import annotations

from collections import Counter
from html import escape as escape_html

from db import get_parser_ingest_event_rows, get_vacancy_counts_by_category, get_vacancy_counts_by_chat
from parser import get_stats_for_filter_reports, reject_reason_label
from services.category_catalog import CATEGORY_DISPLAY


def _cat_label(code: str) -> str:
    row = CATEGORY_DISPLAY.get(code)
    if row:
        return f"{row[1]} {row[0]}"
    return code


def build_ingest_dashboard_report(*, days: int = 7) -> str:
    saved_by_cat = get_vacancy_counts_by_category(days)
    saved_by_chat = get_vacancy_counts_by_chat(days)
    events = get_parser_ingest_event_rows(days)

    saved_total_db = sum(r["count"] for r in saved_by_cat)
    rejected_events = [e for e in events if e.get("event") == "parser_rejected"]
    saved_events = [e for e in events if e.get("event") == "parser_saved"]
    rejected_total = len(rejected_events)
    saved_logged = len(saved_events)

    reason_counts: Counter[str] = Counter()
    reject_by_chat: Counter[str] = Counter()
    for row in rejected_events:
        reason = row.get("reason") or "unknown"
        reason_counts[reason] += 1
        chat = row.get("chat_title") or "—"
        reject_by_chat[chat] += 1

    lines = [
        f"📊 <b>Ingest: сохранено vs отсеяно ({days} дн.)</b>",
        "",
        f"✅ <b>Сохранено в БД:</b> {saved_total_db}",
        f"❌ <b>Отсеяно (лог с деплоя):</b> {rejected_total}",
    ]
    if saved_logged:
        lines.append(f"<i>Лог сохранений: {saved_logged} (сверка с БД)</i>")
    if rejected_total == 0 and saved_total_db > 0:
        lines.append(
            "<i>Отсевы до включения лога не видны — смотрите «последний прогон» ниже.</i>"
        )

    last = get_stats_for_filter_reports()
    if last.get("finished_at"):
        lines.extend([
            "",
            f"<b>Последний прогон</b> ({escape_html(str(last.get('run_kind') or '—'))}):",
            f"• в ленту: {last.get('matched', 0)}",
            f"• отсеяно: {last.get('non_relevant', 0)}",
            f"• просмотрено: {last.get('messages_scanned', 0)}",
        ])

    if saved_by_cat:
        lines.extend(["", f"<b>По категориям (БД, {days} дн.):</b>"])
        for row in saved_by_cat[:12]:
            code = row["category_code"]
            lines.append(f"• {escape_html(_cat_label(code))}: <b>{row['count']}</b>")
        if len(saved_by_cat) > 12:
            lines.append(f"  … ещё {len(saved_by_cat) - 12}")

    if reason_counts:
        lines.extend(["", f"<b>Топ причин отсева (лог, {days} дн.):</b>"])
        for reason, count in reason_counts.most_common(8):
            lines.append(
                f"• {escape_html(reject_reason_label(reason))}: <b>{count}</b>"
            )

    if saved_by_chat:
        lines.extend(["", f"<b>Топ чатов — сохранено (БД):</b>"])
        for row in saved_by_chat[:8]:
            lines.append(
                f"• {escape_html(row['source_chat_title'])}: <b>{row['count']}</b>"
            )

    if reject_by_chat:
        lines.extend(["", f"<b>Топ чатов — отсеяно (лог):</b>"])
        for chat, count in reject_by_chat.most_common(8):
            lines.append(f"• {escape_html(chat)}: <b>{count}</b>")

    lines.extend([
        "",
        "<i>После выката на проде: </i><code>/enrich_backfill 30</code>",
    ])
    return "\n".join(lines)
