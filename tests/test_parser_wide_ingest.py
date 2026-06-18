# -*- coding: utf-8 -*-
"""Широкий ingest: больше recall на входе, фильтры — на выдаче."""

import parser as parser_mod
from parser import evaluate_vacancy

_POSTER = {"username": "boss", "user_id": 1}


def test_wide_accepts_moscow_tc_without_street(monkeypatch):
    monkeypatch.setattr(parser_mod, "PARSER_WIDE_INGEST", True)
    text = (
        "Требуются помощники на площадку\n"
        "📍 Москва, ТЦ Европейский\n"
        "12.06 09:00-18:00\n"
        "400 р/ч\n"
        "@fest_boss"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, _POSTER)
    assert ok is True
    assert cat in ("helper", "misc", "handyman")
    assert reason in ("accepted", "soft_accept:helper", "wide_accept:helper", "wide_accept:misc")


def test_wide_accepts_shift_without_explicit_rate(monkeypatch):
    monkeypatch.setattr(parser_mod, "PARSER_WIDE_INGEST", True)
    text = (
        "На завтра нужны 2 хелпера на монтаж\n"
        "Москва\n"
        "10.06 08:00-20:00\n"
        "Оплата обсудим на месте\n"
        "Пишите в лс"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, None)
    assert ok is True
    assert cat in ("helper", "misc", "handyman")
    assert reason.startswith(("accepted", "soft_accept:", "wide_accept:"))


def test_wide_still_rejects_thin_warehouse_spam(monkeypatch):
    monkeypatch.setattr(parser_mod, "PARSER_WIDE_INGEST", True)
    text = "Смена на склад 10.000р.\n@contact"
    ok, _, reason, _ = evaluate_vacancy(text, _POSTER)
    assert ok is False
    assert reason.startswith("quality_gate:") or reason == "no_payment"


def test_wide_off_keeps_strict_quality_gate(monkeypatch):
    monkeypatch.setattr(parser_mod, "PARSER_WIDE_INGEST", False)
    text = (
        "Работа на производстве крупы на фасовочном конвейере. "
        "Рохля, паллет.\n500 р/час, @warehouse_boss"
    )
    ok, _, reason, _ = evaluate_vacancy(text, _POSTER)
    assert ok is False
    assert reason in ("non_event_labor", "quality_gate:loader", "no_hiring")
