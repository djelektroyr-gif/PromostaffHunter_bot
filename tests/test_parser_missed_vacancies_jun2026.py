# -*- coding: utf-8 -*-
"""Реальные посты, которые не доходили до подписчиков."""

from parser import evaluate_vacancy, detect_category, vacancy_matches_category
from services.channel_rate import extract_hourly_rate_rub


def _degustation():
    return (
        "СРОЧНО СЕГОДНЯ СТРОГО В 15:40 БЫТЬ НА МЕСТЕ\n"
        "17 и 18.06, 24 и 25.06\n\n"
        "600 р/час\n\n"
        "Дегустация мороженого\n\n"
        " 📍Москва проезд. Берингов, д. 3, стр. 5\n"
        " Время с 16.00-19.00\n\n"
        "Для отклика высылайте :\n"
        "ФИО , Возраст, рост, размер\n"
        "контакт для связи, ссылку в вк."
    )


def _vdnh_promo_helper():
    return (
        "ВДНХ! 18-22 июня\n"
        "каждый день \n"
        "10-21:00\n\n"
        "450 р/час\n\n"
        "Нужен ответвенный  СУПЕР АКТИВНЫЙ И БЫСТРЫЙ промо-хелпер.\n"
        "Работа на площадке на фестивале - делаем, что говорят:\n"
        "Подмена Промо, сбор аудиогидов, контроль на площадке \n\n"
        "Отклики с анкетой и опытом работы @Fd4Daria (https://t.me/Fd4Daria)\n"
        "Только на все дни!"
    )


def _ostankino_helper():
    return (
        "17.07 Требуются хелперы на мероприятие\n"
        "550 р/час \n\n"
        "Локация : Останкино 2.0\n"
        "время 16 до 24:00\n\n"
        "Задачи: курирование на площадках, физическая помощь. \n\n"
        "☎️@Fd4Daria\n"
        "Оплата в течение недели после проекта."
    )


def _loader_colon_rate():
    return (
        "Заявка на завтра 18.06.26 в 9.00 \n\n"
        "Калужской шоссе, д. Десна, ул Рябиновая 9 стр 2-2.\n\n"
        "пять человек на разгрузку машины, коробки по 14 кг. \n\n"
        "на 3 часа примерно работы.\n\n"
        "500:4 -\n\n"
        "сморите внимательно адрес"
    )


def test_degustation_is_promoter_not_merchandiser():
    ok, cat, reason, _ = evaluate_vacancy(_degustation(), None)
    assert ok is True
    assert cat == "promoter"
    assert reason in ("accepted", "soft_accept:promoter", "wide_accept:promoter")
    assert vacancy_matches_category(_degustation(), "promoter") is True


def test_vdnh_promo_helper_primary_is_helper():
    poster = {"username": "Fd4Daria", "user_id": 1}
    ok, cat, _, _ = evaluate_vacancy(_vdnh_promo_helper(), poster)
    assert ok is True
    assert cat == "helper"
    assert vacancy_matches_category(_vdnh_promo_helper(), "helper") is True


def test_ostankino_helper_accepted():
    poster = {"username": "Fd4Daria", "user_id": 1}
    ok, cat, _, _ = evaluate_vacancy(_ostankino_helper(), poster)
    assert ok is True
    assert cat == "helper"


def test_loader_colon_rate_format():
    text = _loader_colon_rate()
    assert extract_hourly_rate_rub(text) == 500
    poster = {"username": "loader_boss", "user_id": 99}
    ok, cat, reason, _ = evaluate_vacancy(text, poster)
    assert ok is True
    assert cat == "loader"
    assert reason in ("accepted", "soft_accept:loader", "wide_accept:loader")
