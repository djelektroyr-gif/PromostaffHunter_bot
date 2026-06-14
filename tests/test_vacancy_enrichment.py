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
    is_plausible_map_address,
    resolve_map_fields_for_vacancy,
)


def test_extract_address_city_and_street():
    text = "Завтра к 9:00\nМосква, ул. Ленина, 15\nСтавка 500 р/час"
    assert extract_address_normalized(text) == "Москва, ул. Ленина, 15"


def test_extract_address_location_block_multiline():
    text = (
        "**2 парня хелпера**\n"
        "📅 **ДАТА:** Сегодня\n"
        "💰 **ОПЛАТА**\n"
        "450 р/ч\n"
        "📍 **ЛОКАЦИЯ**\n"
        "Страстной бульвар\n"
        "Тверская\n"
        "📋 **ТРЕБОВАНИЯ И ФУНКЦИОНАЛ**\n"
        "Помощь флористам"
    )
    addr = extract_address_normalized(text)
    assert addr is not None
    assert "Страстной бульвар" in addr
    assert "метро" in addr.lower() and "тверская" in addr.lower()
    assert "Москва" in addr
    url = build_maps_url(address_normalized=addr)
    assert url is not None
    assert "yandex.ru/maps" in url


def test_extract_address_boulevard_without_block():
    text = "Срочно на Страстной бульвар, оплата 450 р/ч"
    addr = extract_address_normalized(text)
    assert addr == "Москва, Страстной бульвар"
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
    assert extract_shift_rate_rub("Оплата за проект: 5.400 руб.") == 5400


def test_enrich_zoo_project_rate_and_shift_date():
    text = (
        "📍 Локация: Московский зоопарк\n"
        "📅 Дата: 14.06.2026\n"
        "💰 Оплата за проект: 5.400 руб.\n"
        "⏰ Время работы: с 21:00 до 09:00"
    )
    enrichment = enrich_vacancy_text(text)
    assert enrichment.rate_shift == 5400
    assert enrichment.shift_date == "14.06.2026"
    assert enrichment.shift_time_start == "21:00"


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
    result = backfill_vacancy_enrichment(days=3)
    assert result["enrichment"] >= 1
    row = fetchone(
        "SELECT address_normalized, rate_hourly, enrichment_version FROM vacancies WHERE id = ?",
        ("vac_enrich_1",),
    )
    assert row[0] is not None
    assert row[1] == 500
    assert row[2] == 5


def test_extract_address_vdnh_landmark():
    text = "ВДНХ! 15-22 июня\n5 часов в день\n750 р/час"
    addr = extract_address_normalized(text)
    assert addr is not None
    assert "вднх" in addr.lower()
    assert build_maps_url(address_normalized=addr) is not None


def test_extract_address_vdnh_exhibition_center():
    text = "#Промо 10.06.\nВДНХ выставочный центр\nСтавка: 6000 р"
    addr = extract_address_normalized(text)
    assert addr is not None
    assert "вднх" in addr.lower()


def test_garbage_address_not_on_map():
    garbage = "кандидатов старше 18"
    assert not is_plausible_map_address(garbage)
    assert build_maps_url(address_normalized=garbage) is None
    promo = (
        "Открыта вакансия промоутера.\n"
        "❗️Важно рассмотрим кандидатов старше 18\n"
        "Работа начинается с 19:00"
    )
    vac = {
        "text": promo,
        "address": garbage,
        "address_normalized": garbage,
    }
    fields = resolve_map_fields_for_vacancy(vac)
    assert fields["address_normalized"] is None
    assert build_maps_url(**fields) is None


def test_extract_address_freeform_scattered():
    """Улица и метро в разных местах текста без единого блока."""
    text = (
        "Нужны 2 хелпера на сегодня\n"
        "450 р/ч, выход на Страстной бульвар\n"
        "сбор у м. Тверская\n"
        "пишите в лс"
    )
    addr = extract_address_normalized(text)
    assert addr is not None
    assert "Страстной бульвар" in addr
    assert "тверская" in addr.lower()


def test_extract_address_where_label_multiline():
    text = "Где:\nТЦ Авиапарк\nм. Сокол\n450 р/ч"
    addr = extract_address_normalized(text)
    assert addr is not None
    assert "Авиапарк" in addr


def test_extract_address_pin_inline_one_line():
    text = "Срочно\n📍 Москва, ул. Профсоюзная, 56\n500 р/ч"
    addr = extract_address_normalized(text)
    assert addr is not None
    assert "Профсоюзная" in addr


def test_extract_address_chuykova_bare_line():
    text = (
        "Здравствуйте, на 11:00\n"
        "Маршала Чуйкова 6к1 - требуется грузчик.\n"
        "Оплата 400р час\n"
        "@boss"
    )
    addr = extract_address_normalized(text)
    assert addr is not None
    assert "Чуйкова" in addr
    assert build_maps_url(address_normalized=addr) is not None


def test_extract_address_korolev_not_mo_only():
    text = (
        "**__Сегодня 10:10\n"
        "2 человека\n"
        "Московская область, Королёв, микрорайон Первомайский, Советская улица, 27\n"
        "Оплата:500/4/2000__**"
    )
    addr = extract_address_normalized(text)
    assert addr is not None
    assert "Королёв" in addr or "Королев" in addr
    assert addr != "Мо"


def test_extract_address_yandex_whatshere_point():
    text = (
        "https://yandex.ru/maps?whatshere%5Bpoint%5D=37.59939273397594%2C55.731794895917375\n"
        "На сегодня 2 человека\n"
        "550 р/ч\n"
        "@geo_boss"
    )
    lat, lon = extract_coordinates_from_text(text)
    assert lat is not None and lon is not None
    assert build_maps_url(location_lat=lat, location_lon=lon) is not None


def test_channel_rate_triple_format():
    text = "Оплата:500/4/2000, разгрузка коробок"
    assert extract_hourly_rate_rub(text) == 500
    assert extract_min_hours(text) == 4
    assert extract_shift_rate_rub(text) == 2000
