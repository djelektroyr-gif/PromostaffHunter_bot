"""Tests for services/push_notify.py."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from services.filter_prefs import default_prefs, normalize_prefs
from services.push_notify import (
    compute_pause_until_morning,
    evaluate_push_delivery,
    is_in_quiet_hours,
    is_user_busy,
    parse_quiet_hours_input,
)

MSK = ZoneInfo("Europe/Moscow")


def _prefs(**notify_kw):
    prefs = default_prefs()
    prefs["notify"].update(notify_kw)
    if notify_kw.get("quiet_start") or notify_kw.get("quiet_end"):
        prefs["notify"]["quiet_configured"] = notify_kw.get("quiet_configured", True)
    return normalize_prefs(prefs)


def test_quiet_hours_overnight():
    prefs = _prefs(quiet_start="23:00", quiet_end="08:00")
    late = datetime(2026, 6, 3, 23, 30, tzinfo=MSK)
    early = datetime(2026, 6, 4, 7, 0, tzinfo=MSK)
    noon = datetime(2026, 6, 4, 12, 0, tzinfo=MSK)
    assert is_in_quiet_hours(prefs, late) is True
    assert is_in_quiet_hours(prefs, early) is True
    assert is_in_quiet_hours(prefs, noon) is False


def test_quiet_hours_same_day():
    prefs = _prefs(quiet_start="13:00", quiet_end="14:00")
    inside = datetime(2026, 6, 3, 13, 30, tzinfo=MSK)
    outside = datetime(2026, 6, 3, 15, 0, tzinfo=MSK)
    assert is_in_quiet_hours(prefs, inside) is True
    assert is_in_quiet_hours(prefs, outside) is False


def test_user_busy():
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    prefs = _prefs(paused_until=future.isoformat())
    assert is_user_busy(prefs) is True
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    prefs2 = _prefs(paused_until=past.isoformat())
    assert is_user_busy(prefs2) is False


def test_evaluate_push_feed_only():
    prefs = _prefs(category_push={"loader": "feed_only"})
    ok, reason, digest = evaluate_push_delivery(prefs, "loader")
    assert ok is False
    assert reason == "feed_only"
    assert digest is False


def test_evaluate_push_quiet_queues_digest():
    prefs = _prefs(quiet_start="00:00", quiet_end="23:59")
    now = datetime(2026, 6, 3, 12, 0, tzinfo=MSK)
    ok, reason, digest = evaluate_push_delivery(prefs, "loader", now=now)
    assert ok is False
    assert reason == "quiet"
    assert digest is True


def test_evaluate_push_allowed():
    prefs = _prefs(quiet_start="23:00", quiet_end="08:00")
    now = datetime(2026, 6, 3, 12, 0, tzinfo=MSK)
    ok, reason, digest = evaluate_push_delivery(prefs, "loader", now=now)
    assert ok is True
    assert reason is None
    assert digest is False


def test_parse_quiet_hours_input():
    assert parse_quiet_hours_input("23:00-08:00") == ("23:00", "08:00")
    assert parse_quiet_hours_input("23:00 – 08:00") == ("23:00", "08:00")
    assert parse_quiet_hours_input("bad") is None


def test_pause_until_morning():
    prefs = _prefs(quiet_end="08:00")
    now = datetime(2026, 6, 3, 22, 0, tzinfo=MSK)
    until = compute_pause_until_morning(prefs, now)
    assert until.astimezone(MSK).hour == 8
    assert until.astimezone(MSK).date().day == 4


def test_default_prefs_do_not_block_quiet_hours():
    prefs = normalize_prefs({})
    late = datetime(2026, 6, 3, 23, 30, tzinfo=MSK)
    assert is_in_quiet_hours(prefs, late) is False
    ok, reason, digest = evaluate_push_delivery(prefs, "loader", now=late)
    assert ok is True
    assert reason is None
    assert digest is False
