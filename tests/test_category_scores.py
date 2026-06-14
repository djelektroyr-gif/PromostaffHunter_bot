"""Tests for category scores and vacancy category matching."""

import json
import sqlite3

import db_backend
from db import get_feed_vacancies_for_user, init_db, save_vacancy, set_user_categories
from services.category_scores import compute_category_scores, parse_category_scores_json, scores_to_json
from services.vacancy_category_match import (
    SECONDARY_CATEGORY_MIN_SCORE,
    vacancy_category_codes,
    vacancy_matching_user_categories,
)


def _setup_db(monkeypatch, tmp_path):
    db_file = tmp_path / "category_scores.db"

    def _connect():
        return sqlite3.connect(str(db_file), timeout=10.0)

    monkeypatch.setattr(db_backend, "connect", _connect)
    monkeypatch.setattr(db_backend, "IS_POSTGRES", False)
    init_db()
    return db_file


def test_compute_category_scores_electrician():
    text = "Требуются электромонтажники на монтаж, оплата 5000₽/смена @boss"
    scores = compute_category_scores(text)
    assert "electrician" in scores
    assert scores["electrician"] >= SECONDARY_CATEGORY_MIN_SCORE


def test_scores_to_json_roundtrip():
    raw = scores_to_json({"helper": 8, "loader": 5})
    parsed = parse_category_scores_json(raw)
    assert parsed == {"helper": 8, "loader": 5}


def test_vacancy_matching_secondary_category():
    vacancy = {
        "category_code": "helper",
        "category_scores_json": json.dumps({"electrician": 12, "helper": 3}),
    }
    matched = vacancy_matching_user_categories(vacancy, ["electrician", "promoter"])
    assert matched == ["electrician"]


def test_vacancy_category_codes_includes_primary_and_secondary():
    vacancy = {
        "category_code": "helper",
        "category_scores_json": json.dumps({"electrician": 12, "loader": 2}),
    }
    codes = vacancy_category_codes(vacancy)
    assert codes == {"helper", "electrician"}


def test_feed_includes_secondary_category_match(monkeypatch, tmp_path):
    _setup_db(monkeypatch, tmp_path)
    uid = 900001
    set_user_categories(uid, ["electrician"])
    scores = scores_to_json({"electrician": 10, "helper": 14})
    save_vacancy(
        "vac_sec_1", "chat1", "Chat", "helper", "электромонтаж на объекте", "http://x",
        category_scores_json=scores,
    )
    rows = get_feed_vacancies_for_user(uid, "electrician", max_hours=168)
    assert len(rows) == 1
    assert rows[0]["id"] == "vac_sec_1"
