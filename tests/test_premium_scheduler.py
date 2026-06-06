import sqlite3
from datetime import datetime, timedelta, timezone

from db import (
    downgrade_expired_premium,
    grant_trial_if_eligible,
    init_db,
    is_user_premium,
    list_expired_premium_user_ids,
    list_premium_renewal_reminder_candidates,
    mark_premium_renewal_warned,
)
from db_backend import execute, fetchone
from services.premium_scheduler import format_premium_renewal_reminder


def _setup_db(monkeypatch, tmp_path):
    db_file = tmp_path / "premium_sched.db"
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    import db_backend
    import db as db_module

    sqlite3.connect(str(db_file)).close()
    db_backend.SQLITE_PATH = str(db_file)
    db_backend.IS_POSTGRES = False
    db_module.IS_POSTGRES = False
    monkeypatch.setattr(db_module, "_migrate_legacy_database_if_needed", lambda: None)
    init_db()
    return db_file


def test_renewal_reminder_candidates_within_three_days(monkeypatch, tmp_path):
    _setup_db(monkeypatch, tmp_path)
    user_id = 810001
    until = datetime.now(timezone.utc) + timedelta(days=2, hours=5)
    execute(
        "INSERT INTO subscribers (user_id, username, first_name, is_active, plan, paid_until, trial_used) "
        "VALUES (?, ?, ?, 1, 'premium', ?, 1)",
        (user_id, "u", "Test", until.isoformat()),
    )
    rows = list_premium_renewal_reminder_candidates(3)
    assert len(rows) == 1
    assert rows[0]["user_id"] == user_id
    assert rows[0]["days_left"] in (1, 2)


def test_renewal_reminder_not_sent_twice_for_same_period(monkeypatch, tmp_path):
    _setup_db(monkeypatch, tmp_path)
    user_id = 810002
    until = datetime.now(timezone.utc) + timedelta(days=1)
    execute(
        "INSERT INTO subscribers (user_id, username, first_name, is_active, plan, paid_until, trial_used) "
        "VALUES (?, ?, ?, 1, 'premium', ?, 1)",
        (user_id, "u", "Test", until.isoformat()),
    )
    assert len(list_premium_renewal_reminder_candidates(3)) == 1
    mark_premium_renewal_warned(user_id, until.isoformat())
    assert list_premium_renewal_reminder_candidates(3) == []


def test_expired_premium_list_and_downgrade(monkeypatch, tmp_path):
    _setup_db(monkeypatch, tmp_path)
    user_id = 810003
    until = datetime.now(timezone.utc) - timedelta(hours=1)
    execute(
        "INSERT INTO subscribers (user_id, username, first_name, is_active, plan, paid_until) "
        "VALUES (?, ?, ?, 1, 'premium', ?)",
        (user_id, "u", "Test", until.isoformat()),
    )
    assert user_id in list_expired_premium_user_ids()
    assert not is_user_premium(user_id)
    msg = downgrade_expired_premium(user_id)
    assert msg is not None
    row = fetchone("SELECT plan FROM subscribers WHERE user_id = ?", (user_id,))
    assert row[0] == "free"
    assert downgrade_expired_premium(user_id) is None


def test_format_reminder_mentions_trial(monkeypatch):
    monkeypatch.setattr("services.premium_scheduler.SUBSCRIPTION_PRICE_RUB", "299")
    text = format_premium_renewal_reminder(
        days_left=3,
        trial_used=True,
        paid_until=datetime.now(timezone.utc) + timedelta(days=3),
    )
    assert "Пробный Premium" in text
    assert "299" in text


def test_trial_within_reminder_window(monkeypatch, tmp_path):
    _setup_db(monkeypatch, tmp_path)
    user_id = 810004
    execute(
        "INSERT INTO subscribers (user_id, username, first_name, is_active, plan) "
        "VALUES (?, ?, ?, 1, 'free')",
        (user_id, "u", "Test"),
    )
    assert grant_trial_if_eligible(user_id, 7)
    execute(
        "UPDATE subscribers SET paid_until = ? WHERE user_id = ?",
        ((datetime.now(timezone.utc) + timedelta(days=2)).isoformat(), user_id),
    )
    assert len(list_premium_renewal_reminder_candidates(3)) == 1
