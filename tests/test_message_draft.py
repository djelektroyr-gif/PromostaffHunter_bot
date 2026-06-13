"""Тесты sendMessageDraft и sendRichMessageDraft для LLM."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from services.message_draft import (
    ask_llm_with_draft,
    build_llm_enhanced_preview_rich_html,
    build_llm_enhanced_rich_html,
    make_draft_id,
    push_message_draft,
    push_rich_message_draft,
)


def test_make_draft_id_stable():
    a = make_draft_id(12345, "vac_abc")
    b = make_draft_id(12345, "vac_abc")
    c = make_draft_id(12345, "vac_xyz")
    assert a == b
    assert a != c
    assert 0 < a < 2**31


def test_push_message_draft_no_method():
    bot = MagicMock(spec=[])
    assert asyncio.run(push_message_draft(bot, 1, 99, "hi")) is False


def test_build_llm_enhanced_rich_html():
    html = build_llm_enhanced_rich_html("Привет", hint_html="<p>hint</p>")
    assert "<h3>✨ Улучшенный черновик</h3>" in html
    assert "Привет" in html
    assert "<p>hint</p>" in html
    assert "<footer>PromoStaff Hunter</footer>" in html


def test_build_llm_enhanced_preview_rich_html_truncates():
    long = "x" * 1000
    html = build_llm_enhanced_preview_rich_html(long)
    assert "…" in html
    assert len(html) < len(long) + 50


def test_ask_llm_with_draft_disabled(monkeypatch):
    monkeypatch.setenv("LLM_MESSAGE_DRAFT_ENABLED", "0")
    import importlib
    import config
    import services.message_draft as md

    importlib.reload(config)
    importlib.reload(md)

    bot = MagicMock()
    bot.send_message_draft = AsyncMock()

    with patch("services.llm_client.ask_llm", new_callable=AsyncMock, return_value="ok") as ask:
        result, mode = asyncio.run(
            md.ask_llm_with_draft(bot, 10, 10, "prompt", seed="s1"),
        )

    assert result == "ok"
    assert mode == "none"
    ask.assert_awaited_once_with("prompt")
    bot.send_message_draft.assert_not_awaited()


def test_ask_llm_with_draft_plain_enabled(monkeypatch):
    monkeypatch.setenv("LLM_MESSAGE_DRAFT_ENABLED", "1")
    monkeypatch.setenv("LLM_RICH_MESSAGE_DRAFT_ENABLED", "0")
    import importlib
    import config
    import services.message_draft as md

    importlib.reload(config)
    importlib.reload(md)

    bot = MagicMock()
    bot.send_message_draft = AsyncMock(return_value=True)

    with patch("services.llm_client.ask_llm", new_callable=AsyncMock, return_value="final text") as ask:
        result, mode = asyncio.run(
            md.ask_llm_with_draft(bot, 10, 10, "prompt", seed="s1"),
        )

    assert result == "final text"
    assert mode == "plain"
    ask.assert_awaited_once()
    assert bot.send_message_draft.await_count == 2
    first_call = bot.send_message_draft.await_args_list[0].kwargs
    assert first_call["text"] == "⏳ Составляю текст…"
    assert first_call["draft_id"] == make_draft_id(10, "s1")


def test_ask_llm_with_rich_draft(monkeypatch):
    monkeypatch.setenv("LLM_MESSAGE_DRAFT_ENABLED", "1")
    monkeypatch.setenv("LLM_RICH_MESSAGE_DRAFT_ENABLED", "1")
    import importlib
    import config
    import services.message_draft as md

    importlib.reload(config)
    importlib.reload(md)

    bot = MagicMock()
    rich_calls: list[str] = []

    async def fake_rich_draft(bot, chat_id, draft_id, html, **kwargs):
        rich_calls.append(html)
        return True

    with patch.object(md, "push_rich_message_draft", new=AsyncMock(side_effect=fake_rich_draft)):
        with patch("services.llm_client.ask_llm", new_callable=AsyncMock, return_value="rich result") as ask:
            result, mode = asyncio.run(
                md.ask_llm_with_draft(bot, 10, 10, "prompt", seed="s1"),
            )

    assert result == "rich result"
    assert mode == "rich"
    ask.assert_awaited_once()
    assert len(rich_calls) == 2
    assert "<tg-thinking>" in rich_calls[0]
    assert "rich result" in rich_calls[1]


def test_push_rich_message_draft_delegates(monkeypatch):
    monkeypatch.setenv("LLM_RICH_MESSAGE_DRAFT_ENABLED", "1")
    import importlib
    import services.message_draft as md

    importlib.reload(md)

    bot = MagicMock()
    with patch(
        "services.telegram_rich_message.send_rich_message_draft_html",
        new_callable=AsyncMock,
        return_value=True,
    ) as send_rich:
        ok = asyncio.run(
            md.push_rich_message_draft(bot, 5, 42, "<p>hi</p>"),
        )

    assert ok is True
    send_rich.assert_awaited_once()
    assert send_rich.await_args.args[2] == 42
    assert send_rich.await_args.args[3] == "<p>hi</p>"
