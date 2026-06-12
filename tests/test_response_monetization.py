"""Платные отклики: trial при первом, кредиты, Stars."""
import os
import tempfile

import pytest

from db import (
    IS_POSTGRES,
    add_response,
    add_response_credits,
    add_subscriber,
    consume_response_credit,
    create_star_purchase,
    complete_star_purchase,
    get_response_credits,
    has_paid_response_unlock,
    has_star_purchase_for_vacancy,
    init_db,
    is_user_premium,
    set_user_plan,
    update_subscriber_profile,
)
from db_backend import execute
from services.response_monetization import (
    consume_response_slot,
    resolve_response_access,
    setup_trial_from_first_response,
)


@pytest.fixture
def tmp_db(monkeypatch):
    if IS_POSTGRES:
        pytest.skip("SQLite-only fixture")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setattr("db_backend.DATABASE_URL", "")
    monkeypatch.setattr("db_backend.IS_POSTGRES", False)
    monkeypatch.setattr("db_backend.SQLITE_PATH", path)
    monkeypatch.setattr("config.get_database_path", lambda: path)
    init_db()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def _seed_user(user_id: int = 700001):
    add_subscriber(user_id, "tester", "Test", None)
    update_subscriber_profile(user_id, full_name="Иван Тест", phone="+79001234567", age=25)
    return user_id


def _seed_vacancy(vacancy_id: str = "vac_test_1", category: str = "helper"):
    execute(
        """
        INSERT INTO vacancies (id, source_chat, source_chat_title, category_code, message_text)
        VALUES (?, '-1001', 'Test chat', ?, 'Нужен хелпер на завтра')
        """,
        (vacancy_id, category),
    )


def test_first_response_gets_trial_access(tmp_db):
    user_id = _seed_user()
    _seed_vacancy()
    access = resolve_response_access(user_id, "vac_test_1")
    assert access.allowed is True
    assert access.apply_first_trial is True
    assert access.needs_paywall is False


def test_after_trial_expired_needs_paywall(tmp_db):
    user_id = _seed_user()
    _seed_vacancy()
    add_response(user_id, "vac_old", "snippet", draft_status="pending")
    access = resolve_response_access(user_id, "vac_test_1")
    assert access.allowed is False
    assert access.needs_paywall is True


def test_premium_skips_paywall(tmp_db):
    user_id = _seed_user()
    set_user_plan(user_id, "premium", 30)
    access = resolve_response_access(user_id, "vac_test_1")
    assert access.allowed is True
    assert access.needs_paywall is False


def test_response_credits_allow_access(tmp_db):
    user_id = _seed_user()
    add_response(user_id, "vac_old", "snippet", draft_status="pending")
    add_response_credits(user_id, 3)
    access = resolve_response_access(user_id, "vac_test_1")
    assert access.allowed is True
    assert access.reason == "credit"


def test_consume_credit_decrements_balance(tmp_db):
    user_id = _seed_user()
    add_response(user_id, "vac_old", "snippet", draft_status="pending")
    add_response_credits(user_id, 2)
    ok, _ = consume_response_slot(user_id, "vac_test_1")
    assert ok is True
    assert get_response_credits(user_id) == 1
    assert consume_response_credit(user_id) is True
    assert get_response_credits(user_id) == 0


def test_star_unlock_per_vacancy(tmp_db):
    user_id = _seed_user()
    vac = "vac_star_1"
    create_star_purchase(user_id, vac, 3, f"resp_pay:{vac}")
    assert not has_paid_response_unlock(user_id, vac)
    complete_star_purchase(f"resp_pay:{vac}")
    assert has_paid_response_unlock(user_id, vac)
    access = resolve_response_access(user_id, vac)
    assert access.reason == "star_paid"


def test_extended_star_separate_from_response_pay(tmp_db):
    user_id = _seed_user()
    vac = "vac_ext_1"
    create_star_purchase(user_id, vac, 35, f"ext_resp:{vac}")
    complete_star_purchase(f"ext_resp:{vac}")
    assert has_star_purchase_for_vacancy(user_id, vac)
    assert not has_paid_response_unlock(user_id, vac)


def test_setup_trial_adds_category(tmp_db):
    user_id = _seed_user()
    _seed_vacancy(category="loader")
    info = setup_trial_from_first_response(user_id, "vac_test_1")
    assert info["trial_granted"] is True
    assert info["category_code"] == "loader"
    assert is_user_premium(user_id)
