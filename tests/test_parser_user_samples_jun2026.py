"""Примеры вакансий из прод-обратной связи (июнь 2026)."""

from parser import evaluate_vacancy, has_payment_signal

_POSTER = {"username": "boss", "user_id": 1}

DRIVER = (
    "Ищу водителя на своем авто, чтоб 12 июня забрать человека в Москве "
    "в районе 10 утра по адресу Дорожная 46к3, отвезти его на базу отдыха "
    "в п. Заплавье (Селигер, район Осташково) не позднее 16-00 , "
    "подождать до 21-30/22 и привезти обратно в Москве\n"
    "Напишите в личку"
)

WAITER = (
    "Истра! 13.06\n\n"
    "Требуется 1 официант на 3-4 часа, подача напитков, закусок\n"
    "С 15:00 до 18/19:00"
)

PROMO_ROSTOVAYA = (
    "требуется на ЗАВТРВ 10,12 июня\n"
    "Промоутер!\n\n"
    "Задача: Раздача листовок в ростовой кукле\n\n"
    "Время: 17:00-20:00\n\n"
    "Адрес: Кастанаевская 43к2\n\n"
    "Оплата: 400₽/час\n\n"
    "Для записи пишите в личку:\n"
    "Телеграмм: https://t.me/Veronika_adison"
)

PROMO_LONG = (
    "РАЗДАЧА ЛИСТОВОК С РЕЧЕВКОЙ 10-21 ИЮНЯ\n\n"
    "2750 руб смена\n\n"
    "ул. Маршала Бирюзова, д. 17\n\n"
    "Для записи на проект присылайте свои заявки в ЛС с темой : ПАРТИЯ"
)

PROMO_MIC = (
    "Сегодня и завтра! 9 и 10 июня!\n"
    "Нужны ответственные, опытные промоутеры  со своим микрофоном или колонкой !\n\n"
    "9 июня: с 17:00 до 20:00\n"
    "550/р час\n"
    "10 июня: с16:00 до 19:00\n"
    "550/р час\n\n"
    "Для отклика высылайте анкету:\n"
    "Телефон для связи"
)

HELPER_EVENT = (
    "Хелперы на мероприятие  парни с 9,10 июня\n\n"
    "Задачи: разгрузка коробок, помощь на регистрации\n\n"
    "Ставка 400/час\n\n"
    "Адрес: Вильгельма Пика 16\n\n"
    "Для записи в личные сообщения Фио,  номер телефона и фотография."
)


def test_driver_personal_car_negotiated_rate():
    assert has_payment_signal(DRIVER) is True
    ok, cat, reason, _ = evaluate_vacancy(DRIVER, _POSTER)
    assert ok is True
    assert cat == "driver"
    assert reason == "accepted"


def test_waiter_istra_shift_without_rub_line():
    assert has_payment_signal(WAITER) is True
    ok, cat, reason, _ = evaluate_vacancy(WAITER, _POSTER)
    assert ok is True
    assert cat == "waiter"


def test_promo_samples_accepted():
    for text in (PROMO_ROSTOVAYA, PROMO_LONG, PROMO_MIC):
        ok, cat, reason, _ = evaluate_vacancy(text, _POSTER)
        assert ok is True, (text[:40], reason)
        assert cat == "promoter"


def test_helper_event_accepted():
    ok, cat, reason, _ = evaluate_vacancy(HELPER_EVENT, _POSTER)
    assert ok is True
    assert cat == "helper"
