"""Push-уведомления админу: поддержка и жалобы."""

from __future__ import annotations

import logging
from html import escape as escape_html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import YOUR_USER_ID
from db import get_unanswered_support_requests

logger = logging.getLogger(__name__)


def support_reply_keyboard(request_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✉️ Ответить",
                callback_data=f"sup_r:{request_id}",
            ),
            InlineKeyboardButton(
                text="👤 Карточка",
                callback_data=f"adm_u_{user_id}_0",
            ),
        ],
    ])


def complaint_action_keyboard(complaint_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Обработано",
                callback_data=f"cmp_ok:{complaint_id}",
            ),
            InlineKeyboardButton(
                text="👤 Карточка",
                callback_data=f"adm_u_{user_id}_0",
            ),
        ],
    ])


def format_support_admin_html(
    *,
    request_id: int,
    user_id: int,
    username: str | None,
    message_text: str,
    pending_count: int | None = None,
) -> str:
    uname = f"@{escape_html(username)}" if username else "—"
    preview = escape_html((message_text or "")[:500])
    if len(message_text or "") > 500:
        preview += "…"
    queue = pending_count if pending_count is not None else len(get_unanswered_support_requests(100))
    return (
        f"❓ <b>Новое обращение #{request_id}</b> (в очереди: {queue})\n\n"
        f"👤 ID: <code>{user_id}</code>\n"
        f"Username: {uname}\n\n"
        f"{preview}"
    )


def format_complaint_admin_html(
    *,
    complaint_id: int,
    user_id: int,
    full_name: str | None,
    username: str | None,
    vacancy_id: str,
    reason: str,
    complaint_text: str | None,
) -> str:
    uname = f"@{escape_html(username)}" if username else "—"
    name = escape_html(full_name or "—")
    body = escape_html(complaint_text) if complaint_text else "—"
    return (
        f"⚠️ <b>Жалоба #{complaint_id}</b>\n\n"
        f"👤 {name} · ID <code>{user_id}</code> · {uname}\n"
        f"🆔 вакансия: <code>{escape_html(vacancy_id or '—')}</code>\n"
        f"📋 Причина: {escape_html(reason or '—')}\n"
        f"💬 Текст: {body}"
    )


async def notify_admin_support_request(bot, request_id: int, user_id: int, username: str | None, message_text: str) -> None:
    if not YOUR_USER_ID:
        return
    text = format_support_admin_html(
        request_id=request_id,
        user_id=user_id,
        username=username,
        message_text=message_text,
    )
    markup = support_reply_keyboard(request_id, user_id)
    try:
        await bot.send_message(
            YOUR_USER_ID,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("notify_admin_support_request #%s: %s", request_id, e)


async def notify_admin_complaint(
    bot,
    complaint_id: int,
    user_id: int,
    vacancy_id: str,
    reason: str,
    complaint_text: str | None = None,
    *,
    full_name: str | None = None,
    username: str | None = None,
) -> None:
    if not YOUR_USER_ID:
        return
    if full_name is None or username is None:
        from db import get_subscriber_profile
        profile = get_subscriber_profile(user_id) or {}
        full_name = full_name or profile.get("full_name")
        username = username or profile.get("username")
    text = format_complaint_admin_html(
        complaint_id=complaint_id,
        user_id=user_id,
        full_name=full_name,
        username=username,
        vacancy_id=vacancy_id,
        reason=reason,
        complaint_text=complaint_text,
    )
    markup = complaint_action_keyboard(complaint_id, user_id)
    try:
        await bot.send_message(
            YOUR_USER_ID,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("notify_admin_complaint #%s: %s", complaint_id, e)
