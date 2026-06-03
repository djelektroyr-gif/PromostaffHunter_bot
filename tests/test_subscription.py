from datetime import datetime, timedelta, timezone

from db import _parse_paid_until, set_user_plan, init_db, is_user_premium
from db_backend import fetchone, execute


def test_parse_paid_until_datetime():
    dt = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    assert _parse_paid_until(dt) == dt


def test_parse_paid_until_string():
    parsed = _parse_paid_until("2026-06-10 12:00:00")
    assert parsed.year == 2026 and parsed.month == 6 and parsed.day == 10


def test_set_user_plan_extend_adds_to_existing(monkeypatch, tmp_path):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    import db_backend
    import db as db_module

    db_backend.SQLITE_PATH = str(db_file)
    db_backend.IS_POSTGRES = False
    db_module.IS_POSTGRES = False

    init_db()
    execute(
        "INSERT INTO subscribers (user_id, username, first_name, is_active, plan, paid_until) "
        "VALUES (?, ?, ?, 1, 'premium', ?)",
        (999001, "u", "Test", "2026-12-01 10:00:00"),
    )

    set_user_plan(999001, plan="premium", days=30, extend=True)
    row = fetchone("SELECT paid_until FROM subscribers WHERE user_id = ?", (999001,))
    extended = _parse_paid_until(row[0])
    assert extended >= datetime(2026, 12, 31, 0, 0, 0, tzinfo=timezone.utc)
    assert is_user_premium(999001)
