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


def test_packing_job_is_loader_not_promoter():
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
