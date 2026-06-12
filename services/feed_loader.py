"""Быстрая лента: один проход по БД, кэш счётчиков, классификация fresh/archive/all."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from config import FEED_ARCHIVE_MAX_HOURS, FEED_FRESH_HOURS, FEED_HISTORY_MAX_HOURS
from db import (
    count_history_vacancies_by_categories,
    get_feed_vacancies_bulk_for_user,
    get_user_categories,
    get_subscriber_filter_prefs_effective,
    is_user_premium,
)

FEED_CACHE_TTL_SEC = 45
_feed_cache: dict[int, tuple[float, FeedSnapshot]] = {}


def _coerce_db_datetime(raw_dt) -> datetime | None:
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


def vacancy_age_hours(vac: dict) -> float | None:
    raw = vac.get("published_at") or vac.get("found_at")
    dt = _coerce_db_datetime(raw)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def vacancy_in_feed_mode(
    vac: dict,
    feed_mode: str,
    *,
    fresh_hours: int = FEED_FRESH_HOURS,
    archive_max_hours: int = FEED_ARCHIVE_MAX_HOURS,
) -> bool:
    age = vacancy_age_hours(vac)
    if feed_mode == "all":
        if age is None:
            return True
        return age <= archive_max_hours
    if age is None:
        return feed_mode == "fresh"
    if feed_mode == "fresh":
        return age <= fresh_hours
    return fresh_hours < age <= archive_max_hours


def _feed_filter_context(user_id: int) -> tuple[bool, dict | None]:
    if not is_user_premium(user_id):
        return False, None
    prefs = get_subscriber_filter_prefs_effective(user_id)
    if not prefs or not prefs.get("apply_to_feed"):
        return False, prefs
    return True, prefs


def _vacancy_passes_feed_filters(vac: dict, cat_code: str, prefs: dict | None) -> bool:
    if not prefs:
        return True
    from services.subscriber_match import vacancy_matches_subscriber

    vac_match = {
        "message_text": vac.get("text") or "",
        "address": vac.get("address"),
        "address_normalized": vac.get("address_normalized"),
        "category_code": cat_code,
        "geo_tags": vac.get("geo_tags"),
        "rate_hourly": vac.get("rate_hourly"),
        "rate_shift": vac.get("rate_shift"),
        "rate_effective_hourly": vac.get("rate_effective_hourly"),
        "shift_date": vac.get("shift_date"),
        "shift_time_start": vac.get("shift_time_start"),
        "location_lat": vac.get("location_lat"),
        "location_lon": vac.get("location_lon"),
    }
    ok, _ = vacancy_matches_subscriber(vac_match, prefs)
    return ok


@dataclass
class FeedSnapshot:
    """Один проход по непросмотренным вакансиям пользователя."""

    categories: list[dict] = field(default_factory=list)
    by_category: dict[str, dict[str, list[dict]]] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=lambda: {"fresh": 0, "archive": 0, "all": 0})
    history_by_category: dict[str, int] = field(default_factory=dict)
    history_total: int = 0
    apply_filters: bool = False


def invalidate_feed_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _feed_cache.clear()
        return
    _feed_cache.pop(user_id, None)


def build_feed_snapshot(
    user_id: int,
    *,
    max_hours: int = FEED_ARCHIVE_MAX_HOURS,
    history_max_hours: int = FEED_HISTORY_MAX_HOURS,
) -> FeedSnapshot:
    categories = get_user_categories(user_id)
    codes = [c["code"] for c in categories]
    cat_by_code = {c["code"]: c for c in categories}
    apply_filters, prefs = _feed_filter_context(user_id)

    by_category: dict[str, dict[str, list[dict]]] = {
        code: {"fresh": [], "archive": [], "all": []} for code in codes
    }
    totals = {"fresh": 0, "archive": 0, "all": 0}

    if codes:
        rows = get_feed_vacancies_bulk_for_user(user_id, codes, max_hours=max_hours)
        for vac in rows:
            cat_code = vac.get("category_code") or ""
            cat = cat_by_code.get(cat_code)
            if not cat:
                continue
            if apply_filters and not _vacancy_passes_feed_filters(vac, cat_code, prefs):
                continue
            vac = dict(vac)
            vac["category"] = cat
            if vacancy_in_feed_mode(vac, "all", archive_max_hours=max_hours):
                by_category[cat_code]["all"].append(vac)
                totals["all"] += 1
            if vacancy_in_feed_mode(vac, "fresh"):
                by_category[cat_code]["fresh"].append(vac)
                totals["fresh"] += 1
            if vacancy_in_feed_mode(vac, "archive", archive_max_hours=max_hours):
                by_category[cat_code]["archive"].append(vac)
                totals["archive"] += 1

        history_by_category = count_history_vacancies_by_categories(
            user_id, codes, max_hours=history_max_hours,
        )
    else:
        history_by_category = {}

    history_total = sum(history_by_category.values())
    return FeedSnapshot(
        categories=categories,
        by_category=by_category,
        totals=totals,
        history_by_category=history_by_category,
        history_total=history_total,
        apply_filters=apply_filters,
    )


def get_feed_snapshot(
    user_id: int,
    *,
    max_hours: int = FEED_ARCHIVE_MAX_HOURS,
    history_max_hours: int = FEED_HISTORY_MAX_HOURS,
    force_refresh: bool = False,
) -> FeedSnapshot:
    now = time.monotonic()
    cached = _feed_cache.get(user_id)
    if not force_refresh and cached and (now - cached[0]) < FEED_CACHE_TTL_SEC:
        return cached[1]
    snap = build_feed_snapshot(
        user_id, max_hours=max_hours, history_max_hours=history_max_hours,
    )
    _feed_cache[user_id] = (now, snap)
    return snap


def snapshot_mode_totals(snap: FeedSnapshot) -> tuple[int, int, int, int]:
    return (
        snap.totals["fresh"],
        snap.totals["archive"],
        snap.totals["all"],
        snap.history_total,
    )


def snapshot_category_count(snap: FeedSnapshot, cat_code: str, feed_mode: str) -> int:
    bucket = snap.by_category.get(cat_code, {})
    return len(bucket.get(feed_mode, []))


def snapshot_collect(
    snap: FeedSnapshot,
    category_codes: list[str] | None,
    feed_mode: str,
) -> list[dict]:
    if category_codes is None:
        codes = [c["code"] for c in snap.categories]
    else:
        codes = list(category_codes)
    out: list[dict] = []
    for code in codes:
        out.extend(snap.by_category.get(code, {}).get(feed_mode, []))
    out.sort(key=lambda v: v.get("published_at") or v.get("found_at") or "", reverse=True)
    return out
