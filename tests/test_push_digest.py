"""Tests for push digest queue and message."""

import sqlite3

import db_backend
from db import (
    add_push_digest_pending,
    clear_push_digest_pending,
    count_push_digest_pending,
    init_db,
)
from services.push_digest_scheduler import format_push_digest_message


def test_digest_message_plural():
    assert "*1*" in format_push_digest_message(1)
    text5 = format_push_digest_message(5)
    assert "5" in text5


def test_digest_pending_db(monkeypatch, tmp_path):
    db_file = tmp_path / "digest.db"

    def _connect():
        return sqlite3.connect(str(db_file), timeout=10.0)

    monkeypatch.setattr(db_backend, "connect", _connect)
    monkeypatch.setattr(db_backend, "IS_POSTGRES", False)
    init_db()
    uid = 900001
    assert add_push_digest_pending(uid, "vac1") is True
    assert add_push_digest_pending(uid, "vac1") is False
    assert add_push_digest_pending(uid, "vac2") is True
    assert count_push_digest_pending(uid) == 2
    assert clear_push_digest_pending(uid) == 2
    assert count_push_digest_pending(uid) == 0
