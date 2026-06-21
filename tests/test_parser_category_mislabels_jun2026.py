# -*- coding: utf-8 -*-
"""Регрессия: неверные категории из обратной связи 19.06.2026."""

from parser import detect_category, evaluate_vacancy, vacancy_matches_category

_POSTER = {"username": "boss", "user_id": 1}


def test_babysitter_animator_not_waiter():
    text = (
        "🔥 СРОЧНО ЭТИ ВЫХОДНЫЕ\n"
        "Аниматор-бейбиситтер в детскую комнату ресторана, делать поделки с детьми!\n"
        "💜 Мега Химки 20.06 и 21.06 (с 14 до 20)\n"
        "Оплата 4100 за обе смены\n"
        "@sunnykiler"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, _POSTER)
    assert ok is True
    assert cat == "animator"
    assert reason == "accepted"
    assert vacancy_matches_category(text, "waiter") is False


def test_restaurant_animator_not_waiter():
    text = (
        "Ресторанный аниматор\n"
        "Творческий мастер - класс/ игры с детьми, присмотр\n"
        "Оплата 2.500\n"
        "@share_hope"
    )
    ok, cat, _, _ = evaluate_vacancy(text, _POSTER)
    assert ok is True
    assert cat == "animator"


def test_restaurant_context_alone_not_waiter_soft_accept():
    text = (
        "Персонал в детскую комнату ресторана на выходные\n"
        "Оплата 4100\n"
        "@boss"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, _POSTER)
    if ok:
        assert cat != "waiter"
    else:
        assert reason.startswith("quality_gate:") or reason == "ambiguous_category"


def test_camp_counselor_rejected():
    text = (
        "СТАРШИЙ #ВОЖАТЫЙ:\n"
        "Ищем старшего вожатого\n"
        "Оплата - 3000\n"
        "Приключенческий лагерь Эволюция (НИЖНИЙ НОВГОРОД)\n"
        "Управление вожатским коллективом\n"
        "@camp"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, _POSTER)
    assert ok is False
    assert cat is None
    assert reason == "camp_educator"


def test_welcome_model_hostess_accepted():
    text = (
        "АКТРИСА/МОДЕЛЬ НА РОЛЬ WELCOME\n"
        "Бренд на мероприятии, встреча гостей\n"
        "Оплата 3500 за смену\n"
        "Нужна видеовизитка + 2-3 фото\n"
        "Девушки 20-28 лет\n"
        "@isaev_den"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, {"username": "isaev_den", "user_id": 1})
    assert ok is True
    assert cat == "hostess"
    assert reason in ("accepted", "soft_accept:hostess")
    assert detect_category(text) == "hostess"


def test_roleplay_security_actress_rejected():
    text = (
        "20.06 «след» охранник, игровые девушки 25-30 лет 4000\n"
        "По сценарию: вышли с бара, садятся в такси. Визжат.\n"
        "Нужны видеовизитка + 2-3 фото.\n"
        "Оплата 4000\n"
        "@cast"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, _POSTER)
    assert ok is False
    assert cat is None
    assert reason == "roleplay_acting"


def test_delivery_courier_rejected():
    text = (
        "Вакансия курьера с ежедневной оплатой. До 15000₽ за смену\n"
        "@boss"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, _POSTER)
    assert ok is False
    assert cat is None
    assert reason == "delivery_courier"


def test_globus_degustation_not_casting_reject():
    text = (
        "⚡ДЕГУСТАЦИЯ АЛКОГОЛЯ⚡ДЕВУШКИ И ЮНОШИ 18+ С МК\n"
        "💰Ставка 600 в час\n"
        "26 июня 17:00-21:00\n"
        "📌 Глобус Красногорск\n"
        "‼️ОТБОР ПО ФОТОКАСТИНГУ‼️\n"
        "пишите в ЛС @Fibs_18 с пометкой ГЛОБУС"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, {"username": "Fibs_18", "user_id": 1})
    assert ok is True
    assert cat == "promoter"
    assert reason in ("accepted", "soft_accept:promoter")


def test_bed_lift_classified_loader():
    text = (
        "прямо сейчас кому 30-40 минут\n"
        "реутов новокосино метро\n"
        "поднять кровать медицинскую пешком на 6 этаж\n"
        "кровать весит 120 кг\n"
        "2 чел 450:4\n"
        "жду скриншот @boss"
    )
    ok, cat, _, _ = evaluate_vacancy(text, {"username": "boss", "user_id": 1})
    assert ok is True
    assert cat == "loader"


def test_pampers_route_driver():
    text = (
        "Подработка в Детский мир — Pampers\n"
        "Строго с авто\n"
        "разместить рекламные материалы по фото-инструкции\n"
        "Синий маршрут — 20 точек — 9 000 ₽\n"
        "Кто готов взять маршрут — напишите\n"
        "@support"
    )
    ok, cat, _, _ = evaluate_vacancy(text, {"username": "support", "user_id": 1})
    assert ok is True
    assert cat == "driver"


