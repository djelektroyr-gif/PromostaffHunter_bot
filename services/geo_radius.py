"""Гео-радиус от центра города (фаза 3 Premium-фильтров)."""

from __future__ import annotations

import math
from functools import lru_cache

from services.filter_prefs import load_city_catalog


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@lru_cache(maxsize=1)
def city_centers() -> dict[str, tuple[float, float]]:
    centers: dict[str, tuple[float, float]] = {}
    for item in load_city_catalog():
        slug = item.get("slug")
        lat, lon = item.get("lat"), item.get("lon")
        if slug and lat is not None and lon is not None:
            centers[slug] = (float(lat), float(lon))
    return centers


def vacancy_within_city_radius(
    *,
    vacancy_lat: float | None,
    vacancy_lon: float | None,
    city_slugs: list[str],
    radius_km: float,
) -> bool:
    if not city_slugs or radius_km <= 0:
        return False
    if vacancy_lat is None or vacancy_lon is None:
        return False
    centers = city_centers()
    for slug in city_slugs:
        center = centers.get(slug)
        if not center:
            continue
        if haversine_km(vacancy_lat, vacancy_lon, center[0], center[1]) <= radius_km:
            return True
    return False
