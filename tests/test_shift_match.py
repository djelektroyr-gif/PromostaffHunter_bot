"""Tests for services/shift_match.py."""

from datetime import datetime
from zoneinfo import ZoneInfo

from services.shift_match import (
    is_night_start,
    shift_date_matches_today_tomorrow,
    time_start_before,
)

MSK = ZoneInfo("Europe/Moscow")


def test_night_start():
    assert is_night_start("23:00") is True
    assert is_night_start("05:30") is True
    assert is_night_start("09:00") is False
    assert is_night_start(None) is False


def test_shift_date_today_tomorrow():
    today = datetime(2026, 6, 3, 12, 0, tzinfo=MSK)
    assert shift_date_matches_today_tomorrow("today", today) is True
    assert shift_date_matches_today_tomorrow("tomorrow", today) is True
    assert shift_date_matches_today_tomorrow("2026-06-03", today) is True
    assert shift_date_matches_today_tomorrow("2026-06-04", today) is True
    assert shift_date_matches_today_tomorrow("2026-06-10", today) is False
    assert shift_date_matches_today_tomorrow(None, today) is True


def test_time_start_before():
    assert time_start_before("07:00", "09:00") is True
    assert time_start_before("10:00", "09:00") is False
