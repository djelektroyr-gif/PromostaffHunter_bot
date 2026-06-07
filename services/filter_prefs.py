"""Premium filter preferences — JSON schema, defaults, migration."""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from pathlib import Path

PREFS_VERSION = 1


def default_prefs() -> dict:
    return {
        "version": PREFS_VERSION,
        "apply_to_feed": False,
        "geo": {
            "include_all": True,
            "cities": [],
            "moscow": None,
            "metro_stations": [],
            "show_if_location_unknown": True,
            "radius_km": None,
        },
        "rates": {},
        "shift": {
            "no_night": False,
            "only_today_tomorrow": False,
            "earliest_start": None,
        },
        "notify": {
            "quiet_start": "23:00",
            "quiet_end": "08:00",
            "paused_until": None,
            "category_push": {},
            "digest_after_pause": True,
        },
    }


@lru_cache(maxsize=1)
def load_city_catalog() -> list[dict]:
    path = Path(__file__).resolve().parent.parent / "assets" / "mo_city_catalog.json"
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def city_display_name(slug: str) -> str:
    for item in load_city_catalog():
        if item.get("slug") == slug:
            names = item.get("names") or []
            if names:
                return names[0].title()
    return slug.replace("_", " ").title()


def normalize_metro_list(raw: str) -> list[str]:
    if not raw or not raw.strip():
        return []
    from parser import _normalize_metro_token

    tokens = []
    for part in re.split(r"[,;\n]+", raw):
        token = _normalize_metro_token(part.strip())
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def migrate_legacy_metro_zones(metro_zones: str | None) -> dict:
    geo = default_prefs()["geo"]
    geo["include_all"] = False
    geo["moscow"] = "metro_only"
    geo["metro_stations"] = normalize_metro_list(metro_zones or "")
    return geo


def normalize_prefs(raw: dict | None) -> dict:
    base = default_prefs()
    if not raw:
        return base
    prefs = copy.deepcopy(base)
    if isinstance(raw.get("version"), int):
        prefs["version"] = raw["version"]
    if "apply_to_feed" in raw:
        prefs["apply_to_feed"] = bool(raw["apply_to_feed"])
    geo = raw.get("geo") if isinstance(raw.get("geo"), dict) else {}
    for key in ("include_all", "show_if_location_unknown"):
        if key in geo:
            prefs["geo"][key] = bool(geo[key])
    if geo.get("cities"):
        prefs["geo"]["cities"] = list(dict.fromkeys(geo["cities"]))
    if geo.get("moscow") in (None, "all", "metro_only"):
        prefs["geo"]["moscow"] = geo.get("moscow")
    if geo.get("metro_stations"):
        prefs["geo"]["metro_stations"] = list(dict.fromkeys(geo["metro_stations"]))
    radius = geo.get("radius_km")
    if radius is not None:
        try:
            prefs["geo"]["radius_km"] = int(radius) if int(radius) > 0 else None
        except (TypeError, ValueError):
            prefs["geo"]["radius_km"] = None
    rates = raw.get("rates")
    if isinstance(rates, dict):
        prefs["rates"] = copy.deepcopy(rates)
    shift = raw.get("shift")
    if isinstance(shift, dict):
        s = prefs["shift"]
        for key in ("no_night", "only_today_tomorrow"):
            if key in shift:
                s[key] = bool(shift[key])
        if shift.get("earliest_start"):
            s["earliest_start"] = str(shift["earliest_start"])
        elif "earliest_start" in shift and shift["earliest_start"] is None:
            s["earliest_start"] = None
    notify = raw.get("notify")
    if isinstance(notify, dict):
        n = prefs["notify"]
        for key in ("quiet_start", "quiet_end", "paused_until"):
            if key in notify and notify[key] is not None:
                n[key] = notify[key]
        if "digest_after_pause" in notify:
            n["digest_after_pause"] = bool(notify["digest_after_pause"])
        if "push_block_was_active" in notify:
            n["push_block_was_active"] = bool(notify["push_block_was_active"])
        cp = notify.get("category_push")
        if isinstance(cp, dict):
            n["category_push"] = {
                k: v for k, v in cp.items()
                if v in ("priority", "normal", "feed_only")
            }
    return prefs


