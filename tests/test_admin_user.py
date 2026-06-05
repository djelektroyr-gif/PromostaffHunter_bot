from db import (
    count_user_notfit_feedback,
    count_user_responses,
    count_user_sent_vacancies,
    get_subscriber_registered_at,
    init_db,
    record_vacancy_notfit,
    set_subscriber_active,
)
from db_backend import execute, fetchone


def _init_test_db(monkeypatch, tmp_path):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    import db_backend
    import db as db_module

    db_backend.SQLITE_PATH = str(db_file)
    db_backend.IS_POSTGRES = False
    db_module.IS_POSTGRES = False
    init_db()
    return db_file


def test_subscriber_activity_counts(monkeypatch, tmp_path):
    _init_test_db(monkeypatch, tmp_path)
    uid = 227713003
    execute(
        "INSERT INTO subscribers (user_id, username, first_name, is_active, plan) VALUES (?, ?, ?, 1, 'free')",
        (uid, "testuser", "Test"),
    )
    execute(
        "INSERT INTO vacancies (id, message_text, category_code, is_sent, is_closed) VALUES (?, ?, ?, 0, 0)",
        ("vac1", "text", "promoter"),
    )
    execute("INSERT INTO sent_vacancies (user_id, vacancy_id) VALUES (?, ?)", (uid, "vac1"))
    execute(
        "INSERT INTO responses (user_id, vacancy_id, status) VALUES (?, ?, 'pending')",
        (uid, "vac1"),
    )
    record_vacancy_notfit(uid, "vac2", "helper", ["promoter"], reason_code="pay")

    assert count_user_sent_vacancies(uid) == 1
    assert count_user_responses(uid) == 1
    assert count_user_notfit_feedback(uid) == 1
    assert get_subscriber_registered_at(uid) is not None


def test_set_subscriber_active(monkeypatch, tmp_path):
    _init_test_db(monkeypatch, tmp_path)
    uid = 100500
    execute(
        "INSERT INTO subscribers (user_id, username, first_name, is_active) VALUES (?, ?, ?, 1)",
        (uid, "u", "Name"),
    )
    set_subscriber_active(uid, False)
    row = fetchone("SELECT is_active FROM subscribers WHERE user_id = ?", (uid,))
    assert row[0] in (0, False)
    set_subscriber_active(uid, True)
    row = fetchone("SELECT is_active FROM subscribers WHERE user_id = ?", (uid,))
    assert row[0] in (1, True)
