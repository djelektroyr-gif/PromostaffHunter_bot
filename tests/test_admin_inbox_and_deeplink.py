"""Тесты push админу и предвыбора категории по vac_ deeplink."""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock

import pytest

from db import add_subscriber, add_support_request, get_user_categories, init_db, save_vacancy, set_user_plan
from db_backend import IS_POSTGRES
from services.admin_inbox_alerts import (
    complaint_action_keyboard,
    format_complaint_admin_html,
    format_moderation_notify_html,
    format_moderation_queue_item_html,
    format_support_admin_html,
    notify_admin_notfit_feedback,
)
from services.inbox_ack_messages import (
    INBOX_SLA_HOURS,
    complaint_ack_text,
    support_request_ack_text,
)
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


def test_complaint_keyboard_has_reply_button():
    kb = complaint_action_keyboard(9, 456)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "cmp_r:9" in callbacks
    assert "cmp_ok:9" in callbacks


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


def test_format_moderation_notify_html_escapes_user_text():
    preview = "м. Тверская, 5000 руб. @boss_name, ставка 450 ₽/ч"
    text = format_moderation_notify_html(
        vacancy_id="vac_123",
        category_name="Промоутер",
        preview=preview,
        employer_user_id=999,
    )
    assert "<b>Модерация вакансии заказчика</b>" in text
    assert "vac_123" in text
    assert preview in text
    assert "\\." not in text
    assert "\\_" not in text


def test_format_moderation_queue_item_html_escapes_contact():
    text = format_moderation_queue_item_html(
        category_name="Хелпер",
        vacancy_id="vac_abc",
        author_contact="@some_user",
        preview="Адрес: ул. Ленина, д. 5",
    )
    assert "@some_user" in text
    assert "vac_abc" in text
    assert "Ленина" in text


def test_support_request_ack_text_includes_id_and_sla():
    text = support_request_ack_text(42)
    assert "№42" in text
    assert str(INBOX_SLA_HOURS) in text
    assert "специалисту" in text


def test_complaint_ack_text_includes_id_and_sla():
    text = complaint_ack_text(9)
    assert "№9" in text
    assert str(INBOX_SLA_HOURS) in text
    assert "рассмотрении" in text


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


def test_notify_admin_notfit_feedback_loads_vacancy(tmp_db, monkeypatch):
    monkeypatch.setattr("services.admin_inbox_alerts.YOUR_USER_ID", 777)
    add_subscriber(55, "tester", "Tester", None)
    save_vacancy(
        "nf_v1",
        "c1",
        "Test Chat",
        "helper",
        "Нужен хелпер на завтра",
        "https://t.me/x/1",
        "@boss",
        None,
        False,
        "dk_nf",
        "2026-06-26 12:00:00",
    )
    bot = AsyncMock()
    asyncio.run(
        notify_admin_notfit_feedback(
            bot,
            feedback_id=3,
            user_id=55,
            vacancy_id="nf_v1",
            reason_code="pay",
            reason_label="Оплата",
            reason_text="мало",
            username="tester",
        )
    )
    bot.send_message.assert_awaited_once()
    text = bot.send_message.call_args.args[1]
    assert "nf_v1" in text
    assert "helper" in text
    assert "Нужен хелпер" in text