def merge_metro_zones_into_prefs(prefs: dict, metro_zones: str | None) -> dict:
    if not metro_zones or not metro_zones.strip():
        return prefs
    geo = prefs.get("geo") or {}
    if geo.get("include_all") and not geo.get("metro_stations") and not geo.get("cities"):
        prefs = normalize_prefs(prefs)
        prefs["geo"] = migrate_legacy_metro_zones(metro_zones)
    elif not geo.get("metro_stations"):
        prefs = normalize_prefs(prefs)
        prefs["geo"]["metro_stations"] = normalize_metro_list(metro_zones)
        prefs["geo"]["include_all"] = False
        if not prefs["geo"].get("moscow"):
            prefs["geo"]["moscow"] = "metro_only"
    return prefs


def has_active_geo_filter(prefs: dict) -> bool:
    geo = prefs.get("geo") or {}
    if geo.get("include_all"):
        return False
    if geo.get("radius_km") and geo.get("cities"):
        return True
    return bool(
        geo.get("cities")
        or geo.get("moscow")
        or geo.get("metro_stations")
    )


def has_active_shift_filter(prefs: dict) -> bool:
    shift = prefs.get("shift") or {}
    return bool(
        shift.get("no_night")
        or shift.get("only_today_tomorrow")
        or shift.get("earliest_start")
    )


def has_active_rate_filter(prefs: dict) -> bool:
    rates = prefs.get("rates") or {}
    for cat_cfg in rates.values():
        if not isinstance(cat_cfg, dict):
            continue
        if cat_cfg.get("min_hourly") or cat_cfg.get("min_shift"):
            return True
    return False


def format_prefs_summary(prefs: dict) -> str:
    prefs = normalize_prefs(prefs)
    parts: list[str] = []
    geo = prefs["geo"]
    if geo.get("include_all") and not has_active_geo_filter(prefs):
        parts.append("гео: везде")
    else:
        geo_bits = []
        for slug in geo.get("cities") or []:
            geo_bits.append(city_display_name(slug))
        if geo.get("moscow") == "all":
            geo_bits.append("Москва")
        elif geo.get("moscow") == "metro_only" and geo.get("metro_stations"):
            geo_bits.append(f"метро ({len(geo['metro_stations'])})")
        elif geo.get("metro_stations"):
            geo_bits.append(f"метро ({len(geo['metro_stations'])})")
        if geo.get("radius_km"):
            geo_bits.append(f"радиус {geo['radius_km']} км")
        parts.append("гео: " + (", ".join(geo_bits) if geo_bits else "выборочно"))
    rate_bits = []
    for code, cfg in (prefs.get("rates") or {}).items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("min_hourly"):
            rate_bits.append(f"{code} от {cfg['min_hourly']} ₽/ч")
        elif cfg.get("min_shift"):
            rate_bits.append(f"{code} от {cfg['min_shift']} ₽/смена")
    if rate_bits:
        parts.append("; ".join(rate_bits[:3]))
    shift = prefs.get("shift") or {}
    shift_bits = []
    if shift.get("no_night"):
        shift_bits.append("без ночи")
    if shift.get("only_today_tomorrow"):
        shift_bits.append("сегодня/завтра")
    if shift.get("earliest_start"):
        shift_bits.append(f"с {shift['earliest_start']}")
    if shift_bits:
        parts.append(", ".join(shift_bits))
    if prefs.get("apply_to_feed"):
        parts.append("фильтры в ленте")
    notify = prefs.get("notify") or {}
    quiet = f"{notify.get('quiet_start', '23:00')}–{notify.get('quiet_end', '08:00')}"
    parts.append(f"quiet {quiet}")
    from services.push_notify import format_busy_line, is_user_busy

    if is_user_busy(prefs):
        busy = format_busy_line(prefs)
        if busy:
            parts.append(f"занят до {busy}")
    return " · ".join(parts) if parts else "по умолчанию (везде)"
