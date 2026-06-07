"""Волна 2: атомарные approve/reject, idempotent модерация."""

import pytest

from db import (
    add_premium_request,
    add_subscriber,
    approve_premium_request,
    attach_premium_request_receipt,
    get_premium_request,
    init_db,
    reject_premium_request,
    save_vacancy,
    set_vacancy_moderation_if_pending,
)
from db_backend import fetchone


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "wave2.db"
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    import db as db_module
    import db_backend

    db_backend.SQLITE_PATH = str(db_file)
    db_backend.IS_POSTGRES = False
    db_module.IS_POSTGRES = False
    init_db()
    yield db_file


def test_approve_premium_request_idempotent(tmp_db):
    add_subscriber(101, "u", "U", None)
    rid = add_premium_request(101, "u", "U", None, None)
    attach_premium_request_receipt(rid, 101, "f1", "photo")
    first = approve_premium_request(rid)
    assert first is not None
    assert first["user_id"] == 101
    assert get_premium_request(rid)["status"] == "approved"
    assert approve_premium_request(rid) is None


def test_reject_premium_request_idempotent(tmp_db):
    add_subscriber(102, "u", "U", None)
    rid = add_premium_request(102, "u", "U", None, None)
    attach_premium_request_receipt(rid, 102, "f1", "photo")
    assert reject_premium_request(rid) == 102
    assert reject_premium_request(rid) is None


def test_moderation_if_pending(tmp_db):
    save_vacancy(
        "emp_v1",
        "c1",
        "Chat",
        "helper",
        "Нужен хелпер",
        "https://t.me/x/1",
        "@boss",
        None,
        False,
        "dk1",
        "2026-06-07 12:00:00",
        moderation_status="pending",
    )
    assert set_vacancy_moderation_if_pending("emp_v1", "approved") is True
    assert set_vacancy_moderation_if_pending("emp_v1", "rejected") is False
    status = fetchone("SELECT moderation_status FROM vacancies WHERE id = ?", ("emp_v1",))[0]
    assert status == "approved"
