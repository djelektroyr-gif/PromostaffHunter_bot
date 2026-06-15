"""Правила автопубликации вакансий в канал: лимиты, тихие часы, грузчик."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from db import (
    count_channel_vacancy_posts_in_msk_hour,
    get_channel_hourly_limit_loader,
    get_channel_hourly_limit_total,
    get_channel_loader_min_rate,
    get_channel_quiet_hours,
    is_channel_crosspost_enabled,
)
from services.channel_rate import extract_hourly_rate_rub

MSK = ZoneInfo("Europe/Moscow")

SKIP_LABELS = {
    "disabled": "автопост выключен в настройках",
    "quiet_hours": "тихие часы (09:00–22:00 МСК)",
    "hourly_limit": "лимит вакансий в час",
    "loader_quota": "квота грузчик 1/ч",
    "loader_rate": "грузчик: ставка ниже минимума или не указана ₽/ч",
    "already_posted": "уже опубликовано",
    "misc_category": "Другая смена — только в боте",
}


def msk_now() -> datetime:
    return datetime.now(MSK)


def is_within_channel_posting_hours(now: datetime | None = None) -> bool:
    now = now or msk_now()
    start_h, end_h = get_channel_quiet_hours()
    hour = now.hour
    return start_h <= hour < end_h


def evaluate_channel_crosspost(
    category_code: str,
    body: str,
    *,
    force: bool = False,
    already_posted: bool = False,
    now: datetime | None = None,
) -> tuple[bool, str]:
    if force:
        return True, "admin_force"
    if already_posted:
        return False, "already_posted"
    if category_code == "misc":
        return False, "misc_category"
    if not is_channel_crosspost_enabled():
        return False, "disabled"
    now = now or msk_now()
    if not is_within_channel_posting_hours(now):
        return False, "quiet_hours"
    total = count_channel_vacancy_posts_in_msk_hour()
    if total >= get_channel_hourly_limit_total():
        return False, "hourly_limit"
    if category_code == "loader":
        loader_count = count_channel_vacancy_posts_in_msk_hour("loader")
        if loader_count >= get_channel_hourly_limit_loader():
            return False, "loader_quota"
        rate = extract_hourly_rate_rub(body)
        if rate is None or rate < get_channel_loader_min_rate():
            return False, "loader_rate"
    return True, "ok"


def format_skip_reason(code: str) -> str:
    return SKIP_LABELS.get(code, code)
