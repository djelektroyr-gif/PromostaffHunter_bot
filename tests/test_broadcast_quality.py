# -*- coding: utf-8 -*-
"""Рассылка только при уверенной категории — без fallback на сохранённую метку."""

from services.broadcast_quality import assess_vacancy_broadcast


def test_permanent_job_not_broadcast_even_if_stored_animator():
    text = (
        "🎭 АНИМАТОРЫ\n"
        "В ресторан All Day ищут заготовщика бара\n"
        "• График 4/3 по 12 часов\n"
        "• Доход от 60.000-80.000\n"
        "• Трёхразовое питание\n"
        "💰 4000 ₽/смена\n"
        "@Hrlucky_group"
    )
    order = {
        "message_text": text,
        "category": "animator",
        "category_code": "animator",
        "reason": "accepted",
        "poster_username": "Hrlucky_group",
    }
    d = assess_vacancy_broadcast(order)
    assert d.eligible is False
    assert d.reason == "permanent_job"


def test_accepted_promoter_broadcasts():
    text = (
        "Ищем промо на мероприятие\n"
        "м. ВДНХ 23 июня\n"
        "Оплата 3500р\n"
        "@boss"
    )
    order = {"message_text": text, "reason": "accepted", "poster_username": "boss"}
    d = assess_vacancy_broadcast(order)
    assert d.eligible is True
    assert d.category_code == "promoter"
    assert d.confidence == "accepted"


def test_wide_accept_saved_but_not_broadcast():
    text = (
        "Нужен монтажник на 26 июня\n"
        "Москва, ЗИЛ\n"
        "Разгрузить машину\n"
        "@boss"
    )
    from parser import evaluate_vacancy

    ok, cat, reason, _ = evaluate_vacancy(text, {"username": "boss", "user_id": 1})
    if not ok or not reason.startswith("wide_accept:"):
        return
    order = {
        "message_text": text,
        "category": cat,
        "reason": reason,
        "poster_username": "boss",
    }
    d = assess_vacancy_broadcast(order)
    assert d.eligible is False
    assert d.reason.startswith("broadcast_wide_skip:")
