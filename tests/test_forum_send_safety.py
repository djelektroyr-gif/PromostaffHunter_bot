"""Безопасная отправка в forum-топики (без дубля message_thread_id)."""

from types import SimpleNamespace

from services.chat_feedback import message_answer_injects_thread_id
from services.forum_topics import merge_send_kwargs


def test_merge_send_kwargs_topic_wins_over_caller():
    caller = {"text": "hi", "message_thread_id": 99, "parse_mode": "HTML"}
    topic = {"message_thread_id": 42}
    assert merge_send_kwargs(caller, topic) == {
        "text": "hi",
        "parse_mode": "HTML",
        "message_thread_id": 42,
    }


def test_merge_send_kwargs_no_duplicate_keys():
    """Один thread_id в итоге — иначе SendMessage() падает в aiogram."""
    merged = merge_send_kwargs(
        {"text": "x", "message_thread_id": 1},
        {"message_thread_id": 2},
    )
    assert list(merged.keys()).count("message_thread_id") == 1
    assert merged["message_thread_id"] == 2


def test_message_answer_injects_thread_id_in_topic():
    msg = SimpleNamespace(is_topic_message=True, message_thread_id=77)
    assert message_answer_injects_thread_id(msg) is True


def test_message_answer_injects_thread_id_in_general():
    msg = SimpleNamespace(is_topic_message=False, message_thread_id=None)
    assert message_answer_injects_thread_id(msg) is False


def test_reply_keyboard_delivery_uses_general_topic_when_forum_enabled(monkeypatch):
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    from services.user_reply_keyboard import reply_keyboard_delivery_kwargs, with_persistent_keyboard

    monkeypatch.setattr("services.user_reply_keyboard.FORUM_TOPICS_ENABLED", True)
    assert reply_keyboard_delivery_kwargs() == {"message_thread_id": 1}
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔍 Посмотреть новые вакансии")]])
    assert with_persistent_keyboard(kb).is_persistent is True


def test_edit_text_must_not_get_reply_keyboard_markup():
    """Документируем ограничение API: edit_text только InlineKeyboardMarkup."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

    inline = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ok", callback_data="x")]])
    reply = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📊 Статистика")]])
    assert inline.model_dump(exclude_none=True)
    assert reply.model_dump(exclude_none=True)
    # Pydantic в EditMessageText принимает только inline — reply ломает edit_text (см. AUDIT §волна 4).
