"""Черновики LLM: sendRichMessageDraft (10.1) и sendMessageDraft (9.3)."""

from __future__ import annotations

import hashlib
import logging
from html import escape as escape_html
from typing import TYPE_CHECKING, Literal

from config import LLM_MESSAGE_DRAFT_ENABLED, LLM_RICH_MESSAGE_DRAFT_ENABLED
from services.chat_feedback import effective_typing_thread_id
from services.telegram_api_capabilities import supports_message_draft

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup

logger = logging.getLogger(__name__)

DraftMode = Literal["rich", "plain", "none"]

_DRAFT_STATUS = "⏳ Составляю текст…"
_DRAFT_PREVIEW_MAX = 900
_RICH_THINKING_HTML = f"<tg-thinking>{escape_html(_DRAFT_STATUS)}</tg-thinking>"


def make_draft_id(user_id: int, seed: str) -> int:
    digest = hashlib.sha256(f"{user_id}:{seed}".encode("utf-8")).hexdigest()
    draft_id = int(digest[:8], 16) % (2**31 - 1)
    return draft_id or 1


def _draft_kwargs(
    chat_id: int,
    draft_id: int,
    message_thread_id: int | None,
) -> dict:
    kwargs: dict = {"chat_id": chat_id, "draft_id": draft_id}
    thread_id = effective_typing_thread_id(message_thread_id)
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    return kwargs


def build_llm_enhanced_rich_html(draft: str, *, hint_html: str | None = None) -> str:
    parts = [
        "<h3>✨ Улучшенный черновик</h3>",
        f"<p>{escape_html(draft)}</p>",
    ]
    if hint_html:
        parts.append(hint_html.strip())
    parts.append("<footer>PromoStaff Hunter</footer>")
    return "\n".join(parts)


def build_llm_enhanced_preview_rich_html(draft: str) -> str:
    preview = draft if len(draft) <= _DRAFT_PREVIEW_MAX else draft[:_DRAFT_PREVIEW_MAX] + "…"
    return (
        "<h3>✨ Черновик отклика</h3>"
        f"<p>{escape_html(preview)}</p>"
    )


async def push_message_draft(
    bot: Bot,
    chat_id: int,
    draft_id: int,
    text: str | None,
    *,
    message_thread_id: int | None = None,
) -> bool:
    if not supports_message_draft(bot):
        return False
    try:
        await bot.send_message_draft(
            text=text,
            **_draft_kwargs(chat_id, draft_id, message_thread_id),
        )
        return True
    except Exception as exc:
        logger.debug("send_message_draft failed: %s", exc)
        return False


async def push_rich_message_draft(
    bot: Bot,
    chat_id: int,
    draft_id: int,
    html: str,
    *,
    message_thread_id: int | None = None,
) -> bool:
    from services.telegram_rich_message import send_rich_message_draft_html

    thread_id = effective_typing_thread_id(message_thread_id)
    return await send_rich_message_draft_html(
        bot,
        chat_id,
        draft_id,
        html,
        message_thread_id=thread_id,
    )


async def ask_llm_with_draft(
    bot: Bot,
    chat_id: int,
    user_id: int,
    prompt: str,
    *,
    seed: str,
    message_thread_id: int | None = None,
) -> tuple[str | None, DraftMode]:
    """
    Запрос к LLM с черновиком над полем ввода.
    Приоритет: sendRichMessageDraft → sendMessageDraft → без черновика.
    """
    from services.llm_client import ask_llm

    draft_id = make_draft_id(user_id, seed)
    draft_mode: DraftMode = "none"

    use_rich = LLM_RICH_MESSAGE_DRAFT_ENABLED and LLM_MESSAGE_DRAFT_ENABLED
    if use_rich:
        use_rich = await push_rich_message_draft(
            bot,
            chat_id,
            draft_id,
            _RICH_THINKING_HTML,
            message_thread_id=message_thread_id,
        )
        if use_rich:
            draft_mode = "rich"

    if not use_rich and LLM_MESSAGE_DRAFT_ENABLED and supports_message_draft(bot):
        use_plain = await push_message_draft(
            bot,
            chat_id,
            draft_id,
            _DRAFT_STATUS,
            message_thread_id=message_thread_id,
        )
        if use_plain:
            draft_mode = "plain"

    result = await ask_llm(prompt)

    if result and draft_mode == "rich":
        await push_rich_message_draft(
            bot,
            chat_id,
            draft_id,
            build_llm_enhanced_preview_rich_html(result),
            message_thread_id=message_thread_id,
        )
    elif result and draft_mode == "plain":
        preview = result if len(result) <= _DRAFT_PREVIEW_MAX else result[:_DRAFT_PREVIEW_MAX] + "…"
        await push_message_draft(
            bot,
            chat_id,
            draft_id,
            preview,
            message_thread_id=message_thread_id,
        )

    return result, draft_mode
