"""Статистика канала — что доступно через Bot API + наши логи."""

from __future__ import annotations

from html import escape as escape_html
from typing import TYPE_CHECKING

from db import (
    count_channel_member_events,
    get_channel_posts_by_hour_msk,
    get_channel_posts_summary,
    get_latest_subscriber_snapshot,
    get_subscriber_snapshot_near,
    record_subscriber_snapshot,
)

if TYPE_CHECKING:
    from aiogram import Bot

BOT_API_STATS_NOTE = (
    "Просмотры постов и «активность подписчиков по часам» Telegram отдаёт "
    "только в приложении: канал → ⋯ → Статистика (нужны права админа). "
    "Bot API даёт число подписчиков и события join/leave, если Telegram их присылает боту."
)


def _format_hour_activity(buckets: list[tuple[int, int]], top_n: int = 5) -> str:
    ranked = sorted(((h, c) for h, c in buckets if c > 0), key=lambda x: -x[1])
    if not ranked:
        return "— (нет постов бота за период)"
    lines = [f"{h:02d}:00 — {c} пост." for h, c in ranked[:top_n]]
    return ", ".join(lines)


def build_channel_stats_html(
    *,
    member_count: int | None,
    member_count_delta: int | None,
    posts_summary: dict,
    joins: int,
    leaves: int,
    activity_hours_line: str,
) -> str:
    if member_count is None:
        subs_line = "не удалось получить (проверьте права бота в канале)"
    else:
        subs_line = f"<b>{member_count}</b>"
        if member_count_delta is not None:
            sign = "+" if member_count_delta >= 0 else ""
            subs_line += f" ({sign}{member_count_delta} за 7 дн. по снимкам)"
    ps = posts_summary
    return (
        "<b>📊 Статистика @promostaff_agency_job</b>\n\n"
        f"<b>Подписчиков:</b> {subs_line}\n"
        f"<b>Подписались</b> (события бота, 7 дн.): {joins}\n"
        f"<b>Отписались</b> (события бота, 7 дн.): {leaves}\n\n"
        f"<b>Наши посты за 7 дн.:</b> {ps.get('total', 0)}\n"
        f"• вакансии: {ps.get('vacancy', 0)}\n"
        f"• промо: {ps.get('promo', 0)}\n"
        f"• новости/своё: {ps.get('custom', 0)}\n\n"
        f"<b>Часы публикаций бота</b> (когда мы постим, 7 дн.):\n"
        f"{escape_html(activity_hours_line)}\n\n"
        f"<i>{escape_html(BOT_API_STATS_NOTE)}</i>"
    )


async def fetch_and_store_member_count(bot: Bot, chat_id: int) -> int | None:
    try:
        count = await bot.get_chat_member_count(chat_id)
        record_subscriber_snapshot(count)
        return count
    except Exception:
        return None


async def build_channel_stats_report(bot, chat_id: int) -> str:
    member_count = await fetch_and_store_member_count(bot, chat_id)
    if member_count is None:
        member_count = get_latest_subscriber_snapshot()
    old = get_subscriber_snapshot_near(7)
    delta = (member_count - old) if (member_count is not None and old is not None) else None
    posts = get_channel_posts_summary(7)
    joins = count_channel_member_events("join", 7)
    leaves = count_channel_member_events("leave", 7)
    hours_line = _format_hour_activity(get_channel_posts_by_hour_msk(7))
    return build_channel_stats_html(
        member_count=member_count,
        member_count_delta=delta,
        posts_summary=posts,
        joins=joins,
        leaves=leaves,
        activity_hours_line=hours_line,
    )
