"""Tests for services/filter_prefs.py."""

from services.filter_prefs import (
    default_prefs,
    format_prefs_summary,
    merge_metro_zones_into_prefs,
    migrate_legacy_metro_zones,
    normalize_prefs,
)


def test_default_prefs_version():
    prefs = default_prefs()
    assert prefs["version"] == 1
    assert prefs["geo"]["include_all"] is True
    assert prefs["apply_to_feed"] is False


def test_normalize_merges_geo():
    raw = {
        "geo": {
            "include_all": False,
            "cities": ["balashiha"],
            "moscow": "all",
            "metro_stations": ["таганская"],
        },
        "apply_to_feed": True,
    }
    prefs = normalize_prefs(raw)
    assert prefs["geo"]["cities"] == ["balashiha"]
    assert prefs["geo"]["moscow"] == "all"
    assert prefs["apply_to_feed"] is True


def test_migrate_legacy_metro():
    geo = migrate_legacy_metro_zones("Таганская, Сокол")
    assert geo["include_all"] is False
    assert geo["moscow"] == "metro_only"
    assert "таганская" in geo["metro_stations"]


def test_merge_metro_into_empty_prefs():
    prefs = default_prefs()
    merged = merge_metro_zones_into_prefs(prefs, "Таганская")
    assert merged["geo"]["metro_stations"]
    assert merged["geo"]["include_all"] is False


def test_format_prefs_summary():
    prefs = normalize_prefs({
        "geo": {"include_all": False, "cities": ["balashiha"], "moscow": "all"},
        "rates": {"loader": {"min_hourly": 480, "min_shift": None}},
    })
    summary = format_prefs_summary(prefs)
    assert "Балашиха" in summary or "balashiha" in summary.lower()
    assert "480" in summary
