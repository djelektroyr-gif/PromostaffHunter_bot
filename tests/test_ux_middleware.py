import asyncio
from unittest.mock import MagicMock

from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Chat, Message, User

from services.chat_feedback import (
    effective_typing_thread_id,
    send_typing,
    typing_keepalive,
)
from services.ux_middleware import ChatActivityMiddleware, _private_chat_id, _resolve_message_thread_id


class _FakeBot:
    def __init__(self) -> None:
        self.actions: list[tuple[int, str, int | None]] = []

    async def send_chat_action(
        self,
        chat_id: int,
        action: str,
        message_thread_id: int | None = None,
    ) -> None:
        self.actions.append((chat_id, action, message_thread_id))


def test_typing_keepalive_sends_at_least_once():
    bot = _FakeBot()

    async def _run() -> None:
        async with typing_keepalive(bot, 123, message_thread_id=55):
            await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert bot.actions and bot.actions[0] == (123, "typing", 55)


def test_send_typing_passes_thread_id():
    bot = _FakeBot()

    async def _run() -> None:
        await send_typing(bot, 7, message_thread_id=99)

    asyncio.run(_run())
    assert bot.actions == [(7, "typing", 99)]


def test_effective_typing_thread_id_general_in_forum(monkeypatch):
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", True)
    assert effective_typing_thread_id(None) == 1
    assert effective_typing_thread_id(501) == 501


def test_effective_typing_thread_id_off_without_forum(monkeypatch):
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", False)
    assert effective_typing_thread_id(None) is None


def test_send_typing_uses_general_when_forum_and_no_thread(monkeypatch):
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", True)
    bot = _FakeBot()

    async def _run() -> None:
        await send_typing(bot, 7)

    asyncio.run(_run())
    assert bot.actions == [(7, "typing", 1)]


def test_wants_min_typing_for_start_command():
    from services.ux_middleware import _wants_min_typing

    msg = MagicMock(spec=Message)
    msg.text = "/start"
    assert _wants_min_typing(msg) is True


def test_private_chat_id_from_callback():
    msg = MagicMock(spec=Message)
    msg.chat = Chat(id=42, type=ChatType.PRIVATE)
    cb = MagicMock(spec=CallbackQuery)
    cb.message = msg
    assert _private_chat_id(cb) == 42


def test_private_chat_id_ignores_group_message():
    msg = MagicMock(spec=Message)
    msg.chat = Chat(id=42, type=ChatType.GROUP)
    msg.from_user = User(id=1, is_bot=False, first_name="U")
    assert _private_chat_id(msg) is None


def test_middleware_typing_during_slow_handler(monkeypatch):
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", False)
    bot = _FakeBot()
    mw = ChatActivityMiddleware()
    msg = MagicMock(spec=Message)
    msg.chat = Chat(id=99, type=ChatType.PRIVATE)
    msg.from_user = User(id=1, is_bot=False, first_name="U")
    msg.text = "hello"
    msg.is_topic_message = False
    msg.message_thread_id = None

    async def handler(event, data):
        await asyncio.sleep(0.12)
        return "ok"

    async def _run() -> None:
        await mw(handler, msg, {"bot": bot})

    asyncio.run(_run())
    assert bot.actions
    assert bot.actions[0][0] == 99
    assert bot.actions[0][1] == "typing"


def test_resolve_thread_from_topic_message(monkeypatch):
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", False)
    msg = MagicMock(spec=Message)
    msg.text = "📖 Как пользоваться"
    msg.is_topic_message = True
    msg.message_thread_id = 77
    assert _resolve_message_thread_id(msg, 1) == 77


def test_resolve_thread_for_support_menu_button(monkeypatch):
    monkeypatch.setattr("config.FORUM_TOPICS_ENABLED", True)
    monkeypatch.setattr("db.get_user_topic_thread_id", lambda uid, key: 501 if key == "support" else None)
    msg = MagicMock(spec=Message)
    msg.text = "❓ Поддержка"
    msg.is_topic_message = False
    msg.message_thread_id = None
    assert _resolve_message_thread_id(msg, 1) == 501
