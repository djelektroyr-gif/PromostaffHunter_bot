"""Тесты bot_events, дайджеста и stuck-регистраций."""

from datetime import datetime, timedelta, timezone

import pytest

from db import (
    add_subscriber,
    get_activity_digest_data,
    get_scheduler_flag,
    get_stuck_registrations,
    init_db,
    mark_reg_stuck_notified,
    set_scheduler_flag,
)
from services.admin_activity_digest import build_activity_digest_html
from services.admin_digest_scheduler import _digest_flag_key, admin_digest_due_now
from services.bot_events import EVENT_START, record_bot_event


@pytest.fixture(autouse=True)
def _db():
    init_db()


def test_log_and_count_events():
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    record_bot_event(9001, EVENT_START)
    record_bot_event(9001, EVENT_START)
    data = get_activity_digest_data(since_utc=since)
    assert data["events"].get("start", 0) >= 2
    assert data["active_users_seen"] >= 1


def test_build_activity_digest_html_contains_sections():
    text = build_activity_digest_html(hours=24)
    assert "Активность" in text
    assert "Парсер" in text


def test_stuck_registration_incomplete_profile():
    from db import execute, q

    uid = 99101
    execute(q("DELETE FROM subscribers WHERE user_id = ?"), (uid,))
    add_subscriber(uid, "stuck", "Stuck", None)
    execute(
        q("UPDATE subscribers SET registered_at = datetime('now', '-2 days'), reg_stuck_notified_at = NULL WHERE user_id = ?"),
        (uid,),
    )
    rows = [r for r in get_stuck_registrations(older_than_hours=24) if r["user_id"] == uid]
    assert len(rows) == 1
    assert rows[0]["reason"] == "не завершил анкету"
    mark_reg_stuck_notified(uid)
    rows2 = [r for r in get_stuck_registrations(older_than_hours=24) if r["user_id"] == uid]
    assert not rows2


def test_scheduler_flag_and_digest_due():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    fake = datetime(2099, 6, 9, 10, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    set_scheduler_flag(_digest_flag_key("2099-06-09"), "")
    assert admin_digest_due_now(fake) is True
    set_scheduler_flag(_digest_flag_key("2099-06-09"), "2099-06-09")
    assert admin_digest_due_now(fake) is False
