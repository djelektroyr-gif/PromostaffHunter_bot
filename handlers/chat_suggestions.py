"""Premium: предложить канал/чат для мониторинга парсером."""

from __future__ import annotations

import logging

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import YOUR_USER_ID
from db import activate_target_chat, get_chat_suggestion, is_user_premium, resolve_chat_suggestion
from services.chat_suggest_flow import (
    SuggestChatError,
    format_user_accepted_html,
    format_user_approved_html,
    format_user_rejected_html,
    submit_chat_suggestion,
)
from services.admin_inbox_alerts import notify_admin_chat_suggestion

BTN_SUGGEST_CHAT = "📡 Предложить канал"
router = Router(name="chat_suggestions")
logger = logging.getLogger(__name__)


class ChatSuggestState(StatesGroup):
    waiting_for_link = State()


class ChatSuggestRejectState(StatesGroup):
    waiting_for_reason = State()


def _premium_required_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Подписка", callback_data="subscription_request")],
    ])


async def _answer_premium_required(message: types.Message) -> None:
    await message.answer(
        "📡 <b>Предложить канал</b> — только для подписчиков Premium.\n\n"
        "Premium-пользователи могут предложить Telegram-канал или чат с вакансиями — "
        "мы проверим и добавим в мониторинг.",
        parse_mode="HTML",
        reply_markup=_premium_required_keyboard(),
    )


@router.message(F.text == BTN_SUGGEST_CHAT)
async def suggest_chat_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_user_premium(user_id):
        await _answer_premium_required(message)
        return
    await state.clear()
    await message.answer(
        "📡 <b>Предложить канал для мониторинга</b>\n\n"
        "Отправьте ссылку или @username канала/чата с вакансиями:\n"
        "• <code>@channelname</code>\n"
        "• <code>https://t.me/channelname</code>\n\n"
        "Заявка уходит администратору на проверку.\n"
        "Отмена — /cancel",
        parse_mode="HTML",
    )
    await state.set_state(ChatSuggestState.waiting_for_link)


@router.message(ChatSuggestState.waiting_for_link)
async def suggest_chat_link_received(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_user_premium(user_id):
        await state.clear()
        await _answer_premium_required(message)
        return
    if message.text and message.text.strip().lower() in ("/cancel", "отмена"):
        await state.clear()
        await message.answer(
            "Отменено.",
            reply_markup=get_settings_keyboard_for_user(user_id),
        )
        return
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пришлите ссылку или @username канала.")
        return
    try:
        suggestion_id, chat_link, probe = await submit_chat_suggestion(
            message.bot,
            user_id,
            raw,
            username=message.from_user.username,
        )
    except SuggestChatError as e:
        await message.answer(f"⚠️ {e.args[0]}")
        return

    await notify_admin_chat_suggestion(
        message.bot,
        suggestion_id=suggestion_id,
        user_id=user_id,
        username=message.from_user.username,
        chat_link=chat_link,
        chat_title=probe.get("title"),
        probe=probe,
    )
    await message.answer(
        format_user_accepted_html(chat_link),
        parse_mode="HTML",
        reply_markup=get_settings_keyboard_for_user(user_id),
    )
    await state.clear()


@router.callback_query(F.data.startswith("chs_ok:"))
async def admin_suggestion_approve(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        suggestion_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Неверный ID", show_alert=True)
        return

    row = get_chat_suggestion(suggestion_id)
    if not row or row["status"] != "pending":
        await callback.answer("Заявка не найдена или уже обработана", show_alert=True)
        return

    chat_link = row["chat_link"]
    if not activate_target_chat(chat_link):
        await callback.answer("Не удалось добавить в БД", show_alert=True)
        return

    resolve_chat_suggestion(suggestion_id, "approved")
    await callback.answer("Канал добавлен")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    await callback.message.answer(
        f"✅ Заявка #{suggestion_id}: {chat_link} добавлен в target_chats.\n"
        "Проверьте доступ Telethon в «📋 Список чатов парсинга».",
    )

    try:
        await callback.bot.send_message(
            row["user_id"],
            format_user_approved_html(chat_link),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("notify user approve #%s: %s", suggestion_id, e)
        await callback.message.answer(f"⚠️ Пользователю {row['user_id']} не удалось отправить уведомление.")


@router.callback_query(F.data.startswith("chs_no:"))
async def admin_suggestion_reject_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        suggestion_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Неверный ID", show_alert=True)
        return
    row = get_chat_suggestion(suggestion_id)
    if not row or row["status"] != "pending":
        await callback.answer("Заявка не найдена или уже обработана", show_alert=True)
        return
    await callback.answer()
    await state.update_data(reject_suggestion_id=suggestion_id)
    await state.set_state(ChatSuggestRejectState.waiting_for_reason)
    await callback.message.answer(
        f"❌ Отклонение заявки #{suggestion_id}\n\n"
        "Коротко укажите причину для пользователя или отправьте «—» без пояснения.\n"
        "/cancel — отмена",
    )


@router.message(ChatSuggestRejectState.waiting_for_reason)
async def admin_suggestion_reject_reason(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    if message.text and message.text.strip().lower() in ("/cancel", "отмена"):
        await state.clear()
        await message.answer("Отклонение отменено.")
        return
    data = await state.get_data()
    suggestion_id = data.get("reject_suggestion_id")
    if not suggestion_id:
        await state.clear()
        await message.answer("Сессия истекла.")
        return
    row = get_chat_suggestion(int(suggestion_id))
    if not row or row["status"] != "pending":
        await state.clear()
        await message.answer("Заявка уже обработана.")
        return
    reason_raw = (message.text or "").strip()
    reason = None if reason_raw in ("", "—", "-") else reason_raw
    resolve_chat_suggestion(int(suggestion_id), "rejected", admin_note=reason)
    await state.clear()
    await message.answer(f"Заявка #{suggestion_id} отклонена.")

    try:
        await message.bot.send_message(
            row["user_id"],
            format_user_rejected_html(row["chat_link"], reason),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning("notify user reject #%s: %s", suggestion_id, e)


def get_settings_keyboard_for_user(user_id: int):
    """Клавиатура настроек с Premium-кнопкой заявки на канал."""
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    from handlers.premium_filters import BTN_PREMIUM_FILTERS

    BTN_SETTINGS_CATEGORIES = "📌 Категории вакансий"
    BTN_SETTINGS_BACK = "◀️ В главное меню"
    rows = [[KeyboardButton(text=BTN_SETTINGS_CATEGORIES)]]
    if is_user_premium(user_id):
        rows.append([KeyboardButton(text=BTN_SUGGEST_CHAT)])
    rows.append([KeyboardButton(text=BTN_PREMIUM_FILTERS)])
    rows.append([KeyboardButton(text=BTN_SETTINGS_BACK)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
