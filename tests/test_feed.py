import sqlite3

import db_backend
from db import (
    add_subscriber,
    get_feed_vacancies_for_user,
    init_db,
    mark_vacancy_sent_to_user,
    save_vacancy,
    set_user_categories,
)


def test_feed_vacancies_exclude_pushed_to_user(monkeypatch, tmp_path):
    db_file = tmp_path / "feed.db"

    def _connect():
        return sqlite3.connect(str(db_file), timeout=10.0)

    monkeypatch.setattr(db_backend, "connect", _connect)
    monkeypatch.setattr(db_backend, "IS_POSTGRES", False)

    init_db()
    uid = 700001
    add_subscriber(uid, "tester", "Test", "User")
    set_user_categories(uid, ["helper"])
    save_vacancy(
        "vac001", "chat1", "Chat", "helper", "Need helper", "https://t.me/c/1/1",
        "@boss", "metro", False, "dedupe1", "2026-06-03 10:00:00",
    )
    save_vacancy(
        "vac002", "chat2", "Chat2", "helper", "Another job", "https://t.me/c/2/2",
        "@boss2", None, False, "dedupe2", "2026-06-03 11:00:00",
    )

    all_feed = get_feed_vacancies_for_user(uid, "helper")
    assert len(all_feed) == 2

    mark_vacancy_sent_to_user("vac001", uid)
    remaining = get_feed_vacancies_for_user(uid, "helper")
    assert len(remaining) == 1
    assert remaining[0]["id"] == "vac002"
