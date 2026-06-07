"""Фильтры смены для Premium (фаза 3)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_hhmm(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    match = _TIME_RE.match(str(value).strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def is_night_start(time_start: str | None) -> bool:
    """Ночная смена: старт с 22:00 до 05:59."""
    parsed = parse_hhmm(time_start)
    if not parsed:
        return False
    hour, _ = parsed
    return hour >= 22 or hour < 6


def _msk_today_tomorrow(now: datetime | None = None) -> tuple[date, date]:
    now = (now or datetime.now(MSK)).astimezone(MSK)
    today = now.date()
    return today, today + timedelta(days=1)


def shift_date_matches_today_tomorrow(shift_date: str | None, now: datetime | None = None) -> bool:
    if not shift_date:
        return True
    token = shift_date.strip().lower()
    if token in ("today", "сегодня"):
        return True
    if token in ("tomorrow", "завтра"):
        return True
    today, tomorrow = _msk_today_tomorrow(now)
    iso = _ISO_DATE_RE.match(token)
    if iso:
        try:
            d = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return True
        return d in (today, tomorrow)
    return True


def time_start_before(time_start: str | None, earliest: str | None) -> bool:
    start = parse_hhmm(time_start)
    earliest_p = parse_hhmm(earliest)
    if not start or not earliest_p:
        return False
    return start[0] * 60 + start[1] < earliest_p[0] * 60 + earliest_p[1]
