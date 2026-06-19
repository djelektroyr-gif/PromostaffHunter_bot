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
