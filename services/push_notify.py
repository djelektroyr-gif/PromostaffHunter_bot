"""Push-режимы Premium: тихие часы, «занят», приоритет категорий."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from services.filter_prefs import normalize_prefs

MSK = ZoneInfo("Europe/Moscow")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
CATEGORY_PUSH_MODES = ("priority", "normal", "feed_only")


def msk_now() -> datetime:
    return datetime.now(MSK)


def parse_hhmm(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    match = _TIME_RE.match(value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def _minutes_since_midnight(hour: int, minute: int) -> int:
    return hour * 60 + minute


def is_quiet_hours_configured(prefs: dict) -> bool:
    notify = normalize_prefs(prefs).get("notify") or {}
    return bool(notify.get("quiet_configured"))


def is_in_quiet_hours(prefs: dict, now: datetime | None = None) -> bool:
    """Тихие часы по MSK; интервал может пересекать полночь."""
    notify = normalize_prefs(prefs).get("notify") or {}
    if not notify.get("quiet_configured"):
        return False
    start = parse_hhmm(notify.get("quiet_start"))
    end = parse_hhmm(notify.get("quiet_end"))
    if not start or not end:
        return False
    now = (now or msk_now()).astimezone(MSK)
    now_m = _minutes_since_midnight(now.hour, now.minute)
    start_m = _minutes_since_midnight(start[0], start[1])
    end_m = _minutes_since_midnight(end[0], end[1])
    if start_m == end_m:
        return False
    if start_m < end_m:
        return start_m <= now_m < end_m
    return now_m >= start_m or now_m < end_m


def parse_paused_until(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        s = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_user_busy(prefs: dict, now: datetime | None = None) -> bool:
    notify = normalize_prefs(prefs).get("notify") or {}
    until = parse_paused_until(notify.get("paused_until"))
    if not until:
        return False
    now = now or datetime.now(timezone.utc)
    return now < until


def get_category_push_mode(prefs: dict, category_code: str | None) -> str:
    notify = normalize_prefs(prefs).get("notify") or {}
    modes = notify.get("category_push") or {}
    if category_code and category_code in modes:
        mode = modes[category_code]
        if mode in CATEGORY_PUSH_MODES:
            return mode
    return "normal"


def is_push_blocked(prefs: dict, now: datetime | None = None) -> bool:
    """True если push заблокирован тихими часами или «занят»."""
    now_utc = now or datetime.now(timezone.utc)
    return is_in_quiet_hours(prefs, now_utc) or is_user_busy(prefs, now_utc)


def evaluate_push_delivery(
    prefs: dict,
    category_code: str | None,
    *,
    now: datetime | None = None,
) -> tuple[bool, str | None, bool]:
    """
    Можно ли отправить push сейчас.

    Returns:
        (can_push, skip_reason, queue_for_digest)
        skip_reason: feed_only | quiet | busy
        queue_for_digest: True если вакансию стоит учесть в digest (quiet/busy)
    """
    prefs = normalize_prefs(prefs)
    mode = get_category_push_mode(prefs, category_code)
    if mode == "feed_only":
        return False, "feed_only", False

    now_utc = now or datetime.now(timezone.utc)
    if is_user_busy(prefs, now_utc):
        return False, "busy", True
    if is_in_quiet_hours(prefs, now_utc):
        return False, "quiet", True
    return True, None, False


def compute_pause_until_morning(prefs: dict, now: datetime | None = None) -> datetime:
    """«До утра» — до quiet_end пользователя (MSK), следующий слот."""
    notify = normalize_prefs(prefs).get("notify") or {}
    end = parse_hhmm(notify.get("quiet_end")) or (8, 0)
    now = (now or msk_now()).astimezone(MSK)
    target = now.replace(hour=end[0], minute=end[1], second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def compute_pause_for_hours(hours: float, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now + timedelta(hours=hours)


def paused_until_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def format_quiet_hours_line(prefs: dict) -> str:
    notify = normalize_prefs(prefs).get("notify") or {}
    if not notify.get("quiet_configured"):
        return "выкл."
    start = notify.get("quiet_start") or "23:00"
    end = notify.get("quiet_end") or "08:00"
    return f"{start} – {end}"


def format_busy_line(prefs: dict, now: datetime | None = None) -> str | None:
    notify = normalize_prefs(prefs).get("notify") or {}
    until = parse_paused_until(notify.get("paused_until"))
    if not until:
        return None
    now = now or datetime.now(timezone.utc)
    if now >= until:
        return None
    local = until.astimezone(MSK)
    return local.strftime("%d.%m %H:%M МСК")


def format_category_push_label(mode: str) -> str:
    return {
        "priority": "🔥 приоритет",
        "normal": "🔔 push",
        "feed_only": "📂 только лента",
    }.get(mode, "🔔 push")


def parse_quiet_hours_input(text: str) -> tuple[str, str] | None:
    """Парсит «23:00-08:00» или «23:00 – 08:00»."""
    parts = re.split(r"[\-–—]+", text.strip())
    if len(parts) != 2:
        return None
    start = parse_hhmm(parts[0].strip())
    end = parse_hhmm(parts[1].strip())
    if not start or not end:
        return None
    return f"{start[0]:02d}:{start[1]:02d}", f"{end[0]:02d}:{end[1]:02d}"


def next_category_push_mode(current: str | None) -> str:
    order = CATEGORY_PUSH_MODES
    if current not in order:
        return order[0]
    idx = order.index(current)
    return order[(idx + 1) % len(order)]