def test_message_with_close_footer_still_parses_vacancy():
    from parser import strip_vacancy_close_footer, is_vacancy_closed_text

    text = (
        "ЗАВТРА К 07:00\n"
        "Требуются 3 человека\n"
        "погрузка/разгрузка машин\n"
        "Ставка 550р/час\n"
        "@egorwave\n"
        "ЗАКРЫТО❌❌❌"
    )
    assert is_vacancy_closed_text(text)
    stripped = strip_vacancy_close_footer(text)
    ok, cat, _, _ = evaluate_vacancy(stripped, {"username": "egorwave", "user_id": 1})
    assert ok is True
    assert cat == "loader"

    text = (
        "ИЩЕМ АКТИВНЫХ РЕБЯТ НА УПАКОВКУ\n"
        "📍 Москва, метро Марьино\n"
        "💰 3300 ₽/смена\n"
        "упаковка красок, есть перерывы\n"
        "@Promoorabota"
    )
    assert detect_category(text) == "loader"
    ok, cat, reason, _ = evaluate_vacancy(text, _POSTER)
    assert ok is True
    assert cat == "loader"
    assert reason == "accepted"


def test_fridge_move_loader_not_electrician():
    text = (
        "Ⓜ️ Пионерская / Срочно\n"
        "Задача: спустить холодильник. 7 этаж. Лифта нет.\n"
        "поднять на 15 этаж. есть грузовой лифт\n"
        "ОПЛАТА: 1800\n"
        "@gruzrabota52"
    )
    assert detect_category(text) == "loader"
    ok, cat, reason, _ = evaluate_vacancy(text, {"username": "gruzrabota52", "user_id": 1})
    assert ok is True
    assert cat == "loader"
    assert reason in ("accepted", "soft_accept:loader")


def test_fridge_vitrine_unload_loader():
    text = (
        "Выгрузка и занос на 1й этаж 2х холодильных витрин по 240кг.\n"
        "такелажные ремни. 550/ч\n"
        "@Alex_Gruz"
    )
    assert detect_category(text) == "loader"


def test_courier_own_car_rejected():
    text = (
        "🚗 Работа курьером на своем авто! Заработок без потолка!\n"
        "Ищем курьеров с личными автомобилями для развоза заказов.\n"
        "Минимальная нагрузка: от 22 смен\n"
        "@MarkBondarev_1"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, {"username": "MarkBondarev_1", "user_id": 1})
    assert ok is False
    assert cat is None
    assert reason == "delivery_courier"


def test_facility_cleaning_handyman_not_loader():
    text = (
        "Работа На Сегодня\n"
        "4000 ₽/смена\n"
        "Задача: Уборка На Территории\n"
        "• уборка в цеху\n"
        "@Artem_disp"
    )
    assert detect_category(text) == "handyman"
    ok, cat, _, _ = evaluate_vacancy(text, {"username": "Artem_disp", "user_id": 1})
    assert ok is True
    assert cat == "handyman"


def test_production_packer_handyman_not_loader():
    text = (
        "На завтра 2 упаковщицы Подольск\n"
        "Упаковка роботов-пылесосов\n"
        "с 9:30 до 18:30\n"
        "550 ₽/ч\n"
        "@nmk_everyone"
    )
    assert detect_category(text) == "handyman"
    ok, cat, _, _ = evaluate_vacancy(text, {"username": "nmk_everyone", "user_id": 1})
    assert ok is True
    assert cat == "handyman"


def _unicode_strike(text: str) -> str:
    return "".join(c + "\u0336" if c.isalnum() else c for c in text)


def test_unicode_strikethrough_gruzchik_order_parses_loader():
    plain = (
        "ЗАВТРА в 10:00 БЕЗ ОПОЗДАНИЙ\n"
        "Нужен 1 грузчик. Всего будет - 4\n"
        "Метро Царицыно. От метро 2650 метров.\n"
        "Фронт работ: Выгрузка контейнера\n"
        "Выплата по безналу с 16.00 до 20.00\n"
        "450/4/1800\n"
        "Заказ № 72297 - ТИ - ТИ"
    )
    struck = _unicode_strike(plain)
    from parser import (
        has_hiring_signal,
        is_unicode_strikethrough_closure,
        normalize_ingest_text,
    )

    assert is_unicode_strikethrough_closure(struck)
    assert not has_hiring_signal(struck)
    normalized = normalize_ingest_text(struck)
    assert has_hiring_signal(normalized)
    ok, cat, reason, _ = evaluate_vacancy(
        struck, {"username": "gruzchik_plus", "user_id": 1},
    )
    assert ok is True
    assert cat == "loader"
    assert reason in ("accepted", "soft_accept:loader", "wide_accept:loader")


def test_montazhnik_zil_without_rate_wide_accept():
    text = (
        "Нужен монтажник  на 26 июня 2026 с 9:30 до 13:30\n"
        "Москва, ЗИЛ\n"
        "Разгрузить машину\n"
        "Собрать металический каркас 210*240\n"
        "Собрать каркас из бруса 210*240\n"
        "Для отклика - @Glazunova_Daria01"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, {"username": "Glazunova_Daria01", "user_id": 1})
    assert ok is True
    assert cat in ("booth", "helper", "loader")
    assert reason == "accepted" or reason.startswith(("soft_accept:", "wide_accept:"))
