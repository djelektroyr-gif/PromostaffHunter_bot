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


def test_notfit_recent_with_comment(monkeypatch, tmp_path):
    from db import get_notfit_recent, count_notfit_feedback_total

    _init_test_db(monkeypatch, tmp_path)
    uid = 9001
    execute(
        "INSERT INTO subscribers (user_id, username, first_name, is_active) VALUES (?, ?, ?, 1)",
        (uid, "tester", "T"),
    )
    execute(
        "INSERT INTO vacancies (id, message_text, category_code, source_chat_title, message_link) "
        "VALUES (?, ?, ?, ?, ?)",
        ("vac_nf", "Нужен промоутер", "promoter", "Test chat", "https://t.me/c/1/2"),
    )
    fid = record_vacancy_notfit(
        uid, "vac_nf", "promoter", ["promoter"],
        reason_code="other", reason_text="Я мужчина",
    )
    assert fid > 0
    assert count_notfit_feedback_total() == 1
    recent = get_notfit_recent(5)
    assert recent[0]["reason_text"] == "Я мужчина"
    assert recent[0]["username"] == "tester"


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


def test_admin_gift_premium_helpers():
    from main import (
        GIFT_PRESET_DAYS,
        admin_gift_days_keyboard,
        format_gift_premium_user_message,
        premium_request_admin_keyboard,
    )

    msg = format_gift_premium_user_message(14, "2026-07-01")
    assert "14" in msg
    assert "подарок" in msg.lower()
    assert "2026-07-01" in msg
    kb = admin_gift_days_keyboard(12345, cards_page=2)
    data = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert f"adm_gd_12345_{GIFT_PRESET_DAYS[0]}_2" in data
    assert "adm_gx_12345_2" in data
    pr_kb = premium_request_admin_keyboard(99, 12345)
    pr_data = [btn.callback_data for row in pr_kb.inline_keyboard for btn in row]
    assert "pr_g_99_7" in pr_data
    assert "pr_a_99" in pr_data
