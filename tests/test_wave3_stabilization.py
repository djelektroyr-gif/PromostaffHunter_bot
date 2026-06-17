"""Волна 3: idempotent stars/push/channel, feed session persist, регрессии P1/R3."""

import os
import tempfile

import pytest

from db import (
    add_premium_request,
    add_response,
    add_subscriber,
    approve_premium_request,
    attach_premium_request_receipt,
    complete_star_purchase,
    create_star_purchase,
    get_last_processed_id,
    get_premium_request,
    init_db,
    is_vacancy_channel_posted,
    get_vacancy_row,
    unpack_vacancy_row_basic,
    load_user_feed_session,
    mark_vacancy_channel_posted,
    release_vacancy_channel_post,
    save_user_feed_session,
    save_vacancy,
    set_vacancy_moderation_if_pending,
    try_reserve_promo_slot,
    try_reserve_vacancy_channel_post,
    delete_vacancy_completely,
    count_published_channel_vacancy_posts,
    try_reserve_vacancy_sent_to_user,
    unreserve_vacancy_sent_to_user,
    update_last_processed_id,
)
from db_backend import IS_POSTGRES, fetchone
from services.fsm_storage import create_fsm_storage


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


# --- P1: monotonic parser cursor (wave 1 regression) ---


def test_p1_update_last_processed_id_monotonic(tmp_db):
    update_last_processed_id("chat1", 105)
    assert get_last_processed_id("chat1") == 105
    update_last_processed_id("chat1", 103)
    assert get_last_processed_id("chat1") == 105
    update_last_processed_id("chat1", 110)
    assert get_last_processed_id("chat1") == 110


# --- R3: UNIQUE responses (wave 1 regression) ---


def test_r3_add_response_unique(tmp_db):
    add_subscriber(1, "u", "A", None)
    assert add_response(1, "vac_a", "text", draft_status="pending") is True
    assert add_response(1, "vac_a", "text2", draft_status="pending") is False


# --- Premium approve idempotent (wave 2 regression) ---


def test_premium_approve_idempotent(tmp_db):
    add_subscriber(101, "u", "U", None)
    rid = add_premium_request(101, "u", "U", None, None)
    attach_premium_request_receipt(rid, 101, "f1", "photo")
    assert approve_premium_request(rid) is not None
    assert get_premium_request(rid)["status"] == "approved"
    assert approve_premium_request(rid) is None


# --- Moderation if pending (wave 2 regression) ---


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


# --- Wave 3: Stars ---


def test_complete_star_purchase_idempotent(tmp_db):
    add_subscriber(201, "u", "U", None)
    create_star_purchase(201, "vac_s1", 50, "payload-abc")
    first = complete_star_purchase("payload-abc")
    assert first == {"user_id": 201, "vacancy_id": "vac_s1", "stars_amount": 50}
    assert complete_star_purchase("payload-abc") is None


# --- Wave 3: push reserve ---


def test_try_reserve_vacancy_sent_to_user(tmp_db):
    add_subscriber(301, "u", "U", None)
    assert try_reserve_vacancy_sent_to_user("vac_p1", 301) is True
    assert try_reserve_vacancy_sent_to_user("vac_p1", 301) is False
    unreserve_vacancy_sent_to_user("vac_p1", 301)
    assert try_reserve_vacancy_sent_to_user("vac_p1", 301) is True


# --- Wave 3: channel reserve ---


def test_try_reserve_vacancy_channel_post(tmp_db):
    assert try_reserve_vacancy_channel_post("vac_ch1", "loader") is True
    assert try_reserve_vacancy_channel_post("vac_ch1", "loader") is False
    assert is_vacancy_channel_posted("vac_ch1") is False
    mark_vacancy_channel_posted("vac_ch1", category_code="loader", message_id=42)
    assert is_vacancy_channel_posted("vac_ch1") is True
    assert try_reserve_vacancy_channel_post("vac_ch1", "loader") is False


