"""Тесты кластерного дедупа вакансий."""

from __future__ import annotations

from services.vacancy_dedupe import (
    extract_headline_fingerprint,
    headline_similarity,
)


def test_headline_fingerprint_hostess():
    t1 = "‼️ **МОСКВА. ХОСТЕС-ПРОМО НА СТЕНД (3 ИЮЛЯ) ‼️**"
    t2 = "‼️ **МОСКВА. ХОСТЕС-ПРОМО НА СТЕНД (3 ИЮЛЯ) ‼️**"
    fp1 = extract_headline_fingerprint(t1)
    fp2 = extract_headline_fingerprint(t2)
    assert fp1 and fp2
    assert headline_similarity(fp1, fp2) >= 0.99


def test_cross_channel_headline_duplicate(monkeypatch):
    import parser as p

    hostess_a = (
        "‼️ **МОСКВА. ХОСТЕС-ПРОМО НА СТЕНД (3 ИЮЛЯ) ‼️**\n"
        "📍 Москва\n"
        "💰 900 ₽/ч\n"
        "👩‍💼 ХОСТЕС-ПРОМО (4 девушки)"
    )
    hostess_b = (
        "‼️ **МОСКВА. ХОСТЕС-ПРОМО НА СТЕНД (3 ИЮЛЯ) ‼️**\n"
        "📍 уточняется\n"
        "💰 900 ₽/ч\n"
        "🗓 Дата: 3 июля 2026 (пятница)"
    )

    def fake_exact(*_a, **_k):
        return False

    def fake_recent(*_a, **_k):
        return [{
            "id": "v_host_a",
            "message_text": hostess_a,
            "author_contact": None,
            "dedupe_key": "k1",
            "source_chat_title": "Channel A",
            "category_code": "promoter",
        }]

    monkeypatch.setattr(p, "has_recent_duplicate_vacancy", fake_exact)
    monkeypatch.setattr(p, "get_recent_open_vacancies_for_dedupe", fake_recent)

    dup = p.detect_duplicate_type(hostess_b, None, "k2", "promoter", "Channel B")
    assert dup in ("headline", "campaign", "fuzzy")


def test_daily_repost_same_dedupe_key():
    import parser as p

    day1 = (
        "Завтра 14.06 к 11:45\n"
        "Нужен 1 Грузчик РФ, 18+\n"
        "г. Москва, ул. Муравская, д. 38\n"
        "офисный переезд\n"
        "450/4/1800"
    )
    day2 = (
        "Завтра 15.06 к 11:45\n"
        "Нужен 1 Грузчик РФ, 18+\n"
        "г. Москва, ул. Муравская, д. 38\n"
        "офисный переезд\n"
        "450/4/1800"
    )
    assert p.build_vacancy_dedupe_key(day1, "@boss") == p.build_vacancy_dedupe_key(day2, "@boss")


def test_daily_repost_detected_as_duplicate(monkeypatch):
    import parser as p

    stored = (
        "Завтра 14.06 к 11:45\n"
        "Нужен 1 Грузчик РФ, 18+\n"
        "г. Москва, ул. Муравская, д. 38\n"
        "офисный переезд\n"
        "450/4/1800\n"
        "@evgeniy_boss"
    )
    repost = (
        "Завтра 15.06 к 11:45\n"
        "Нужен 1 Грузчик РФ, 18+\n"
        "г. Москва, ул. Муравская, д. 38\n"
        "офисный переезд\n"
        "450/4/1800\n"
        "@evgeniy_boss"
    )

    def fake_recent(*_a, **_k):
        return [{
            "id": "v_old",
            "message_text": stored,
            "author_contact": "@evgeniy_boss",
            "dedupe_key": "old_key",
            "source_chat_title": "Channel A",
            "category_code": "loader",
        }]

    monkeypatch.setattr(p, "has_recent_duplicate_vacancy", lambda *_a, **_k: False)
    monkeypatch.setattr(p, "get_recent_open_vacancies_for_dedupe", fake_recent)

    dup = p.detect_duplicate_type(
        repost,
        "@evgeniy_boss",
        p.build_vacancy_dedupe_key(repost, "@evgeniy_boss"),
        "loader",
        "Channel B",
    )
    assert dup in ("exact", "fuzzy", "campaign")


def test_detect_duplicate_excludes_self_for_channel_crosspost(monkeypatch):
    """Кросс-пост в канал: вакансия не должна считаться дублем самой себя."""
    import parser as p

    body = "Нужен промоутер на выставку, 900 ₽/ч, Москва"
    dedupe_key = p.build_vacancy_dedupe_key(body, "@boss")

    def fake_has_recent(dedupe_key, max_age_days=1, *, exclude_id=None):
        return exclude_id != "vac_self"

    monkeypatch.setattr(p, "has_recent_duplicate_vacancy", fake_has_recent)
    monkeypatch.setattr(p, "get_recent_open_vacancies_for_dedupe", lambda *_a, **_k: [])

    dup = p.detect_duplicate_type(
        body,
        "@boss",
        dedupe_key,
        "promoter",
        "Chat",
        exclude_id="vac_self",
    )
    assert dup is None


def test_find_cluster_vacancy_ids_cross_channel(monkeypatch):
    import parser as p

    loader_a = (
        "Завтра 14.06 к 11:45\n"
        "Нужен 1 Грузчик РФ, 18+\n"
        "г. Москва, ул. Муравская, д. 38, к. 1\n"
        "офисный переезд\n"
        "450/4/1800\n"
        "👉 Евгений"
    )
    loader_b = (
        "Завтра 14.06 к 11:45\n"
        "Нужен 1 Грузчик РФ, 18+\n"
        "г. Москва, ул. Муравская, д. 38, к. 1\n"
        "офисный переезд: помощь в упаковке\n"
        "450/4/1800, доп час 450\n"
        "@evgeniy_boss"
    )

    def fake_recent(*_a, **_k):
        return [{
            "id": "v_loader_a",
            "message_text": loader_a,
            "author_contact": "@Evgeniy",
            "dedupe_key": "x",
            "source_chat_title": "Channel A",
            "category_code": "loader",
        }]

    monkeypatch.setattr(p, "get_recent_open_vacancies_for_dedupe", fake_recent)

    cluster = p.find_cluster_vacancy_ids(loader_b, "@evgeniy_boss", "loader")
    assert "v_loader_a" in cluster
