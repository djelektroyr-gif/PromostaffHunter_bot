"""Push-уведомления админу: ошибки, регистрация, зависшие анкеты."""

from __future__ import annotations

import logging
import time
from html import escape as escape_html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import YOUR_USER_ID
from db import log_bot_event

logger = logging.getLogger(__name__)

_ERROR_ALERT_COOLDOWN_SEC = 600
_error_alert_last: dict[str, float] = {}


def _user_label(user_id: int, username: str | None = None, first_name: str | None = None) -> str:
    parts = []
    if first_name:
        parts.append(escape_html(first_name))
    if username:
        parts.append(f"@{escape_html(username)}")
    parts.append(f"<code>{user_id}</code>")
    return " · ".join(parts)


def _admin_user_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Карточка", callback_data=f"adm_u_{user_id}_0")],
    ])


def _should_alert_error(key: str) -> bool:
    now = time.monotonic()
    last = _error_alert_last.get(key, 0.0)
    if now - last < _ERROR_ALERT_COOLDOWN_SEC:
        return False
    _error_alert_last[key] = now
    return True


async def notify_admin_handler_error(
    bot,
    *,
    user_id: int | None,
    handler: str,
    error_text: str,
) -> None:
    if not YOUR_USER_ID:
        return
    key = f"{handler}:{error_text[:80]}"
    if not _should_alert_error(key):
        return
    log_bot_event(user_id, "handler_error", {"handler": handler, "error": error_text[:300]})
    uid_line = f"👤 <code>{user_id}</code>" if user_id else "👤 —"
    text = (
        f"🚨 <b>Ошибка в боте</b>\n\n"
        f"{uid_line}\n"
        f"Handler: <code>{escape_html(handler[:120])}</code>\n\n"
        f"{escape_html(error_text[:500])}"
    )
    markup = _admin_user_keyboard(user_id) if user_id else None
    try:
        await bot.send_message(
            YOUR_USER_ID,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("notify_admin_handler_error: %s", e)


async def notify_admin_registration_success(
    bot,
    *,
    user_id: int,
    username: str | None,
    first_name: str | None,
    role: str,
    categories: list[str] | None = None,
) -> None:
    if not YOUR_USER_ID:
        return
    if role == "employer":
        title = "✅ Новый заказчик"
        extra = ""
    elif categories:
        title = "✅ Исполнитель — категории выбраны"
        extra = f"\n📌 {escape_html(', '.join(categories))}"
    else:
        title = "✅ Анкета исполнителя"
        extra = "\n<i>Категории ещё не выбраны</i>"
    text = f"{title}\n\n👤 {_user_label(user_id, username, first_name)}{extra}"
    try:
        await bot.send_message(
            YOUR_USER_ID,
            text,
            parse_mode="HTML",
            reply_markup=_admin_user_keyboard(user_id),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("notify_admin_registration_success user=%s: %s", user_id, e)


async def notify_admin_registration_stuck(bot, row: dict) -> None:
    if not YOUR_USER_ID:
        return
    text = (
        f"⏳ <b>Зависла регистрация</b>\n\n"
        f"👤 {_user_label(row['user_id'], row.get('username'), row.get('first_name'))}\n"
        f"Роль: {escape_html(row.get('user_role') or '—')}\n"
        f"Причина: {escape_html(row.get('reason') or '—')}"
    )
    try:
        await bot.send_message(
            YOUR_USER_ID,
            text,
            parse_mode="HTML",
            reply_markup=_admin_user_keyboard(row["user_id"]),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("notify_admin_registration_stuck user=%s: %s", row.get("user_id"), e)


async def notify_admin_registration_validation_loop(
    bot,
    *,
    user_id: int,
    username: str | None,
    first_name: str | None,
    step: str,
    fail_count: int,
) -> None:
    if not YOUR_USER_ID:
        return
    text = (
        f"⚠️ <b>Не может пройти регистрацию</b>\n\n"
        f"👤 {_user_label(user_id, username, first_name)}\n"
        f"Шаг: {escape_html(step)}\n"
        f"Ошибок валидации за час: {fail_count}"
    )
    try:
        await bot.send_message(
            YOUR_USER_ID,
            text,
            parse_mode="HTML",
            reply_markup=_admin_user_keyboard(user_id),
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("notify_admin_registration_validation_loop user=%s: %s", user_id, e)
