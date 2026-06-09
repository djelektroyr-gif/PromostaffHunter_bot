"""Тесты заявок Premium на добавление канала в мониторинг."""

from __future__ import annotations

import os
import tempfile

import pytest

from db import (
    activate_target_chat,
    create_chat_suggestion,
    get_pending_chat_suggestion_for_link,
    resolve_chat_suggestion,
    target_chat_is_active,
    init_db,
)
from db_backend import IS_POSTGRES
from services.chat_suggest_flow import normalize_chat_link, username_from_chat_link


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


def test_normalize_chat_link_variants():
    assert normalize_chat_link("@mychannel") == "https://t.me/mychannel"
    assert normalize_chat_link("https://t.me/foo/") == "https://t.me/foo"
    assert normalize_chat_link("mychannel") == "https://t.me/mychannel"
    assert normalize_chat_link("not valid!!!") is None


def test_username_from_chat_link():
    assert username_from_chat_link("https://t.me/jobs_msk") == "@jobs_msk"
    assert username_from_chat_link("https://t.me/+AbCdEf") is None


def test_chat_suggestion_flow(tmp_db):
    assert create_chat_suggestion(101, "https://t.me/newjobs", user_username="u1") == 1
    pending = get_pending_chat_suggestion_for_link("https://t.me/newjobs")
    assert pending is not None
    assert pending["user_id"] == 101
    assert pending["status"] == "pending"

    assert not target_chat_is_active("https://t.me/newjobs")
    assert activate_target_chat("https://t.me/newjobs") is True
    assert target_chat_is_active("https://t.me/newjobs")

    assert resolve_chat_suggestion(pending["id"], "approved") is True
    pending2 = get_pending_chat_suggestion_for_link("https://t.me/newjobs")
    assert pending2 is None


def test_resolve_rejected(tmp_db):
    sid = create_chat_suggestion(301, "https://t.me/rejectme", user_username="x")
    assert resolve_chat_suggestion(sid, "rejected", admin_note="нет доступа") is True
    from db import get_chat_suggestion
    row = get_chat_suggestion(sid)
    assert row["status"] == "rejected"
    assert row["admin_note"] == "нет доступа"
