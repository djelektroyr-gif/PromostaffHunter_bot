"""Тесты forum topics и channel preview."""

from services.channel_post import build_channel_preview_keyboard, build_channel_preview_text
from services.forum_topics import TOPIC_VACANCIES, topic_message_kwargs


def test_topic_message_kwargs_empty_without_db(monkeypatch):
    monkeypatch.setattr("services.forum_topics.get_user_topic_thread_id", lambda uid, key: None)
    assert topic_message_kwargs(123, TOPIC_VACANCIES) == {}


def test_topic_message_kwargs_with_thread(monkeypatch):
    monkeypatch.setattr("services.forum_topics.get_user_topic_thread_id", lambda uid, key: 42)
    assert topic_message_kwargs(123, TOPIC_VACANCIES) == {"message_thread_id": 42}


def test_channel_preview_keyboard_urls():
    kb = build_channel_preview_keyboard("abc123")
    row0 = kb.inline_keyboard[0][0]
    assert "vac_abc123" in row0.url
    assert row0.url.startswith("https://t.me/")


def test_channel_preview_text_truncates():
    body = (
        "На завтра нужен промоутер у метро Таганская. Оплата 2000.\n"
        "☎️ @boss123"
    )
    text = build_channel_preview_text(
        category_name="Промоутер",
        category_emoji="📢",
        body=body,
        source="Secret Group",
        freshness="несколько часов назад",
    )
    assert "Промоутер" in text
    assert "несколько часов назад" in text
    assert "Secret Group" not in text
    assert "@" not in text
    assert "boss123" not in text
    assert "·" in text
