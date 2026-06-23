# -*- coding: utf-8 -*-
"""Pin вакансии в General + история в топике."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from db import (
    clear_general_vacancy_pin,
    get_general_vacancy_pin,
    init_db,
    save_user_topic_thread,
    set_general_vacancy_pin,
)
from services.chat_feedback import GENERAL_TOPIC_THREAD_ID
from services.forum_topics import TOPIC_VACANCIES
from services.forum_vacancy_pin import (
    _delivery_kwargs,
    send_vacancy_push_pinned_general,
)


@pytest.fixture(autouse=True)
def _db():
    init_db()


def test_general_delivery_uses_thread_one(monkeypatch):
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", True)
    save_user_topic_thread(42, TOPIC_VACANCIES, 99)
    assert _delivery_kwargs(42, None) == {"message_thread_id": GENERAL_TOPIC_THREAD_ID}
    assert _delivery_kwargs(42, TOPIC_VACANCIES) == {"message_thread_id": 99}


def test_general_vacancy_pin_roundtrip():
    set_general_vacancy_pin(42, 100, "vac_abc", "<b>test</b>")
    pin = get_general_vacancy_pin(42)
    assert pin["message_id"] == 100
    assert pin["vacancy_id"] == "vac_abc"
    assert "test" in pin["card_text"]
    clear_general_vacancy_pin(42)
    assert get_general_vacancy_pin(42) is None


def test_push_edits_general_when_pin_exists(monkeypatch):
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", True)
    user_id = 778
    save_user_topic_thread(user_id, TOPIC_VACANCIES, 55)
    set_general_vacancy_pin(user_id, 10, "vac_old", "old")

    bot = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.send_message = AsyncMock()
    bot.delete_message = AsyncMock()

    async def _ensure(_uid):
        return None

    ok = asyncio.run(
        send_vacancy_push_pinned_general(
            bot,
            user_id,
            "vac_old",
            "<b>new</b>",
            None,
            rebuild_keyboard=lambda _vid: None,
            ensure_topics=_ensure,
        )
    )
    assert ok is True
    bot.edit_message_text.assert_awaited_once()
    bot.delete_message.assert_not_awaited()
    assert get_general_vacancy_pin(user_id)["message_id"] == 10
    assert get_general_vacancy_pin(user_id)["vacancy_id"] == "vac_old"


def test_push_replaces_general_and_appends_history(monkeypatch):
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", True)
    user_id = 777
    save_user_topic_thread(user_id, TOPIC_VACANCIES, 55)
    set_general_vacancy_pin(user_id, 10, "vac_old", "old", message_thread_id=GENERAL_TOPIC_THREAD_ID)

    bot = AsyncMock()
    sent = []

    async def _send(chat_id, text, **kwargs):
        msg = MagicMock()
        msg.message_id = 100 + len(sent)
        sent.append({"chat_id": chat_id, "thread": kwargs.get("message_thread_id"), "text": text})
        return msg

    bot.send_message = _send
    bot.delete_message = AsyncMock()

    async def _edit_fail(*_a, **_k):
        raise TelegramBadRequest(method="editMessageText", message="message to edit not found")

    bot.edit_message_text = _edit_fail

    async def _ensure(_uid):
        return None

    ok = asyncio.run(
        send_vacancy_push_pinned_general(
            bot,
            user_id,
            "vac_new",
            "<b>new</b>",
            None,
            rebuild_keyboard=lambda _vid: None,
            ensure_topics=_ensure,
        )
    )
    assert ok is True
    bot.delete_message.assert_awaited()
    assert get_general_vacancy_pin(user_id)["vacancy_id"] == "vac_new"
    general = [s for s in sent if s["thread"] == GENERAL_TOPIC_THREAD_ID]
    history = [s for s in sent if s["thread"] == 55]
    assert len(general) == 1
    assert len(history) == 1
    assert general[0]["text"] == "<b>new</b>"


def test_clear_general_if_pinned(monkeypatch):
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", True)
    user_id = 888
    set_general_vacancy_pin(
        user_id, 42, "vac_x", "card", message_thread_id=GENERAL_TOPIC_THREAD_ID,
    )
    bot = AsyncMock()
    bot.delete_message = AsyncMock()

    from services.forum_vacancy_pin import clear_general_vacancy_if_pinned

    asyncio.run(clear_general_vacancy_if_pinned(bot, user_id, "vac_x"))
    bot.delete_message.assert_awaited()
    assert get_general_vacancy_pin(user_id) is None
