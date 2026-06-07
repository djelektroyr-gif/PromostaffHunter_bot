import sqlite3

import db_backend
from db import backfill_vacancy_enrichment, fetchone, init_db, save_vacancy
from services.channel_rate import extract_hourly_rate_rub, extract_min_hours, extract_shift_rate_rub
from services.vacancy_enrichment import (
    build_geo_tags,
    build_maps_url,
    enrich_vacancy_text,
    extract_address_normalized,
    extract_coordinates_from_text,
)


def test_extract_address_city_and_street():
    text = "Завтра к 9:00\nМосква, ул. Ленина, 15\nСтавка 500 р/час"
    assert extract_address_normalized(text) == "Москва, ул. Ленина, 15"


def test_extract_address_explicit_label():
    text = "Адрес: Балашиха, шоссе Энтузиастов, 12\nОплата 480 р/ч"
    assert "Балашиха" in extract_address_normalized(text)


def test_extract_coordinates_yandex_and_geo_uri():
    text = "Точка: https://yandex.ru/maps/?ll=37.6173,55.7558&z=16"
    lat, lon = extract_coordinates_from_text(text)
    assert lat == 55.7558
    assert lon == 37.6173
    geo = "geo:55.751244,37.618423"
    lat2, lon2 = extract_coordinates_from_text(geo)
    assert lat2 == 55.751244
    assert lon2 == 37.618423


def test_enrich_rates_hourly_shift_and_min_hours():
    text = (
        "Нужны грузчики\n"
        "3500 ₽/смена, минималка 4 часа\n"
        "467 руб. час по самозанятости"
    )
    enrichment = enrich_vacancy_text(text)
    assert enrichment.rate_hourly == 467
    assert enrichment.rate_shift == 3500
    assert enrichment.min_hours == 4
    assert enrichment.rate_effective_hourly == 467


def test_channel_rate_helpers():
    text = "550 р/час с 11 до 19, минималка 4 часа"
    assert extract_hourly_rate_rub(text) == 550
    assert extract_min_hours(text) == 4
    assert extract_shift_rate_rub("Ставка 4000 ₽/смена") == 4000


def test_geo_tags_moscow_metro_and_city():
    text = "Срочно, метро Таганская, Москва"
    tags = build_geo_tags(text, "Москва, метро Таганская")
    assert "moscow" in tags
    assert any(t.startswith("metro:") for t in tags)


def test_build_maps_url_prefers_coordinates():
    url = build_maps_url(
        address="Москва",
        location_lat=55.75,
        location_lon=37.61,
    )
    assert url is not None
    assert "ll=37.61,55.75" in url
    url_text = build_maps_url(address_normalized="Балашиха, ТЦ Октябрь")
    assert "text=" in url_text


def test_backfill_updates_recent_vacancies(monkeypatch, tmp_path):
    db_file = tmp_path / "enrich.db"

    def _connect():
        return sqlite3.connect(str(db_file), timeout=10.0)

    monkeypatch.setattr(db_backend, "connect", _connect)
    monkeypatch.setattr(db_backend, "IS_POSTGRES", False)
    init_db()
    save_vacancy(
        "vac_enrich_1",
        "chat1",
        "Chat",
        "loader",
        "Москва, ул. Ленина, 10. Ставка 500 р/час, минималка 4 часа",
        "https://t.me/c/1/1",
        "@boss",
        None,
        False,
        "dedupe_enrich",
        "2026-06-07 10:00:00",
    )
    updated = backfill_vacancy_enrichment(days=3)
    assert updated >= 1
    row = fetchone(
        "SELECT address_normalized, rate_hourly, enrichment_version FROM vacancies WHERE id = ?",
        ("vac_enrich_1",),
    )
    assert row[0] is not None
    assert row[1] == 500
    assert row[2] == 1
