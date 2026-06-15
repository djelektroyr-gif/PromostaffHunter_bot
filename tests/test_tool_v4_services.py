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
    assert "Открыть" in row0.text
    assert row0.url.startswith("https://t.me/")


def test_channel_preview_text_structured():
    body = (
        "Нужны 2 промоутера на завтра\n"
        "м. Таганская\n"
        "Раздача листовок\n"
        "2000₽/смена\n"
        "☎️ @boss123"
    )
    text = build_channel_preview_text(
        category_name="Промоутер",
        category_emoji="📢",
        category_code="promoter",
        body=body,
        source="Secret Group",
        freshness="🟢 Свежая: несколько часов назад",
    )
    assert "Промоутер" in text
    assert "Свежая" in text or "Актуальна" in text
    assert "Опубликовано" not in text
    assert "завтра" in text
    assert "Secret Group" not in text
    assert "boss123" not in text.lower()
    assert "t.me/" not in text.lower()
    assert "Открыть в боте" in text or "в боте" in text.lower()
    assert "·" in text


def test_bot_vacancy_card_shows_publication_time():
    import importlib.util
    from pathlib import Path

    main_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("hunter_main_card", main_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    text = mod.format_vacancy_card_html(
        category_emoji="📦",
        category_name="Грузчик",
        freshness="🟢 Свежая: только что",
        published_at="06.06.2026 16:52 МСК",
        body="Разгрузка машины, 400 р/ч",
        source="Secret Group",
        message_link="https://t.me/c/123/456",
    )
    assert "06.06.2026 16:52 МСК" in text
    assert "Опубликовано" in text
    assert "Secret Group" not in text
    assert "t.me/c" not in text
