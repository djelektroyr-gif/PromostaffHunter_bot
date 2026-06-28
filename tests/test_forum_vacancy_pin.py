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
    _clear_general_vacancy_display,
    _delivery_kwargs,
    deliver_forum_vacancy_push,
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


def test_general_push_retries_without_thread(monkeypatch):
    """General thread miss — повтор без thread_id, push не теряется."""
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", True)
    user_id = 779
    clear_general_vacancy_pin(user_id)
    save_user_topic_thread(user_id, TOPIC_VACANCIES, 55)

    bot = AsyncMock()
    calls = {"n": 0}

    async def _send(chat_id, text, **kwargs):
        calls["n"] += 1
        if kwargs.get("message_thread_id") == GENERAL_TOPIC_THREAD_ID:
            raise TelegramBadRequest(method="sendMessage", message="message thread not found")
        msg = MagicMock()
        msg.message_id = 200 + calls["n"]
        return msg

    bot.send_message = _send
    bot.delete_message = AsyncMock()

    async def _ensure(_uid):
        return None

    ok = asyncio.run(
        send_vacancy_push_pinned_general(
            bot,
            user_id,
            "vac_fb",
            "<b>fallback</b>",
            None,
            rebuild_keyboard=lambda _vid: None,
            ensure_topics=_ensure,
        )
    )
    assert ok is True
    assert calls["n"] >= 2
    assert get_general_vacancy_pin(user_id)["vacancy_id"] == "vac_fb"


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


def test_push_plain_fallback_when_delete_and_edit_fail(monkeypatch):
    """Если старую карточку не удалить — всё равно шлём push (plain fallback)."""
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", True)
    user_id = 889
    clear_general_vacancy_pin(user_id)
    save_user_topic_thread(user_id, TOPIC_VACANCIES, 55)
    set_general_vacancy_pin(user_id, 10, "vac_old", "old", message_thread_id=GENERAL_TOPIC_THREAD_ID)

    bot = AsyncMock()
    sent = []

    async def _send(chat_id, text, **kwargs):
        msg = MagicMock()
        msg.message_id = 300 + len(sent)
        sent.append(kwargs.get("message_thread_id"))
        return msg

    bot.send_message = _send
    bot.delete_message = AsyncMock(side_effect=TypeError("unexpected keyword argument"))
    bot.edit_message_text = AsyncMock(
        side_effect=TelegramBadRequest(method="editMessageText", message="message to edit not found"),
    )

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
    assert get_general_vacancy_pin(user_id)["vacancy_id"] == "vac_new"


def test_vacancies_topic_miss_does_not_fallback_to_general(monkeypatch):
    """Сбой thread «Вакансии» не должен дублировать карточку в General."""
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", True)
    user_id = 890
    save_user_topic_thread(user_id, TOPIC_VACANCIES, 55)

    bot = AsyncMock()
    threads_sent = []

    async def _send(chat_id, text, **kwargs):
        tid = kwargs.get("message_thread_id")
        threads_sent.append(tid)
        if tid == 55:
            raise TelegramBadRequest(method="sendMessage", message="message thread not found")
        msg = MagicMock()
        msg.message_id = 401
        return msg

    bot.send_message = _send
    bot.create_forum_topic = AsyncMock(
        return_value=MagicMock(message_thread_id=77),
    )

    from services.forum_vacancy_pin import append_vacancy_history_message

    asyncio.run(
        append_vacancy_history_message(
            bot, user_id, "vac_hist", "<b>x</b>", None,
        )
    )
    assert GENERAL_TOPIC_THREAD_ID not in threads_sent
    assert 55 in threads_sent or 77 in threads_sent


def test_pin_kept_when_delete_fails(monkeypatch):
    """Если delete не удался — pin не сбрасываем (повтор на следующем push)."""
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", True)
    user_id = 891
    set_general_vacancy_pin(user_id, 10, "vac_old", "old", message_thread_id=GENERAL_TOPIC_THREAD_ID)

    bot = AsyncMock()
    bot.delete_message = AsyncMock(
        side_effect=TelegramBadRequest(method="deleteMessage", message="forbidden"),
    )

    asyncio.run(_clear_general_vacancy_display(bot, user_id))
    pin = get_general_vacancy_pin(user_id)
    assert pin is not None
    assert pin["message_id"] == 10


def test_deliver_forum_push_fallback_sets_pin(monkeypatch):
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", True)
    user_id = 892
    clear_general_vacancy_pin(user_id)
    save_user_topic_thread(user_id, TOPIC_VACANCIES, 55)

    bot = AsyncMock()
    sent_threads = []

    async def _send(chat_id, text, **kwargs):
        msg = MagicMock()
        msg.message_id = 500 + len(sent_threads)
        sent_threads.append(kwargs.get("message_thread_id"))
        return msg

    bot.send_message = _send
    bot.delete_message = AsyncMock()

    async def _ensure(_uid):
        return None

    async def _pinned_fail(*_a, **_k):
        raise RuntimeError("simulated pinned failure")

    monkeypatch.setattr(
        "services.forum_vacancy_pin.send_vacancy_push_pinned_general",
        _pinned_fail,
    )

    from services.forum_vacancy_pin import deliver_forum_vacancy_push

    ok = asyncio.run(
        deliver_forum_vacancy_push(
            bot,
            user_id,
            "vac_fb",
            "<b>fb</b>",
            None,
            ensure_topics=_ensure,
        )
    )
    assert ok is True
    assert get_general_vacancy_pin(user_id)["vacancy_id"] == "vac_fb"
    assert GENERAL_TOPIC_THREAD_ID in sent_threads
    assert 55 in sent_threads
    assert len(sent_threads) == 2


def test_deliver_skips_fallback_when_pin_already_set(monkeypatch):
    """После успешного pinned fallback не дублирует карточку в General."""
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", True)
    user_id = 893
    clear_general_vacancy_pin(user_id)
    save_user_topic_thread(user_id, TOPIC_VACANCIES, 55)
    set_general_vacancy_pin(user_id, 501, "vac_done", "ok", message_thread_id=GENERAL_TOPIC_THREAD_ID)

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    bot.delete_message = AsyncMock()

    async def _pinned_ok(*_a, **_k):
        raise RuntimeError("should not be called after pin check")

    monkeypatch.setattr(
        "services.forum_vacancy_pin.send_vacancy_push_pinned_general",
        _pinned_ok,
    )

    from services.forum_vacancy_pin import deliver_forum_vacancy_push

    ok = asyncio.run(
        deliver_forum_vacancy_push(
            bot,
            user_id,
            "vac_done",
            "<b>x</b>",
            None,
            ensure_topics=AsyncMock(),
        )
    )
    assert ok is True
    bot.send_message.assert_not_awaited()
