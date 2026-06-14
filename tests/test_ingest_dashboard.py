"""Ingest dashboard report."""

import json
import sqlite3

import db_backend
from db import init_db, log_bot_event, save_vacancy
from services.ingest_dashboard import build_ingest_dashboard_report


def _setup_db(monkeypatch, tmp_path):
    db_file = tmp_path / "ingest_dash.db"

    def _connect():
        return sqlite3.connect(str(db_file), timeout=10.0)

    monkeypatch.setattr(db_backend, "connect", _connect)
    monkeypatch.setattr(db_backend, "IS_POSTGRES", False)
    init_db()


def test_ingest_dashboard_shows_saved_and_rejected(monkeypatch, tmp_path):
    _setup_db(monkeypatch, tmp_path)
    save_vacancy(
        "v1", "c1", "Chat Alpha", "promoter", "promo job", "http://x",
        published_at="2026-06-10 12:00:00",
    )
    log_bot_event(None, "parser_rejected", {
        "reason": "permanent_job",
        "chat_title": "Chat Beta",
    })
    text = build_ingest_dashboard_report(days=7)
    assert "Chat Alpha" in text
    assert "promoter" in text.lower() or "Промоутер" in text
    assert "permanent_job" in text or "постоянная" in text
    assert "Сохранено в БД" in text
