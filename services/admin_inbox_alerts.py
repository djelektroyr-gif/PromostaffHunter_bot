"""Push-уведомления админу: поддержка и жалобы."""

from __future__ import annotations

import logging
from html import escape as escape_html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import YOUR_USER_ID
from db import get_unanswered_support_requests, get_vacancy_push_row

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
                text="✉️ Ответить",
                callback_data=f"cmp_r:{complaint_id}",
            ),
            InlineKeyboardButton(
                text="✅ Обработано",
                callback_data=f"cmp_ok:{complaint_id}",
            ),
        ],
        [
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


def chat_suggestion_action_keyboard(suggestion_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Добавить",
                callback_data=f"chs_ok:{suggestion_id}",
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"chs_no:{suggestion_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="👤 Карточка",
                callback_data=f"adm_u_{user_id}_0",
            ),
        ],
    ])


async def notify_admin_chat_suggestion(
    bot,
    *,
    suggestion_id: int,
    user_id: int,
    username: str | None,
    chat_link: str,
    chat_title: str | None = None,
    probe: dict | None = None,
) -> None:
    if not YOUR_USER_ID:
        return
    from services.chat_suggest_flow import format_admin_chat_suggestion_html

    text = format_admin_chat_suggestion_html(
        suggestion_id=suggestion_id,
        user_id=user_id,
        username=username,
        chat_link=chat_link,
        chat_title=chat_title,
        probe=probe,
    )
    markup = chat_suggestion_action_keyboard(suggestion_id, user_id)
    try:
        await bot.send_message(
            YOUR_USER_ID,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("notify_admin_chat_suggestion #%s: %s", suggestion_id, e)


def notfit_admin_keyboard(vacancy_id: str, user_id: int, message_link: str | None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text="👤 Карточка",
                callback_data=f"adm_u_{user_id}_0",
            ),
        ],
    ]
    if message_link and str(message_link).startswith("http"):
        rows.append([InlineKeyboardButton(text="🔗 Пост в канале", url=message_link)])
    rows.append([InlineKeyboardButton(text="🟡 Все отзывы", callback_data="adm_notfit_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_notfit_admin_html(
    *,
    feedback_id: int,
    user_id: int,
    username: str | None,
    vacancy_id: str,
    reason_code: str,
    reason_label: str,
    reason_text: str | None,
    vacancy_category: str | None,
    source_chat_title: str | None,
    message_preview: str | None,
) -> str:
    uname = f"@{escape_html(username)}" if username else "—"
    comment = escape_html((reason_text or "").strip()) or "—"
    preview = escape_html((message_preview or "").replace("\n", " ")[:220])
    if message_preview and len(message_preview) > 220:
        preview += "…"
    chat = escape_html(source_chat_title or "—")
    cat = escape_html(vacancy_category or "—")
    return (
        f"🟡 <b>Не подходит #{feedback_id}</b>\n\n"
        f"👤 <code>{user_id}</code> · {uname}\n"
        f"📋 {escape_html(reason_label)}"
        f"{f' ({escape_html(reason_code)})' if reason_code else ''}\n"
        f"💬 <b>Комментарий:</b> {comment}\n"
        f"🏷 Категория: {cat} · чат: {chat}\n"
        f"🆔 <code>{escape_html(vacancy_id or '—')}</code>\n\n"
        f"{preview}"
    )


async def notify_admin_notfit_feedback(
    bot,
    *,
    feedback_id: int,
    user_id: int,
    vacancy_id: str,
    reason_code: str,
    reason_label: str,
    reason_text: str | None,
    username: str | None = None,
) -> None:
    if not YOUR_USER_ID:
        return
    row = get_vacancy_push_row(vacancy_id)
    message_text = row[0] if row else None
    message_link = row[1] if row else None
    source_chat = row[2] if row else None
    vac_cat = row[5] if row else None
    if username is None:
        from db import get_subscriber_profile
        profile = get_subscriber_profile(user_id) or {}
        username = profile.get("username")
    text = format_notfit_admin_html(
        feedback_id=feedback_id,
        user_id=user_id,
        username=username,
        vacancy_id=vacancy_id,
        reason_code=reason_code,
        reason_label=reason_label,
        reason_text=reason_text,
        vacancy_category=vac_cat,
        source_chat_title=source_chat,
        message_preview=message_text,
    )
    markup = notfit_admin_keyboard(vacancy_id, user_id, message_link)
    try:
        await bot.send_message(
            YOUR_USER_ID,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("notify_admin_notfit_feedback #%s: %s", feedback_id, e)
