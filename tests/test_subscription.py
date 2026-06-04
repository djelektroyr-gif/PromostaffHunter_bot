from datetime import datetime, timedelta, timezone

from db import (
    _parse_paid_until,
    add_premium_request,
    attach_premium_request_receipt,
    cancel_premium_request_awaiting,
    count_pending_premium_requests,
    get_premium_request,
    init_db,
    is_user_premium,
    reject_premium_request,
    resolve_premium_requests,
    set_user_plan,
)
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


def test_premium_request_receipt_flow(monkeypatch, tmp_path):
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
        "INSERT INTO subscribers (user_id, username, first_name, is_active) VALUES (?, ?, ?, 1)",
        (777, "testuser", "Test"),
    )

    request_id = add_premium_request(777, "testuser", "Test User", "+7999", "helper", is_renewal=False)
    req = get_premium_request(request_id)
    assert req["status"] == "awaiting_receipt"
    assert count_pending_premium_requests() == 0

    assert attach_premium_request_receipt(request_id, 777, "file_abc", "photo")
    req = get_premium_request(request_id)
    assert req["status"] == "pending"
    assert req["receipt_file_id"] == "file_abc"
    assert count_pending_premium_requests() == 1

    rejected_user = reject_premium_request(request_id)
    assert rejected_user == 777
    assert get_premium_request(request_id)["status"] == "rejected"


def test_premium_request_cancel_awaiting(monkeypatch, tmp_path):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    import db_backend
    import db as db_module

    db_backend.SQLITE_PATH = str(db_file)
    db_backend.IS_POSTGRES = False
    db_module.IS_POSTGRES = False

    init_db()
    request_id = add_premium_request(888, None, "U", None, None)
    cancel_premium_request_awaiting(888, request_id)
    assert get_premium_request(request_id)["status"] == "cancelled"


def test_resolve_premium_requests_on_approve(monkeypatch, tmp_path):
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
        "INSERT INTO subscribers (user_id, username, first_name, is_active) VALUES (?, ?, ?, 1)",
        (555, "u", "U"),
    )
    request_id = add_premium_request(555, "u", "U", None, None)
    attach_premium_request_receipt(request_id, 555, "f1", "photo")
    resolve_premium_requests(555)
    assert get_premium_request(request_id)["status"] == "approved"