def test_release_vacancy_channel_post_on_failure(tmp_db):
    assert try_reserve_vacancy_channel_post("vac_ch2", "helper") is True
    release_vacancy_channel_post("vac_ch2")
    assert try_reserve_vacancy_channel_post("vac_ch2", "helper") is True


def test_stale_channel_reserve_reclaimed(tmp_db):
    from datetime import datetime, timedelta, timezone

    from db import execute, q

    assert try_reserve_vacancy_channel_post("vac_stale", "loader") is True
    assert try_reserve_vacancy_channel_post("vac_stale", "loader") is False
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    execute(
        q("UPDATE vacancy_channel_posts SET posted_at = ? WHERE vacancy_id = ?"),
        (old, "vac_stale"),
    )
    assert try_reserve_vacancy_channel_post("vac_stale", "loader") is True


def test_count_published_channel_vacancy_posts(tmp_db):
    assert count_published_channel_vacancy_posts("loader") == 0
    mark_vacancy_channel_posted("v1", category_code="loader", message_id=1)
    mark_vacancy_channel_posted("v2", category_code="loader", message_id=2)
    mark_vacancy_channel_posted("v3", category_code="helper", message_id=3)
    assert count_published_channel_vacancy_posts("loader") == 2
    assert count_published_channel_vacancy_posts("helper") == 1


def test_try_reserve_promo_slot(tmp_db):
    assert try_reserve_promo_slot("09:00", "2026-06-07") is True
    assert try_reserve_promo_slot("09:00", "2026-06-07") is False


# --- Wave 3: feed session persist ---


def test_user_feed_session_roundtrip(tmp_db):
    add_subscriber(401, "u", "U", None)
    save_user_feed_session(401, "fresh", ["loader", "helper"], ["v1", "v2", "v3"], page=2)
    session = load_user_feed_session(401)
    assert session["feed_mode"] == "fresh"
    assert session["feed_filter"] == ["loader", "helper"]
    assert session["vacancy_ids"] == ["v1", "v2", "v3"]
    assert session["page"] == 2


def test_delete_vacancy_completely(tmp_db):
    add_subscriber(501, "u", "U", None)
    add_subscriber(502, "u2", "U2", None)
    save_vacancy(
        "spam_v1",
        "c1",
        "Chat",
        "helper",
        "Реклама курсовых",
        "https://t.me/x/1",
        "@spam",
        None,
        False,
        "dk_spam",
        "2026-06-07 12:00:00",
    )
    assert try_reserve_vacancy_sent_to_user("spam_v1", 501) is True
    assert try_reserve_vacancy_sent_to_user("spam_v1", 502) is True
    add_response(501, "spam_v1", "отклик", draft_status="pending")
    mark_vacancy_channel_posted("spam_v1", category_code="helper", message_id=999)
    save_user_feed_session(501, "fresh", ["helper"], ["spam_v1", "other_v"], page=0)

    stats = delete_vacancy_completely("spam_v1")
    assert stats is not None
    assert stats["push_recipients"] == 2
    assert stats["deleted_vacancy"] == 1
    assert stats["deleted_sent_vacancies"] == 2
    assert stats["deleted_responses"] == 1
    assert stats["channel_message_id"] == 999
    assert stats["feed_sessions_updated"] == 1

    assert get_vacancy_row("spam_v1") is None
    assert is_vacancy_channel_posted("spam_v1") is False
    session = load_user_feed_session(501)
    assert session["vacancy_ids"] == ["other_v"]
    assert delete_vacancy_completely("spam_v1") is None


def test_unpack_vacancy_row_basic_ignores_geo_columns():
    row = ("text", "link", "chat", "@boss", "addr", "norm addr", 55.7, 37.6)
    assert unpack_vacancy_row_basic(row) == ("text", "link", "chat", "@boss", "addr")
    assert unpack_vacancy_row_basic(None) is None


# --- FSM storage fallback ---


def test_create_fsm_storage_memory_without_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    storage = create_fsm_storage()
    from aiogram.fsm.storage.memory import MemoryStorage

    assert isinstance(storage, MemoryStorage)
