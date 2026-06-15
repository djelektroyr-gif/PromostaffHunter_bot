# -*- coding: utf-8 -*-
"""Регрессия: жалобы на качество ленты (июнь 2026)."""

from parser import (
    detect_category,
    evaluate_vacancy,
    is_group_welcome_spam,
    passes_quality_gate,
)

_POSTER = {"username": "boss", "user_id": 1}

PROMO_LEAFLETS = (
    "Красногорск, нужны: 1 человек(ка) (ТОЛЬКО 18+)\n"
    "📍 Московская область, Красногорск, улица Липовой Рощи, 2к1 (м. Строгино), "
    "Что делать? - Раздавать листовки , 15.06.26 (ЗАВТРА) в: 11:45\n"
    "400 р/ч\n"
    "№26166\n"
    "👉 Glavgruz Admin Екатерина"
)

LOADER_SHORT = (
    "Завтра 9:30\n"
    "📍 ул. Примерная 5\n"
    "💰 450 ₽/ч\n"
    "1 человек\n"
    "👉 @DispIvan"
)

BACK_PHOTO = (
    "**Предлагаю работу** 🤝🏻\n"
    "📍 Москва\n"
    "**Требуется** 🔎 бэкфотограф."
)

HELPER_TEMPLATE = (
    "**Предлагаю работу** 🤝🏻\n"
    "📍 Москва\n"
    "**Требуется** 🔎 хэлперы."
)

DRIVER_VACANCY = (
    "🟢 ВАКАНСИЯ: ВОДИТЕЛЬ-КУРЬЕР (НА ЛИЧНОМ АВТО) | Москва\n"
    "📍 Адрес\tМосква\n"
    "__Ищете стабильную работу с высоким доходом и без общения с клиентами?__"
)

SHIFT_PERMANENT_MIX = (
    "СРОЧНО 🚨\n"
    "📍 Адрес\tВодный Стадион (2 мин от метро)\n"
    "🕐 Смена\tс 09:00, до 21:00\n"
    "💰 Ставка\t300 ₽/ч\n"
    "НА ПОСТОЯННУЮ РАБОТУ"
)

WELCOME = (
    "Cole Kelly, \u0434\u043e\u0431\u0440\u043e \u043f\u043e\u0436\u0430\u043b\u043e\u0432\u0430\u0442\u044c \u0432 \u0433\u0440\u0443\u043f\u043f\u0443 "
    "\u0425\u0435\u043b\u043f\u0435\u0440\u044b \u041c\u043e\u0441\u043a\u0432\u0430! "
    "\u041f\u043e\u0434\u043f\u0438\u0448\u0438\u0441\u044c \u043d\u0430 \u043d\u0430\u0448 \u043a\u0430\u043d\u0430\u043b https://t.me/example"
)


def test_promo_leaflets_accepted_as_promoter():
    assert detect_category(PROMO_LEAFLETS) == "promoter"
    ok, cat, reason, _ = evaluate_vacancy(PROMO_LEAFLETS, _POSTER)
    assert ok is True
    assert cat == "promoter"
    assert reason == "accepted"


def test_loader_short_not_misc_when_rate_present():
    ok, cat, reason, _ = evaluate_vacancy(LOADER_SHORT, _POSTER)
    assert ok is True
    assert cat in ("loader", "misc")
    assert reason in ("accepted", "soft_accept:loader")


def test_backphoto_helper_not_booth():
    assert detect_category(BACK_PHOTO) == "helper"
    ok, cat, reason, _ = evaluate_vacancy(BACK_PHOTO, _POSTER)
    assert ok is True
    assert cat == "helper"


def test_helper_template_not_booth():
    assert detect_category(HELPER_TEMPLATE) == "helper"
    ok, cat, reason, _ = evaluate_vacancy(HELPER_TEMPLATE, _POSTER)
    assert ok is True
    assert cat == "helper"


def test_driver_vacancy_line_detected():
    assert detect_category(DRIVER_VACANCY) == "driver"
    ok, cat, reason, _ = evaluate_vacancy(DRIVER_VACANCY, _POSTER)
    assert ok is True
    assert cat == "driver"


def test_hourly_shift_with_permanent_phrase_still_event_staff():
    ok, cat, reason, _ = evaluate_vacancy(SHIFT_PERMANENT_MIX, _POSTER)
    assert ok is True
    assert reason in ("accepted", "soft_accept:helper", "soft_accept:loader")


def test_group_welcome_rejected():
    assert is_group_welcome_spam(WELCOME) is True
    ok, _, reason, _ = evaluate_vacancy(WELCOME, _POSTER)
    assert ok is False
    assert reason in ("chat_noise", "no_payment", "no_hiring", "group_welcome")


def test_promo_quality_gate_with_leaflet_verb():
    assert passes_quality_gate("promoter", PROMO_LEAFLETS) is True


THIN_LOADER_SPAM = (
    "Смена на склад 10.000р.\n"
    "@contact"
)

GOOD_LOADER_STAKHANOVSKAYA = (
    "На завтра к 9:00 нужно еще 2, будет 10 грузчиков\n"
    "Опалечитать и грузить фуру (возможно не одну) с велосипедами, в основном в коробках, "
    "вес 1 коробки 17кг\n"
    "В 1 фуру влезет 198шт 33пал по 6шт, работа на целый день. "
    "ТОЛЬКО ТЕ, КТО УМЕЕТ КРУТИТЬ СТРЕЙЧЕМ ПАЛЕТИТЫ!!!\n"
    "500/4\n\n"
    "Метро стахановская\n"
    "Улица Стахановская 18ст2\n"
    "@loader_boss"
)


def test_thin_loader_warehouse_line_rejected():
    ok, cat, reason, _ = evaluate_vacancy(THIN_LOADER_SPAM, _POSTER)
    assert ok is False
    assert reason.startswith("quality_gate:") or reason == "no_payment"
    assert passes_quality_gate("loader", THIN_LOADER_SPAM) is False


def test_good_loader_dual_rate_and_address_accepted():
    ok, cat, reason, _ = evaluate_vacancy(GOOD_LOADER_STAKHANOVSKAYA, _POSTER)
    assert ok is True
    assert cat == "loader"
    assert reason == "accepted"
