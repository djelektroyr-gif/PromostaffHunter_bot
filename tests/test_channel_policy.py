from datetime import datetime
from zoneinfo import ZoneInfo

from services.channel_policy import evaluate_channel_crosspost, is_within_channel_posting_hours
from services.channel_rate import extract_hourly_rate_rub


def test_extract_hourly_rate():
    assert extract_hourly_rate_rub("Ставка 500 р/ч, оплата сразу") == 500
    assert extract_hourly_rate_rub("550 р/час с 11 до 19") == 550
    assert extract_hourly_rate_rub("оплата 3000 за смену") is None
    assert extract_hourly_rate_rub("400 р/ч") == 400


def test_quiet_hours(monkeypatch):
    monkeypatch.setattr("services.channel_policy.get_channel_quiet_hours", lambda: (9, 22))
    tz = ZoneInfo("Europe/Moscow")
    assert is_within_channel_posting_hours(datetime(2026, 6, 3, 10, 0, tzinfo=tz))
    assert not is_within_channel_posting_hours(datetime(2026, 6, 3, 8, 30, tzinfo=tz))
    assert not is_within_channel_posting_hours(datetime(2026, 6, 3, 22, 0, tzinfo=tz))


def test_loader_rate_gate(monkeypatch):
    monkeypatch.setattr("services.channel_policy.is_channel_crosspost_enabled", lambda: True)
    monkeypatch.setattr("services.channel_policy.count_channel_vacancy_posts_in_msk_hour", lambda cat=None: 0)
    monkeypatch.setattr("services.channel_policy.get_channel_hourly_limit_total", lambda: 6)
    monkeypatch.setattr("services.channel_policy.get_channel_hourly_limit_loader", lambda: 1)
    monkeypatch.setattr("services.channel_policy.get_channel_loader_min_rate", lambda: 450)
    monkeypatch.setattr("services.channel_policy.get_channel_quiet_hours", lambda: (9, 22))

    noon = datetime(2026, 6, 3, 12, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    ok, _ = evaluate_channel_crosspost(
        "loader", "Нужны грузчики, 500 р/ч", now=noon,
    )
    assert ok
    bad, reason = evaluate_channel_crosspost(
        "loader", "Нужны грузчики, 400 р/ч", now=noon,
    )
    assert not bad
    assert reason == "loader_rate"
