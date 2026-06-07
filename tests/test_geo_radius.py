"""Tests for services/geo_radius.py."""

from services.filter_prefs import load_city_catalog
from services.geo_radius import city_centers, haversine_km, vacancy_within_city_radius


def test_haversine_zero():
    assert haversine_km(55.75, 37.62, 55.75, 37.62) == 0.0


def test_vacancy_within_radius_balashiha():
    load_city_catalog.cache_clear()
    city_centers.cache_clear()
    centers = city_centers()
    assert "balashiha" in centers
    lat, lon = centers["balashiha"]
    assert vacancy_within_city_radius(
        vacancy_lat=lat + 0.01,
        vacancy_lon=lon + 0.01,
        city_slugs=["balashiha"],
        radius_km=15,
    ) is True
    assert vacancy_within_city_radius(
        vacancy_lat=56.5,
        vacancy_lon=38.0,
        city_slugs=["balashiha"],
        radius_km=5,
    ) is False
