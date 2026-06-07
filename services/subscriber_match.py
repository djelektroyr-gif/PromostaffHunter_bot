"""Сопоставление вакансии с Premium-фильтрами подписчика."""

from __future__ import annotations

import json

from parser import vacancy_matches_user_metro, _normalize_metro_token
from services.filter_prefs import (
    has_active_geo_filter,
    has_active_rate_filter,
    has_active_shift_filter,
    normalize_prefs,
)
from services.geo_radius import vacancy_within_city_radius
from services.shift_match import (
    is_night_start,
    shift_date_matches_today_tomorrow,
    time_start_before,
)


def _parse_geo_tags(vacancy: dict) -> set[str]:
    raw = vacancy.get("geo_tags")
    if isinstance(raw, list):
        return {str(t) for t in raw if t}
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return {str(t) for t in parsed if t}
        except json.JSONDecodeError:
            pass
    return set()


def _vacancy_metro_tokens(vacancy: dict) -> set[str]:
    from parser import extract_metro_tokens

    tags = _parse_geo_tags(vacancy)
    metros = {_normalize_metro_token(t.split(":", 1)[1]) for t in tags if t.startswith("metro:")}
    if metros:
        return {m for m in metros if m}
    text = vacancy.get("message_text") or ""
    address = vacancy.get("address_normalized") or vacancy.get("address") or ""
    return {_normalize_metro_token(t) for t in extract_metro_tokens(f"{text} {address}") if t}


def _matches_geo(vacancy: dict, prefs: dict) -> bool:
    geo = prefs.get("geo") or {}
    if geo.get("include_all") or not has_active_geo_filter(prefs):
        return True

    vac_tags = _parse_geo_tags(vacancy)
    user_cities = set(geo.get("cities") or [])
    radius_km = geo.get("radius_km")

    if not vac_tags:
        if user_cities and radius_km:
            if vacancy_within_city_radius(
                vacancy_lat=vacancy.get("location_lat"),
                vacancy_lon=vacancy.get("location_lon"),
                city_slugs=list(user_cities),
                radius_km=float(radius_km),
            ):
                return True
        return bool(geo.get("show_if_location_unknown", True))

    if user_cities and user_cities & vac_tags:
        return True

    moscow_mode = geo.get("moscow")
    if moscow_mode == "all" and "moscow" in vac_tags:
        return True

    metro_stations = {
        _normalize_metro_token(m)
        for m in (geo.get("metro_stations") or [])
        if m
    }
    vac_metros = _vacancy_metro_tokens(vacancy)
    if metro_stations and vac_metros & metro_stations:
        return True
    if moscow_mode == "metro_only" and "moscow" in vac_tags and vac_metros:
        return True

    if user_cities and "mo" in vac_tags:
        return True

    if user_cities and radius_km:
        if vacancy_within_city_radius(
            vacancy_lat=vacancy.get("location_lat"),
            vacancy_lon=vacancy.get("location_lon"),
            city_slugs=list(user_cities),
            radius_km=float(radius_km),
        ):
            return True

    return False


def _matches_rate(vacancy: dict, prefs: dict) -> bool:
    if not has_active_rate_filter(prefs):
        return True
    category_code = vacancy.get("category_code")
    if not category_code:
        return True
    cat_rates = (prefs.get("rates") or {}).get(category_code)
    if not isinstance(cat_rates, dict):
        return True

    min_hourly = cat_rates.get("min_hourly")
    min_shift = cat_rates.get("min_shift")
    if not min_hourly and not min_shift:
        return True

    rate_hourly = vacancy.get("rate_hourly")
    rate_shift = vacancy.get("rate_shift")
    rate_effective = vacancy.get("rate_effective_hourly")

    if min_hourly and rate_hourly is not None and rate_hourly >= min_hourly:
        return True
    if min_shift and rate_shift is not None and rate_shift >= min_shift:
        return True
    if min_hourly and rate_effective is not None and rate_effective >= min_hourly:
        return True

    if rate_hourly is None and rate_shift is None and rate_effective is None:
        return True

    return False


def _matches_shift(vacancy: dict, prefs: dict) -> bool:
    if not has_active_shift_filter(prefs):
        return True
    shift = prefs.get("shift") or {}
    time_start = vacancy.get("shift_time_start")
    shift_date = vacancy.get("shift_date")

    if shift.get("no_night") and time_start and is_night_start(time_start):
        return False
    if shift.get("only_today_tomorrow") and shift_date:
        if not shift_date_matches_today_tomorrow(shift_date):
            return False
    earliest = shift.get("earliest_start")
    if earliest and time_start and time_start_before(time_start, earliest):
        return False
    return True


def vacancy_matches_subscriber(
    vacancy: dict,
    prefs: dict | None,
    *,
    for_push: bool = False,
    legacy_metro_zones: str | None = None,
) -> tuple[bool, str | None]:
    """True если вакансия подходит под фильтры."""
    del for_push
    if not prefs and legacy_metro_zones:
        ok = vacancy_matches_user_metro(
            vacancy.get("message_text") or "",
            vacancy.get("address_normalized") or vacancy.get("address"),
            legacy_metro_zones,
        )
        return ok, None if ok else "legacy_metro"

    if not prefs:
        return True, None

    prefs = normalize_prefs(prefs)
    if not _matches_geo(vacancy, prefs):
        return False, "geo"
    if not _matches_rate(vacancy, prefs):
        return False, "rate"
    if not _matches_shift(vacancy, prefs):
        return False, "shift"
    return True, None


def build_vacancy_match_dict(
    *,
    message_text: str,
    address: str | None = None,
    address_normalized: str | None = None,
    category_code: str | None = None,
    geo_tags=None,
    rate_hourly: int | None = None,
    rate_shift: int | None = None,
    rate_effective_hourly: int | None = None,
    shift_date: str | None = None,
    shift_time_start: str | None = None,
    location_lat: float | None = None,
    location_lon: float | None = None,
) -> dict:
    return {
        "message_text": message_text or "",
        "address": address,
        "address_normalized": address_normalized,
        "category_code": category_code,
        "geo_tags": geo_tags,
        "rate_hourly": rate_hourly,
        "rate_shift": rate_shift,
        "rate_effective_hourly": rate_effective_hourly,
        "shift_date": shift_date,
        "shift_time_start": shift_time_start,
        "location_lat": location_lat,
        "location_lon": location_lon,
    }
