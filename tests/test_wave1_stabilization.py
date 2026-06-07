"""Волна 1 стабилизации: cursor парсера, idempotent отклики."""

import os
import tempfile

import pytest

from db import add_response, init_db, update_last_processed_id, get_last_processed_id
from db_backend import IS_POSTGRES


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
    monkeypatch.setattr("config.get_database_path", lambda: path)
    init_db()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def test_update_last_processed_id_monotonic(tmp_db):
    update_last_processed_id("chat1", 105)
    assert get_last_processed_id("chat1") == 105
    update_last_processed_id("chat1", 103)
    assert get_last_processed_id("chat1") == 105
    update_last_processed_id("chat1", 110)
    assert get_last_processed_id("chat1") == 110


def test_add_response_unique(tmp_db):
    from db import add_subscriber

    add_subscriber(1, "u", "A", None)
    assert add_response(1, "vac_a", "text", draft_status="pending") is True
    assert add_response(1, "vac_a", "text2", draft_status="pending") is False
    assert add_response(1, "vac_b", "text", draft_status="pending") is True


def test_update_response_delivery(tmp_db):
    from db import add_subscriber, update_response_delivery, get_response_record

    add_subscriber(2, "u2", "B", None)
    add_response(2, "vac_x", "t", draft_status="pending")
    update_response_delivery(
        2, "vac_x", draft_status="delivered", employer_contact="@boss",
    )
    rec = get_response_record(2, "vac_x")
    assert rec["draft_status"] == "delivered"
    assert rec["employer_contact"] == "@boss"
