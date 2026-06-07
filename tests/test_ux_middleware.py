import asyncio
from unittest.mock import MagicMock

from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Chat, Message, User

from services.ux_middleware import ChatActivityMiddleware, _private_chat_id


class _FakeBot:
    def __init__(self) -> None:
        self.actions: list[tuple[int, str]] = []

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.actions.append((chat_id, action))


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


def test_middleware_typing_during_slow_handler():
    bot = _FakeBot()
    mw = ChatActivityMiddleware()
    msg = MagicMock(spec=Message)
    msg.chat = Chat(id=99, type=ChatType.PRIVATE)
    msg.from_user = User(id=1, is_bot=False, first_name="U")

    async def handler(event, data):
        await asyncio.sleep(0.12)
        return "ok"

    async def _run() -> None:
        await mw(handler, msg, {"bot": bot})

    asyncio.run(_run())
    assert bot.actions
    assert bot.actions[0] == (99, "typing")
