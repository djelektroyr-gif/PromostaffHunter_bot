"""Тесты push админу и предвыбора категории по vac_ deeplink."""

from __future__ import annotations

import os
import tempfile

import pytest

from db import add_subscriber, add_support_request, get_user_categories, init_db, set_user_plan
from db_backend import IS_POSTGRES
from services.admin_inbox_alerts import format_complaint_admin_html, format_support_admin_html
from services.onboarding_deeplink import apply_vacancy_deeplink_category_preselect


@pytest.fixture
def tmp_db(monkeypatch):
    if IS_POSTGRES:
        pytest.skip("SQLite-only fixture")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setattr("db_backend.DATABASE_URL", "")
    monkeypatch.setattr("db_backend.IS_POSTGRES", False)
    monkeypatch.setattr("db_backend.SQLITE_PATH", path)
    init_db()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def test_add_support_request_returns_id(tmp_db):
    add_subscriber(1, "u", "U", None)
    rid = add_support_request(1, "help me", "u")
    assert isinstance(rid, int) and rid > 0


def test_format_support_admin_html():
    text = format_support_admin_html(
        request_id=7,
        user_id=123,
        username="tester",
        message_text="Не приходит push",
        pending_count=3,
    )
    assert "#7" in text
    assert "123" in text
    assert "push" in text


def test_format_complaint_admin_html():
    text = format_complaint_admin_html(
        complaint_id=9,
        user_id=456,
        full_name="Иван",
        username="ivan",
        vacancy_id="vac_x",
        reason="Мошенничество",
        complaint_text="Подозрительно",
    )
    assert "#9" in text
    assert "vac_x" in text


def test_vac_deeplink_preselects_category(tmp_db):
    uid = 100
    add_subscriber(uid, "u", "U", None)
    hint = apply_vacancy_deeplink_category_preselect(uid, "helper", free_limit=1)
    cats = get_user_categories(uid)
    assert len(cats) == 1
    assert cats[0]["code"] == "helper"
    assert "канала" in hint


def test_vac_deeplink_skips_if_categories_exist(tmp_db):
    uid = 101
    add_subscriber(uid, "u", "U", None)
    set_user_plan(uid, plan="premium", days=30, extend=True)
    apply_vacancy_deeplink_category_preselect(uid, "loader", free_limit=1)
    hint = apply_vacancy_deeplink_category_preselect(uid, "helper", free_limit=1)
    assert hint == ""
    assert any(c["code"] == "loader" for c in get_user_categories(uid))


def test_unknown_category_no_preselect(tmp_db):
    uid = 102
    add_subscriber(uid, "u", "U", None)
    assert apply_vacancy_deeplink_category_preselect(uid, "unknown_role", free_limit=1) == ""
