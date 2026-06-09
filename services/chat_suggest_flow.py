"""Premium: предложение канала/чата для мониторинга парсером."""

from __future__ import annotations

import logging
import re
from html import escape as escape_html

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from config import CHAT_SUGGEST_DAILY_LIMIT
from db import (
    count_pending_chat_suggestions,
    count_user_chat_suggestions_since,
    create_chat_suggestion,
    get_pending_chat_suggestion_for_link,
    target_chat_is_active,
)

logger = logging.getLogger(__name__)

_INVITE_LINK_RE = re.compile(r"t\.me/\+", re.I)


def normalize_chat_link(raw: str) -> str | None:
    if not raw:
        return None
    link = raw.strip()
    if link.startswith("@"):
        return f"https://t.me/{link[1:]}"
    if link.startswith("https://t.me/"):
        return link.rstrip("/")
    if link.startswith("http://t.me/"):
        return "https://" + link[len("http://") :].rstrip("/")
    if link.startswith("t.me/"):
        return f"https://{link}".rstrip("/")
    if re.fullmatch(r"[a-zA-Z0-9_]{5,32}", link):
        return f"https://t.me/{link}"
    return None


def username_from_chat_link(chat_link: str) -> str | None:
    if not chat_link or _INVITE_LINK_RE.search(chat_link):
        return None
    m = re.search(r"t\.me/([a-zA-Z0-9_]{5,32})$", chat_link.rstrip("/"))
    return f"@{m.group(1)}" if m else None


async def probe_public_chat(bot: Bot, chat_link: str) -> dict:
    """Bot API: публичный канал/группа по @username. Invite-ссылки — без проверки."""
    username = username_from_chat_link(chat_link)
    if not username:
        return {"ok": None, "title": None, "hint": "invite_link"}
    try:
        chat = await bot.get_chat(username)
        kind = chat.type.value if hasattr(chat.type, "value") else str(chat.type)
        if kind not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
            return {"ok": False, "title": chat.title, "hint": "not_a_channel"}
        return {"ok": True, "title": chat.title, "hint": kind}
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.info("probe_public_chat %s: %s", chat_link, e)
        return {"ok": False, "title": None, "hint": "not_found"}
    except Exception as e:
        logger.warning("probe_public_chat %s: %s", chat_link, e)
        return {"ok": None, "title": None, "hint": "error"}


class SuggestChatError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


async def submit_chat_suggestion(
    bot: Bot,
    user_id: int,
    raw_link: str,
    *,
    username: str | None = None,
) -> tuple[int, str, dict]:
    """
    Валидация и создание заявки.
    Returns: (suggestion_id, chat_link, probe_info)
    """
    chat_link = normalize_chat_link(raw_link)
    if not chat_link:
        raise SuggestChatError("bad_format", "Неверный формат ссылки.")

    if target_chat_is_active(chat_link):
        raise SuggestChatError(
            "already_monitored",
            "Этот канал уже в списке мониторинга — новые вакансии оттуда уже собираем.",
        )

    if get_pending_chat_suggestion_for_link(chat_link):
        raise SuggestChatError(
            "already_pending",
            "Заявка на этот канал уже на рассмотрении у администратора.",
        )

    if count_user_chat_suggestions_since(user_id, 24) >= CHAT_SUGGEST_DAILY_LIMIT:
        raise SuggestChatError(
            "rate_limit",
            f"Лимит заявок — {CHAT_SUGGEST_DAILY_LIMIT} в сутки. Попробуйте завтра.",
        )

    probe = await probe_public_chat(bot, chat_link)
    if probe.get("ok") is False and probe.get("hint") == "not_a_channel":
        raise SuggestChatError(
            "not_a_channel",
            "Похоже, это не канал или группа с вакансиями. Пришлите ссылку на чат/канал.",
        )

    suggestion_id = create_chat_suggestion(
        user_id,
        chat_link,
        user_username=username,
        chat_title=probe.get("title"),
    )
    if not suggestion_id:
        raise SuggestChatError("db_error", "Не удалось сохранить заявку. Попробуйте позже.")

    return suggestion_id, chat_link, probe


def format_user_accepted_html(chat_link: str) -> str:
    link = escape_html(chat_link)
    return (
        "✅ <b>Заявка принята к рассмотрению</b>\n\n"
        f"Канал: {link}\n\n"
        "После проверки администратор добавит его в мониторинг или сообщит причину отказа. "
        "Обычно это занимает до 1–2 рабочих дней."
    )


def format_user_approved_html(chat_link: str) -> str:
    return (
        "🎉 <b>Канал добавлен в мониторинг</b>\n\n"
        f"{escape_html(chat_link)}\n\n"
        "Новые вакансии оттуда будут попадать в общую базу — "
        "вы увидите их по своим категориям в ленте или push (Premium)."
    )


def format_user_rejected_html(chat_link: str, reason: str | None = None) -> str:
    body = reason.strip() if reason else (
        "Канал не подошёл для мониторинга (нет доступа парсера, не вакансии или дубликат)."
    )
    return (
        "ℹ️ <b>Заявка на канал отклонена</b>\n\n"
        f"{escape_html(chat_link)}\n\n"
        f"{escape_html(body)}"
    )


def format_admin_chat_suggestion_html(
    *,
    suggestion_id: int,
    user_id: int,
    username: str | None,
    chat_link: str,
    chat_title: str | None,
    probe: dict | None = None,
) -> str:
    uname = f"@{escape_html(username)}" if username else "—"
    title = escape_html(chat_title) if chat_title else "—"
    queue = count_pending_chat_suggestions()
    probe_line = ""
    if probe:
        hint = probe.get("hint") or "—"
        ok = probe.get("ok")
        if ok is True:
            probe_line = f"\n🤖 Bot API: канал найден ({escape_html(str(hint))})"
        elif ok is False:
            probe_line = f"\n⚠️ Bot API: не найден / закрыт ({escape_html(str(hint))})"
        else:
            probe_line = "\n🔗 Invite-ссылка или проверка недоступна — проверьте доступ Telethon"
    return (
        f"📡 <b>Новая заявка на мониторинг #{suggestion_id}</b> (в очереди: {queue})\n\n"
        f"👤 Premium · ID <code>{user_id}</code> · {uname}\n"
        f"📎 {escape_html(chat_link)}\n"
        f"📛 Название: {title}"
        f"{probe_line}\n\n"
        "После «Добавить» проверьте доступ в «📋 Список чатов парсинга»."
    )
