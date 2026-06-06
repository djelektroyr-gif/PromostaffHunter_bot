import asyncio

from services.chat_feedback import typing_keepalive


class _FakeBot:
    def __init__(self) -> None:
        self.actions: list[tuple[int, str]] = []

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.actions.append((chat_id, action))


def test_typing_keepalive_sends_at_least_once():
    bot = _FakeBot()

    async def _run() -> None:
        async with typing_keepalive(bot, 123):
            await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert bot.actions and bot.actions[0] == (123, "typing")
