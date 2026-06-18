# -*- coding: utf-8 -*-
"""Бюджет «от X до Y тысяч» и вакансии диджея."""

from parser import evaluate_vacancy
from services.vacancy_rate import has_payment_or_negotiated_rate

EVENT_FAMILY_DJ = (
    "Москва\n"
    "Требуется диджей 4 июля на 5 часов (4+1) с аппаратурой "
    "(колонки, сабы если есть, микрофоны, телевизор)\n\n"
    "Напишите, пожалуйста, одним сообщением цену и промо. "
    "Бюджет от 15 до 25 тысяч"
)


def test_budget_thousands_is_payment_signal():
    assert has_payment_or_negotiated_rate(EVENT_FAMILY_DJ) is True


def test_event_family_dj_accepted_without_poster_username():
    ok, cat, reason, _ = evaluate_vacancy(EVENT_FAMILY_DJ, None)
    assert ok is True
    assert cat == "dj"
    assert reason in ("accepted", "soft_accept:dj", "wide_accept:dj")


def test_event_family_dj_accepted_with_poster():
    ok, cat, reason, _ = evaluate_vacancy(EVENT_FAMILY_DJ, {"username": "boss", "user_id": 1})
    assert ok is True
    assert cat == "dj"
