"""Tests for services/subscriber_match.py."""

from services.subscriber_match import vacancy_matches_subscriber


def _prefs(rates=None, apply_to_feed=False, **geo_kw):
    from services.filter_prefs import default_prefs

    prefs = default_prefs()
    prefs["geo"].update(geo_kw)
    if rates is not None:
        prefs["rates"] = rates
    prefs["apply_to_feed"] = apply_to_feed
    return prefs


def test_no_prefs_passes():
    vac = {"message_text": "работа", "category_code": "loader"}
    ok, reason = vacancy_matches_subscriber(vac, None)
    assert ok is True
    assert reason is None


def test_geo_include_all():
    vac = {"message_text": "работа", "geo_tags": ["balashiha"], "category_code": "loader"}
    prefs = _prefs(include_all=True)
    ok, _ = vacancy_matches_subscriber(vac, prefs)
    assert ok is True


def test_geo_city_match():
    vac = {"message_text": "работа", "geo_tags": ["balashiha"], "category_code": "loader"}
    prefs = _prefs(include_all=False, cities=["balashiha"])
    ok, _ = vacancy_matches_subscriber(vac, prefs)
    assert ok is True


def test_geo_city_miss():
    vac = {"message_text": "работа", "geo_tags": ["khimki"], "category_code": "loader"}
    prefs = _prefs(include_all=False, cities=["balashiha"])
    ok, reason = vacancy_matches_subscriber(vac, prefs)
    assert ok is False
    assert reason == "geo"


def test_geo_unknown_shown_by_default():
    vac = {"message_text": "работа без адреса", "category_code": "loader"}
    prefs = _prefs(include_all=False, cities=["balashiha"], show_if_location_unknown=True)
    ok, _ = vacancy_matches_subscriber(vac, prefs)
    assert ok is True


def test_geo_unknown_hidden():
    vac = {"message_text": "работа без адреса", "category_code": "loader"}
    prefs = _prefs(include_all=False, cities=["balashiha"], show_if_location_unknown=False)
    ok, reason = vacancy_matches_subscriber(vac, prefs)
    assert ok is False
    assert reason == "geo"


def test_moscow_all():
    vac = {"message_text": "москва", "geo_tags": ["moscow"], "category_code": "loader"}
    prefs = _prefs(include_all=False, moscow="all")
    ok, _ = vacancy_matches_subscriber(vac, prefs)
    assert ok is True


def test_metro_match_from_tags():
    vac = {
        "message_text": "работа",
        "geo_tags": ["moscow", "metro:таганская"],
        "category_code": "loader",
    }
    prefs = _prefs(include_all=False, metro_stations=["таганская"])
    ok, _ = vacancy_matches_subscriber(vac, prefs)
    assert ok is True


def test_rate_hourly_pass():
    vac = {
        "message_text": "работа",
        "category_code": "loader",
        "rate_hourly": 500,
        "geo_tags": ["moscow"],
    }
    prefs = _prefs(include_all=True, rates={"loader": {"min_hourly": 480, "min_shift": None}})
    ok, _ = vacancy_matches_subscriber(vac, prefs)
    assert ok is True


def test_rate_hourly_fail():
    vac = {
        "message_text": "работа",
        "category_code": "loader",
        "rate_hourly": 400,
        "geo_tags": ["moscow"],
    }
    prefs = _prefs(include_all=True, rates={"loader": {"min_hourly": 480, "min_shift": None}})
    ok, reason = vacancy_matches_subscriber(vac, prefs)
    assert ok is False
    assert reason == "rate"


def test_rate_unknown_passes():
    vac = {"message_text": "работа", "category_code": "loader", "geo_tags": ["moscow"]}
    prefs = _prefs(include_all=True, rates={"loader": {"min_hourly": 480, "min_shift": None}})
    ok, _ = vacancy_matches_subscriber(vac, prefs)
    assert ok is True


def test_shift_no_night_blocks():
    vac = {
        "message_text": "работа",
        "category_code": "loader",
        "geo_tags": ["moscow"],
        "shift_time_start": "23:00",
    }
    prefs = _prefs(include_all=True)
    prefs["shift"]["no_night"] = True
    ok, reason = vacancy_matches_subscriber(vac, prefs)
    assert ok is False
    assert reason == "shift"


def test_geo_radius_match():
    from services.filter_prefs import load_city_catalog
    from services.geo_radius import city_centers

    load_city_catalog.cache_clear()
    city_centers.cache_clear()
    lat, lon = city_centers()["balashiha"]
    vac = {
        "message_text": "работа",
        "category_code": "loader",
        "geo_tags": [],
        "location_lat": lat + 0.02,
        "location_lon": lon + 0.02,
    }
    prefs = _prefs(
        include_all=False,
        cities=["balashiha"],
        radius_km=20,
        show_if_location_unknown=False,
    )
    ok, _ = vacancy_matches_subscriber(vac, prefs)
    assert ok is True


def test_legacy_metro_fallback():
    vac = {"message_text": "м. Таганская, грузчик", "address": None, "category_code": "loader"}
    ok, reason = vacancy_matches_subscriber(vac, None, legacy_metro_zones="Таганская")
    assert ok is True
    assert reason is None
