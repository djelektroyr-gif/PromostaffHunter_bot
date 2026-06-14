# -*- coding: utf-8 -*-
"""Permanent job soften + ingest dashboard."""

from parser import evaluate_vacancy, is_permanent_job_spam

_POSTER = {"username": "zolotoy_10", "user_id": 1}

PROMO_MIXED_PERMANENT = (
    "\u0420\u0430\u0437\u0434\u0430\u0447\u0430 \u043b\u0438\u0441\u0442\u043e\u0432\u043e\u043a (\u0442\u043e\u043b\u044c\u043a\u043e \u0434\u0435\u0432\u0443\u0448\u043a\u0438)\n"
    "15,15,17,18,19 \u0438\u044e\u043d\u044f \n"
    "\u0421 13:00 \u0434\u043e 15:00 \n"
    "400 \u0440\u0443\u0431\u043b\u0435\u0439 \u0432 \u0447\u0430\u0441.\n"
    "\u0420\u0430\u0431\u043e\u0442\u0430\u0442\u044c \u0432\u0441\u0435 \u0434\u043d\u0438.\n"
    "\u0410\u0434\u0440\u0435\u0441: \u0412\u0438\u043b\u044c\u0433\u0435\u043b\u044c\u043c\u0430 \u043f\u0438\u043a\u0430 11; "
    "\u041d\u0430 \u043f\u043e\u0441\u0442\u043e\u044f\u043d\u043d\u0443\u044e \u043e\u0441\u043d\u043e\u0432\u0443 \u043d\u0443\u0436\u0435\u043d \u0447\u0435\u043b\u043e\u0432\u0435\u043a \n"
    "\u041f\u0440\u043e\u043c\u043e\u0443\u0442\u0435\u0440 \n\n"
    "\u041c\u0435\u0442\u0440\u043e \u0422\u0440\u0435\u0442\u044c\u044f\u043a\u043e\u0432\u0441\u043a\u0430\u044f \n\n"
    "\u0412\u0440\u0435\u043c\u044f: 12:00-16:00\n"
    "\u0412\u0440\u0435\u043c\u044f 18:00-22:00\n\n"
    "\u0421\u0442\u0430\u0432\u043a\u0430 400\u20bd/\u0447\n\n"
    "\u0417\u0430\u043f\u0438\u0441\u044c \u0432 \u041b\u0421: @zolotoy_10"
)


def test_promo_with_soft_permanent_phrase_accepted():
    assert is_permanent_job_spam(PROMO_MIXED_PERMANENT) is False
    ok, cat, reason, _ = evaluate_vacancy(PROMO_MIXED_PERMANENT, _POSTER)
    assert ok is True
    assert cat == "promoter"
    assert reason == "accepted"


def test_real_permanent_still_rejected():
    text = (
        "\u041d\u0430 \u043f\u043e\u0441\u0442\u043e\u044f\u043d\u043d\u0443\u044e \u043e\u0441\u043d\u043e\u0432\u0443 \u043c\u043e\u0439\u0449\u0438\u043a \u043a\u043e\u0442\u043b\u043e\u0432\n"
        "35000 \u0440\u0443\u0431 \u0432 \u043c\u0435\u0441\u044f\u0446, \u043e\u0444\u043e\u0440\u043c\u043b\u0435\u043d\u0438\u0435 \u043f\u043e \u0422\u041a\n"
        "@hr_boss"
    )
    ok, _, reason, _ = evaluate_vacancy(text, {"username": "hr_boss"})
    assert ok is False
    assert reason == "permanent_job"
