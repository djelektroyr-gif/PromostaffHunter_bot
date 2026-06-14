# -*- coding: utf-8 -*-
"""Soft ingest: staff gate OK, quality gate fail -> misc + scores."""

from parser import (
    _eligible_for_soft_ingest,
    evaluate_vacancy,
    passes_quality_gate,
)

_POSTER = {"username": "boss", "user_id": 1}


def test_production_loader_still_rejected_not_softened():
    text = (
        "\u0420\u0430\u0431\u043e\u0442\u0430 \u043d\u0430 \u043f\u0440\u043e\u0438\u0437\u0432\u043e\u0434\u0441\u0442\u0432\u0435 "
        "\u043a\u0440\u0443\u043f\u044b \u043d\u0430 \u0444\u0430\u0441\u043e\u0432\u043e\u0447\u043d\u043e\u043c \u043a\u043e\u043d\u0432\u0435\u0439\u0435\u0440\u0435. "
        "\u0420\u043e\u0445\u043b\u044f, \u043f\u0430\u043b\u043b\u0435\u0442.\n"
        "500 \u0440/\u0447\u0430\u0441, @warehouse_boss"
    )
    assert _eligible_for_soft_ingest("loader", text) is False
    ok, _, reason, _ = evaluate_vacancy(text, _POSTER)
    assert ok is False
    assert reason in ("non_event_labor", "quality_gate:loader", "no_hiring")


def test_soft_ingest_when_quality_gate_fails_with_event_context():
    text = (
        "\u041d\u0430 \u043f\u043b\u043e\u0449\u0430\u0434\u043a\u0435 \u0444\u0435\u0441\u0442\u0438\u0432\u0430\u043b\u044f \u043d\u0443\u0436\u043d\u044b 2 \u0447\u0435\u043b\u043e\u0432\u0435\u043a\u0430\n"
        "\u0437\u0430\u0434\u0430\u0447\u0438: \u0440\u0430\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0430 \u043c\u0435\u0431\u0435\u043b\u0438, \u0441\u043e\u043f\u0440\u043e\u0432\u043e\u0436\u0434\u0435\u043d\u0438\u0435 \u0433\u043e\u0441\u0442\u0435\u0439\n"
        "15.06 08:00-20:00\n"
        "450 \u0440/\u0447\u0430\u0441\n"
        "@fest_boss"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, _POSTER)
    if reason == "accepted":
        assert cat in ("helper", "promoter", "misc")
        return
    assert ok is True
    assert cat in ("helper", "promoter", "misc")
    assert reason.startswith("soft_accept:")


def test_zoo_site_vacancy_accepted():
    text = (
        "\u041f\u043b\u043e\u0449\u0430\u0434\u043a\u0430: \u0437\u043e\u043e\u043f\u0430\u0440\u043a\n"
        "\u041d\u0443\u0436\u043d\u044b 2 \u0441\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a\u0430, \u0440\u0430\u0437\u0433\u0440\u0443\u0437\u043a\u0430\n"
        "10.06 09:00-17:00\n"
        "5400 \u0437\u0430 \u0441\u043c\u0435\u043d\u0443\n"
        "@zoo_hr"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, _POSTER)
    assert ok is True
    assert cat in ("helper", "misc", "loader")
    if reason.startswith("soft_"):
        assert reason.startswith("soft_accept:")


def test_force_category_skips_soft_ingest():
    text = (
        "\u041d\u0430 \u043f\u043b\u043e\u0449\u0430\u0434\u043a\u0435 \u0444\u0435\u0441\u0442\u0438\u0432\u0430\u043b\u044f \u043d\u0443\u0436\u043d\u044b 2 \u0447\u0435\u043b\u043e\u0432\u0435\u043a\u0430\n"
        "450 \u0440/\u0447\u0430\u0441\n"
        "@fest_boss"
    )
    ok, cat, reason, _ = evaluate_vacancy(text, _POSTER, force_category="promoter")
    if not passes_quality_gate("promoter", text):
        assert ok is False
        assert reason == "quality_gate:promoter"
    else:
        assert ok is True
        assert cat == "promoter"
