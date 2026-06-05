import asyncio
import os
import re
import logging
from html import escape as escape_html
from datetime import datetime, timezone, date, timedelta
from urllib.parse import quote
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, BotCommand
from db import *
from db_backend import db_conn, fetchone, now_minus_days, bool_false, run_db
from parser import (
    run_parser, get_last_debug_report, detect_category, extract_contact_from_text,
    start_realtime_listener, stop_realtime_listener, get_new_messages, extract_address_from_text,
    make_vacancy_id, PARSER_LABEL, inspect_parser_chats, format_parser_chats_report,
    get_parser_status_snapshot, format_parser_status_line, vacancy_matches_user_metro,
    spawn_background_task,
)
from config import (
    BOT_TOKEN, YOUR_USER_ID, SUBSCRIPTION_PAY_URL, SUBSCRIPTION_SUPPORT,
    SUBSCRIPTION_PRICE_RUB, SUBSCRIPTION_CARD_HINT, TRIAL_DAYS, VACANCY_MAX_AGE_HOURS,
)
from profile_photos import (
    get_user_photos_dir, persist_user_photo, photo_health_loop, send_profile_photo,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Метка сборки — в логах и /status, чтобы проверить деплой на Bothost.
APP_BUILD = os.getenv("APP_BUILD", "profile-menu-v1")

BTN_SETTINGS = "⚙️ Настройки"
BTN_MY_DATA = "👤 Мои данные"
BTN_SETTINGS_LEGACY = "📋 Категории"
BTN_MY_DATA_LEGACY = "📞 Мои контакты"
BTN_UNSUB_LEGACY = "❌ Отписаться"

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Flood control для рассылки (пауза между отправками)
SEND_DELAY = 1  # секунда между push-вакансиями одному пользователю
MSK_TZ = timezone(timedelta(hours=3))
_vacancy_push_sem = asyncio.Semaphore(2)  # не более 2 параллельных push-рассылок
BROADCAST_DELAY = 0.08  # ~12 msg/s — безопаснее для Bot API при массовой рассылке
FREE_CATEGORY_LIMIT = 3
RESPONSES_PAGE_SIZE = 5
PREMIUM_RENEWAL_WARN_DAYS = 7
PREMIUM_DEFAULT_DAYS = 30
_processing_finish: set[int] = set()


def escape_markdown(text: str) -> str:
    if not text:
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)


def build_maps_url(address: str) -> str | None:
    if not address or not address.strip():
        return None
    url = f"https://yandex.ru/maps/?text={quote(address.strip())}"
    if len(url) > 2048 or not url.startswith("https://"):
        return None
    return url


def build_vacancy_keyboard(vacancy_id: str, address: str | None = None) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="✋ Откликнуться", callback_data=f"respond_{vacancy_id}")]]
    maps_url = build_maps_url(address) if address else None
    if maps_url:
        buttons.append([InlineKeyboardButton(text="🗺️ Показать на карте", url=maps_url)])
    buttons.append([InlineKeyboardButton(text="⚠️ Пожаловаться", callback_data=f"complain_{vacancy_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_vacancy_card_html(
    *,
    category_emoji: str,
    category_name: str,
    freshness: str,
    published_at: str,
    body: str,
    source: str,
    message_link: str | None = None,
) -> str:
    lines = [
        f"{category_emoji} <b>{escape_html(category_name)}</b>",
        escape_html(freshness),
        f"🕒 Опубликовано: {escape_html(published_at)}",
        f"📢 Из чата: {escape_html(source)}",
        "",
        escape_html((body or "")[:500]),
    ]
    if message_link and message_link.startswith("https://"):
        lines.extend(["", f'<a href="{escape_html(message_link)}">🔗 Ссылка на сообщение</a>'])
    return "\n".join(lines)


async def send_vacancy_card(
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """HTML-карточка вакансии с fallback без разметки."""
    try:
        await send_message_with_retry(
            chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as e:
        if "parse" in str(e).lower():
            plain = re.sub(r"<[^>]*>", "", text)
            await send_message_with_retry(
                chat_id,
                text=plain,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return
        raise


def category_picker_text(selected_count: int, user_id: int, hint: str = "") -> str:
    if is_user_premium(user_id):
        limit_line = f"💎 Premium: без лимита. Выбрано: *{selected_count}*."
    else:
        limit_line = (
            f"🆓 Free: до *{FREE_CATEGORY_LIMIT}* категорий (push — только Premium).\n"
            f"Выбрано: *{selected_count}*."
        )
    hint_line = f"\n\n{hint}" if hint else ""
    return (
        "⚙️ *Настройки — категории вакансий*\n\n"
        "✅ — выбраны · ⬜ — добавить\n"
        f"{limit_line}{hint_line}\n\n"
        "Готово — «✅ Завершить выбор»"
    )


def build_categories_markup(selected_codes: list, user_id: int) -> InlineKeyboardMarkup:
    all_cats = get_all_categories()
    buttons, row = [], []
    for i, cat in enumerate(all_cats):
        prefix = "✅" if cat["code"] in selected_codes else "⬜"
        row.append(InlineKeyboardButton(
            text=f"{prefix} {cat['emoji']} {cat['name']}",
            callback_data=f"cat_{cat['code']}",
        ))
        if len(row) == 2 or i == len(all_cats) - 1:
            buttons.append(row)
            row = []
    if not is_user_premium(user_id):
        buttons.append([InlineKeyboardButton(
            text="💎 Premium — больше категорий и push",
            callback_data="subscription_from_categories",
        )])
    buttons.append([InlineKeyboardButton(text="🔕 Отключить рассылку", callback_data="disable_feed")])
    buttons.append([InlineKeyboardButton(text="✅ Завершить выбор", callback_data="finish_categories")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def edit_category_picker(message: types.Message, selected_codes: list, user_id: int, hint: str = ""):
    try:
        await message.edit_text(
            category_picker_text(len(selected_codes), user_id, hint),
            parse_mode="Markdown",
            reply_markup=build_categories_markup(selected_codes, user_id),
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"edit_category_picker: {e}")


async def send_category_picker(chat_id: int, user_id: int, selected_codes: list = None):
    if selected_codes is None:
        selected_codes = [c["code"] for c in get_user_categories(user_id)]
    return await bot.send_message(
        chat_id,
        category_picker_text(len(selected_codes), user_id),
        parse_mode="Markdown",
        reply_markup=build_categories_markup(selected_codes, user_id),
    )


def subscription_action_buttons(user_id: int) -> InlineKeyboardMarkup | None:
    buttons = []
    premium = is_user_premium(user_id)
    if SUBSCRIPTION_PAY_URL:
        label = "💳 Продлить Premium" if premium else "💳 Оплатить Premium"
        buttons.append([InlineKeyboardButton(text=label, url=SUBSCRIPTION_PAY_URL)])
    if premium:
        buttons.append([
            InlineKeyboardButton(
                text="📩 Запросить продление (перевод)",
                callback_data="subscription_renew",
            )
        ])
    else:
        buttons.append([
            InlineKeyboardButton(
                text="📩 Запросить Premium (перевод)",
                callback_data="subscription_request",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


def premium_request_admin_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"✅ Активировать {PREMIUM_DEFAULT_DAYS} дн.",
                callback_data=f"pr_a_{request_id}",
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"pr_r_{request_id}",
            ),
        ],
    ])


def format_premium_request_admin_caption(req: dict) -> str:
    title = "💳 *Запрос продления Premium*" if req.get("is_renewal") else "💳 *Запрос Premium*"
    pending = count_pending_premium_requests()
    return (
        f"{title} #{req['id']} (в очереди: {pending})\n\n"
        f"👤 {escape_markdown(req.get('full_name') or '—')}\n"
        f"ID: `{req['user_id']}`\n"
        f"Username: @{req.get('username') or '—'}\n"
        f"📞 {escape_markdown(str(req.get('phone') or '—'))}\n"
        f"📋 {escape_markdown(str(req.get('category_codes') or '—'))}\n"
        f"🕐 {req.get('created_at') or '—'}"
    )


async def send_admin_premium_request_alert(request_id: int):
    if not YOUR_USER_ID:
        return
    req = get_premium_request(request_id)
    if not req or req.get("status") != "pending":
        return
    caption = format_premium_request_admin_caption(req)
    markup = premium_request_admin_keyboard(request_id)
    file_id = req.get("receipt_file_id")
    kind = req.get("receipt_kind")
    try:
        if file_id and kind == "photo":
            await bot.send_photo(
                YOUR_USER_ID, file_id, caption=caption,
                parse_mode="Markdown", reply_markup=markup,
            )
        elif file_id and kind == "document":
            await bot.send_document(
                YOUR_USER_ID, file_id, caption=caption,
                parse_mode="Markdown", reply_markup=markup,
            )
        else:
            await bot.send_message(
                YOUR_USER_ID, caption, parse_mode="Markdown", reply_markup=markup,
            )
    except Exception as e:
        logger.warning(f"premium_request notify admin: {e}")


async def activate_premium_for_user(target_id: int, days: int) -> bool:
    set_user_plan(target_id, plan="premium", days=days, extend=True)
    resolve_premium_requests(target_id)
    profile = get_subscriber_profile(target_id)
    paid_until = format_db_date_short(profile.get("paid_until")) if profile else ""
    until_line = f"\nДействует до: *{paid_until}*" if paid_until else ""
    try:
        await bot.send_message(
            target_id,
            f"💎 *Premium активирован* на {days} дн.{until_line}\n\n"
            "• моментальные push-уведомления\n"
            "• все категории без лимита\n"
            "• фильтр по метро (📍 Мои районы)\n\n"
            "Категории — кнопка «⚙️ Настройки».",
            parse_mode="Markdown",
        )
        return True
    except Exception as e:
        logger.warning(f"Не удалось уведомить {target_id} о Premium: {e}")
        return False


async def safe_callback_answer(
    callback: types.CallbackQuery,
    text: str | None = None,
    show_alert: bool = False,
) -> bool:
    """answer_callback_query; не падаем на просроченной кнопке (старая клавиатура после деплоя)."""
    try:
        await callback.answer(text=text, show_alert=show_alert)
        return True
    except TelegramBadRequest as e:
        err = str(e).lower()
        if any(
            x in err
            for x in ("query is too old", "query id is invalid", "response timeout expired")
        ):
            logger.debug(
                "Просроченный callback %r от user %s",
                callback.data,
                callback.from_user.id,
            )
            return False
        raise


async def send_message_with_retry(user_id: int, **kwargs):
    """Отправка с одним повтором при FloodWait."""
    for attempt in range(2):
        try:
            return await bot.send_message(user_id, **kwargs)
        except TelegramRetryAfter as e:
            if attempt == 0:
                wait = int(e.retry_after) + 1
                logger.warning(f"FloodWait {wait}s для {user_id}, повтор...")
                await asyncio.sleep(wait)
                continue
            raise
        except Exception as e:
            err = str(e).lower()
            if attempt == 0 and "retry after" in err:
                wait = 3
                if "retry after" in err:
                    import re as _re
                    m = _re.search(r"retry after (\d+)", err)
                    if m:
                        wait = int(m.group(1)) + 1
                logger.warning(f"FloodWait ~{wait}s для {user_id}, повтор...")
                await asyncio.sleep(wait)
                continue
            raise

async def send_long_message(chat_id: int, text: str, parse_mode: str = "Markdown", chunk_size: int = 3800):
    """Разбивает длинный текст на части (лимит Telegram ~4096)."""
    if len(text) <= chunk_size:
        await bot.send_message(chat_id, text, parse_mode=parse_mode)
        return
    start = 0
    while start < len(text):
        await bot.send_message(chat_id, text[start:start + chunk_size], parse_mode=parse_mode)
        start += chunk_size
        await asyncio.sleep(0.2)

def build_admin_dashboard_text() -> str:
    stats = get_admin_stats()
    parser = get_parser_status_snapshot()
    premium = count_premium_subscribers()
    return (
        f"👑 *Панель администратора*\n\n"
        f"📡 *Парсер*\n{format_parser_status_line(parser)}\n\n"
        f"📊 *Статистика*\n"
        f"• Подписчиков: {stats['subscribers']} (💎 premium: {premium})\n"
        f"• Полных профилей: {stats['full_profiles']}\n"
        f"• Откликов: {stats['responses']}\n"
        f"• Вакансий в очереди: {stats['pending_vacancies']}\n"
        f"• Всего вакансий: {stats['total_vacancies']}\n"
        f"• ⚠️ Жалоб: {stats['pending_complaints']}\n"
        f"• ❓ Поддержка: {stats['pending_support']}\n"
        f"• 💳 Ожидают Premium: {count_pending_premium_requests()}"
    )

async def run_broadcast(admin_chat_id: int, text: str, status_msg: types.Message = None):
    subscribers = get_all_subscribers()
    if not subscribers:
        return 0, 0, "Нет подписчиков."
    if not status_msg:
        status_msg = await bot.send_message(admin_chat_id, f"📢 Рассылка {len(subscribers)} подписчикам...")
    sent, failed = 0, 0
    body = f"📢 *Рассылка от администратора:*\n\n{text}"
    for i, uid in enumerate(subscribers, 1):
        try:
            await send_message_with_retry(uid, text=body, parse_mode="Markdown")
            sent += 1
        except Exception as e:
            failed += 1
            if "bot was blocked" in str(e).lower():
                logger.info(f"Рассылка: {uid} заблокировал бота")
            else:
                logger.warning(f"Рассылка: ошибка {uid}: {e}")
        if i % 25 == 0 or i == len(subscribers):
            try:
                await status_msg.edit_text(
                    f"📢 Рассылка… {i}/{len(subscribers)}\n✅ {sent} | ❌ {failed}"
                )
            except Exception:
                pass
        await asyncio.sleep(BROADCAST_DELAY)
    return sent, failed, None

def format_subscription_screen(user_id: int) -> str:
    premium = is_user_premium(user_id)
    profile = get_subscriber_profile(user_id)
    paid_until = profile.get("paid_until") if profile else None
    trial_used = profile.get("trial_used") if profile else False
    pay_heading = "<b>Продление:</b>" if premium else "<b>Оплата (вручную):</b>"
    if premium:
        until_str = escape_html(format_db_date_short(paid_until))
        until_line = f"Действует до: <b>{until_str}</b>" if paid_until else "Без ограничения по сроку"
        status = f"💎 <b>Premium активен</b>\n{until_line}"
        until_dt = _coerce_db_datetime(paid_until)
        if until_dt:
            days_left = (until_dt - datetime.now(timezone.utc)).days
            if days_left <= PREMIUM_RENEWAL_WARN_DAYS:
                status += f"\n⏳ Осталось <b>{max(days_left, 0)}</b> дн. — продлите кнопками ниже."
    else:
        status = (
            "🆓 <b>Бесплатный доступ</b>\n"
            "Лента «🔍 Посмотреть новые вакансии» — без моментальных push"
        )
    pay_lines = []
    if SUBSCRIPTION_CARD_HINT:
        pay_lines.append(f"💳 <b>Реквизиты:</b> {escape_html(SUBSCRIPTION_CARD_HINT)}")
    pay_lines.append(f"💰 <b>Сумма:</b> {escape_html(SUBSCRIPTION_PRICE_RUB)} ₽/мес")
    pay_lines.append(f"В комментарии к переводу укажите ваш Telegram ID: <code>{user_id}</code>")
    if SUBSCRIPTION_PAY_URL:
        pay_lines.append(f"Или оплатите по ссылке:\n{escape_html(SUBSCRIPTION_PAY_URL)}")
    else:
        action = escape_html("продления" if premium else "Premium")
        pay_lines.append(
            f"После перевода напишите {escape_html(SUBSCRIPTION_SUPPORT)} "
            f"или нажмите «Запросить {action}» ниже."
        )
    pay_block = "\n".join(pay_lines)
    trial_hint = ""
    if not trial_used and TRIAL_DAYS > 0 and not premium:
        trial_hint = (
            f"\n\n🎁 Новым пользователям — пробный Premium "
            f"<b>{TRIAL_DAYS} дн.</b> после выбора категорий."
        )
    return (
        f"💎 <b>Подписка Promostaff Hunter</b>\n\n"
        f"{status}\n\n"
        f"<b>Premium даёт:</b>\n"
        f"• моментальные push-уведомления\n"
        f"• все категории без лимита\n"
        f"• фильтр по метро/району (📍 Мои районы)\n\n"
        f"<b>Free:</b> до {FREE_CATEGORY_LIMIT} категорий, только лента без push\n\n"
        f"{pay_heading}\n{pay_block}{trial_hint}"
    )


async def send_subscription_screen(
    message: types.Message,
    user_id: int,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    edit: bool = False,
):
    """Экран подписки: HTML + fallback без разметки (env с _, &, < не ломают Telegram)."""
    text = format_subscription_screen(user_id)
    try:
        if edit:
            await message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await message.answer(text, parse_mode="HTML", reply_markup=reply_markup)
    except TelegramBadRequest as e:
        err = str(e).lower()
        if edit and "message is not modified" in err:
            return
        if "parse" in err and "entit" in err:
            logger.warning("subscription screen HTML rejected, plain fallback: %s", e)
            plain = re.sub(r"<[^>]*>", "", text)
            if edit:
                await message.edit_text(plain, reply_markup=reply_markup)
            else:
                await message.answer(plain, reply_markup=reply_markup)
            return
        raise


async def notify_admin_parser_issue(text: str):
    if not YOUR_USER_ID:
        return
    try:
        await bot.send_message(YOUR_USER_ID, text)
    except Exception as e:
        logger.warning(f"Не удалось отправить алерт админу: {e}")

def get_postvacancy_categories_keyboard():
    categories = get_all_categories()
    buttons, row = [], []
    for i, cat in enumerate(categories):
        row.append(InlineKeyboardButton(
            text=f"{cat['emoji']} {cat['name']}", callback_data=f"postcat_{cat['code']}"
        ))
        if len(row) == 2 or i == len(categories) - 1:
            buttons.append(row)
            row = []
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Пагинация
user_pages = {}
user_response_pages = {}

# Состояния FSM
class RegistrationState(StatesGroup):
    waiting_for_name = State()
    waiting_for_birthdate = State()
    waiting_for_phone = State()
    waiting_for_photo = State()

class BroadcastState(StatesGroup):
    waiting_for_text = State()

class ComplaintState(StatesGroup):
    waiting_for_reason = State()
    waiting_for_text = State()

class SupportState(StatesGroup):
    waiting_for_question = State()

class AddChatState(StatesGroup):
    waiting_for_link = State()

class PostVacancyState(StatesGroup):
    waiting_for_category = State()
    waiting_for_text = State()
    waiting_for_photo = State()

class MetroState(StatesGroup):
    waiting_for_zones = State()

class RespondWithPhotoState(StatesGroup):
    waiting_for_photo = State()

class ResponseDraftState(StatesGroup):
    waiting_for_comment = State()

class PremiumPaymentState(StatesGroup):
    waiting_for_receipt = State()

class ProfileEditState(StatesGroup):
    waiting_for_name = State()
    waiting_for_birthdate = State()
    waiting_for_phone = State()
    waiting_for_photo = State()
    waiting_for_extra = State()

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def get_category_emoji(category_code: str) -> str:
    emojis = {
        "promoter": "📢", "hostess": "👩‍💼", "wardrobe": "🧥", "animator": "🎭",
        "helper": "👷", "loader": "📦", "waiter": "🍽️", "driver": "🚐",
        "security": "🛡️", "parking": "🚗", "supervisor": "👨‍💼"
    }
    return emojis.get(category_code, "📌")


def get_category_name(category_code: str) -> str:
    names = {
        "promoter": "Промоутер", "hostess": "Хостес", "wardrobe": "Гардеробщик",
        "animator": "Аниматор", "helper": "Хелпер", "loader": "Грузчик",
        "waiter": "Официант", "driver": "Водитель", "security": "Охранник",
        "parking": "Парковщик", "supervisor": "Супервайзер",
    }
    return names.get(category_code, category_code)


def build_user_help_html(user_id: int) -> str:
    """Инструкция для исполнителя — по шагам, как в Time Bot."""
    profile = get_subscriber_profile(user_id)
    premium = is_user_premium(user_id)
    trial_used = bool(profile and profile.get("trial_used"))

    if premium:
        push_block = (
            "У вас <b>Premium</b>: новые вакансии по выбранным категориям "
            "приходят <b>push-сообщениями</b> в этот чат."
        )
    else:
        push_block = (
            f"На <b>Free</b> push нет — открывайте ленту кнопкой "
            f"«🔍 Посмотреть новые вакансии». Premium ({escape_html(SUBSCRIPTION_PRICE_RUB)} ₽/мес) — "
            "мгновенные уведомления."
        )

    trial_block = ""
    if not premium and not trial_used and TRIAL_DAYS > 0:
        trial_block = (
            f"\n\n🎁 После «✅ Завершить выбор» категорий — пробный Premium "
            f"<b>{TRIAL_DAYS} дн.</b> (push + метро)."
        )

    return (
        "<b>📖 Как пользоваться ботом</b>\n\n"
        "PromoStaff Hunter собирает вакансии из Telegram-каналов "
        "и показывает только те роли, которые вы выбрали.\n\n"
        "<b>1. Настройки</b>\n"
        f"Кнопка «⚙️ Настройки» — категории вакансий "
        f"(хелпер, промо, грузчик…). Free: до {FREE_CATEGORY_LIMIT}, Premium: без лимита.\n"
        "Нажмите «✅ Завершить выбор». Отключить рассылку — «🔕 Отключить рассылку».\n\n"
        "<b>2. Смотреть вакансии</b>\n"
        "«🔍 Посмотреть новые вакансии» — лента по вашим категориям. "
        "Можно выбрать одну категорию или «Все».\n\n"
        "<b>3. Push-уведомления</b>\n"
        f"{push_block}{trial_block}\n\n"
        "<b>4. Отклик на вакансию</b>\n"
        "На карточке — «✋ Откликнуться». Бот отправит заказчику вашу анкету "
        "(ФИО, возраст, телефон, фото если добавляли).\n\n"
        "<b>5. Мои отклики</b>\n"
        "«📨 Мои отклики» — история, статус вакансии, ссылка на пост.\n\n"
        "<b>6. Районы (Premium)</b>\n"
        "«📍 Мои районы» — станции метро; push и лента только по ним "
        "(если метро в вакансии не указано — не отсекаем).\n\n"
        "<b>7. Подписка</b>\n"
        "«💎 Подписка» — тариф, продление, оплата по реквизитам.\n\n"
        "<b>8. Мои данные</b>\n"
        "«👤 Мои данные» — ФИО, телефон, фото и доп. информация для откликов. Можно редактировать.\n\n"
        "<b>Список команд</b>\n"
        "/help — эта инструкция\n"
        "/start — главное меню\n\n"
        "Вопрос или ошибка — кнопка «❓ Поддержка».\n\n"
        "<b>Начните с «⚙️ Настройки», затем откройте «🔍 Посмотреть новые вакансии»!</b>"
    )


def build_admin_help_html() -> str:
    return (
        "<b>📖 Админ: как пользоваться</b>\n\n"
        "<b>Парсер</b>\n"
        "• «🔍 Ручная проверка» или /check_now — прогон всех чатов\n"
        "• «📝 Отчёт парсера» — статистика последнего прогона\n"
        "• «📋 Список чатов парсинга» — доступ и мониторинг\n\n"
        "<b>Пользователи</b>\n"
        "• «💎 Запросы Premium» — чек + ✅/❌\n"
        "• /setplan USER_ID premium 30 — выдать Premium\n\n"
        "<b>Команды</b>\n"
        "/help — эта справка\n"
        "/start — админ-меню\n"
        "/debug_last — отчёт парсера"
    )


async def send_user_help(message: types.Message, user_id: int):
    text = build_user_help_html(user_id)
    try:
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    except TelegramBadRequest:
        await message.answer(re.sub(r"<[^>]*>", "", text), disable_web_page_preview=True)

def calculate_age(birth_date_str: str) -> int:
    try:
        birth_date = datetime.strptime(birth_date_str, "%d.%m.%Y")
        today = datetime.now()
        age = today.year - birth_date.year
        if (today.month, today.day) < (birth_date.month, birth_date.day):
            age -= 1
        return age
    except ValueError:
        return None

def format_user_card(card: dict, idx: int = None) -> str:
    prefix = f"{idx}. " if idx is not None else ""
    name = card.get("full_name") or "—"
    username = f"@{card['username']}" if card.get("username") else "нет"
    cats = ", ".join(card.get("categories") or []) or "не выбраны"
    status = "активен" if card.get("is_active") else "неактивен"
    if card.get("is_premium"):
        until = format_db_date_short(card.get("paid_until"))
        plan_line = f"💎 Premium до {until}" if until else "💎 Premium"
    elif (card.get("plan") or "free") == "premium":
        plan_line = "💎 Premium (истёк)"
    else:
        plan_line = "🆓 Free"
    return (
        f"{prefix}👤 *{escape_markdown(name)}*\n"
        f"ID: `{card['user_id']}`\n"
        f"Username: {escape_markdown(username)}\n"
        f"Телефон: {escape_markdown(card.get('phone') or '—')}\n"
        f"Тариф: {escape_markdown(plan_line)}\n"
        f"Статус: {escape_markdown(status)}\n"
        f"Категории: {escape_markdown(cats)}"
    )

def _coerce_db_datetime(raw_dt):
    """SQLite отдаёт str, PostgreSQL — datetime; приводим к aware UTC."""
    if raw_dt is None:
        return None
    if isinstance(raw_dt, datetime):
        if raw_dt.tzinfo is None:
            return raw_dt.replace(tzinfo=timezone.utc)
        return raw_dt.astimezone(timezone.utc)
    if isinstance(raw_dt, date):
        return datetime.combine(raw_dt, datetime.min.time()).replace(tzinfo=timezone.utc)
    if isinstance(raw_dt, str):
        s = raw_dt.strip()
        if not s:
            return None
        for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
            try:
                return datetime.strptime(s[:size], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def format_db_date_short(raw_dt) -> str:
    dt = _coerce_db_datetime(raw_dt)
    if dt is None:
        return str(raw_dt)[:10] if raw_dt else ""
    return dt.strftime("%Y-%m-%d")


def format_db_datetime_short(raw_dt) -> str:
    dt = _coerce_db_datetime(raw_dt)
    if dt is None:
        return str(raw_dt)[:16] if raw_dt else "—"
    return dt.strftime("%Y-%m-%d %H:%M")


def format_publication_time(raw_dt) -> str:
    """Время поста в канале (Telethon UTC) → отображение в МСК."""
    dt = _coerce_db_datetime(raw_dt)
    if dt is None:
        return "сейчас"
    return dt.astimezone(MSK_TZ).strftime("%d.%m.%Y %H:%M МСК")


def get_freshness_label(raw_dt) -> str:
    published = _coerce_db_datetime(raw_dt)
    if published is None:
        return "🟢 Актуальна"
    try:
        delta_minutes = (datetime.now(timezone.utc) - published).total_seconds() / 60
        if delta_minutes <= 30:
            return "🟢 Актуальна: только что"
        if delta_minutes <= 180:
            return "🟢 Актуальна: сегодня"
        return "🟡 Актуальна: ранее сегодня"
    except Exception:
        return "🟢 Актуальна"

def normalize_chat_link(raw: str) -> str:
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

def extract_required_fields_from_vacancy(vacancy_text: str) -> list:
    if not vacancy_text:
        return []
    txt = vacancy_text.lower()
    required = []
    checks = [
        ("Возраст", ["возраст", "лет"]),
        ("Гражданство", ["гражданств", "рф", "снг"]),
        ("Опыт", ["опыт", "стаж"]),
        ("Размер одежды", ["размер одежды", "размер"]),
        ("Паспорт", ["паспорт"]),
        ("Медкнижка", ["медкниж", "мед книж"]),
        ("Самозанятость", ["самозанят", "самозанятость"]),
    ]
    for label, keys in checks:
        if any(k in txt for k in keys):
            required.append(label)
    return required

def build_candidate_profile_text(profile: dict, extra_comment: str = None) -> str:
    lines = [
        "Здравствуйте! Откликаюсь на вашу вакансию.",
        "",
        "Анкета кандидата:",
        f"• ФИО: {profile.get('full_name') or '—'}",
        f"• Возраст: {profile.get('age') or '—'}",
        f"• Телефон: {profile.get('phone') or '—'}",
        f"• Telegram: @{profile.get('username') if profile.get('username') else 'нет'}",
    ]
    if profile.get("resume_extra"):
        lines.append(f"• О себе: {profile['resume_extra'].strip()}")
    if extra_comment:
        lines.extend(["", "Дополнительно:", extra_comment.strip()])
    return "\n".join(lines).strip()


def profile_data_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ ФИО", callback_data="profile_edit_name"),
            InlineKeyboardButton(text="📞 Телефон", callback_data="profile_edit_phone"),
        ],
        [
            InlineKeyboardButton(text="🎂 Возраст", callback_data="profile_edit_age"),
            InlineKeyboardButton(text="📷 Фото", callback_data="profile_edit_photo"),
        ],
        [InlineKeyboardButton(text="📝 Доп. информация", callback_data="profile_edit_extra")],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="profile_back_menu")],
    ])


def build_profile_data_text(profile: dict) -> str:
    photo_status = "нет"
    if profile.get("photo_storage_path") or profile.get("photo_file_id"):
        photo_status = "есть"
    extra = (profile.get("resume_extra") or "").strip()
    extra_block = f"\n📝 *Доп. информация:*\n{escape_markdown(extra)}\n" if extra else "\n📝 *Доп. информация:* не заполнена\n"
    birth = f"\n🎂 *Дата рождения:* {escape_markdown(profile['birth_date'])}" if profile.get("birth_date") else ""
    return (
        "👤 *Мои данные*\n\n"
        "Эти данные уходят заказчику при отклике на вакансию.\n\n"
        f"• ФИО: {escape_markdown(profile.get('full_name') or '—')}"
        f"{birth}\n"
        f"• Возраст: {profile.get('age') or '—'} лет\n"
        f"• Телефон: {escape_markdown(profile.get('phone') or '—')}\n"
        f"• Фото: {photo_status}"
        f"{extra_block}\n"
        "Что изменить?"
    )


async def send_profile_data_screen(chat_id: int, user_id: int):
    profile = get_subscriber_profile(user_id)
    if not profile or not profile.get("full_name") or not profile.get("phone"):
        await bot.send_message(
            chat_id,
            "⚠️ Профиль не заполнен. Нажмите /start для регистрации.",
        )
        return
    text = build_profile_data_text(profile)
    if profile.get("photo_storage_path") or profile.get("photo_file_id"):
        try:
            await send_profile_photo(
                bot,
                chat_id,
                profile,
                caption=text,
                parse_mode="Markdown",
                reply_markup=profile_data_markup(),
            )
            return
        except Exception as e:
            logger.warning("send_profile_data_screen photo: %s", e)
    await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=profile_data_markup())


async def finish_profile_field_edit(message: types.Message, state: FSMContext, user_id: int, notice: str):
    questionnaire = rebuild_candidate_questionnaire(user_id)
    update_candidate_questionnaire(user_id, questionnaire)
    keyboard, _ = get_main_keyboard(user_id)
    await state.clear()
    await message.answer(notice, parse_mode="Markdown", reply_markup=keyboard)
    await send_profile_data_screen(message.chat.id, user_id)

def build_contact_link(contact: str, text: str) -> str:
    if not contact:
        return None
    contact = contact.strip()
    if contact.startswith("@"):
        username = contact[1:]
        return f"https://t.me/{username}?text={quote(text)}"
    digits = re.sub(r"\D", "", contact)
    if digits:
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if len(digits) == 11 and digits.startswith("7"):
            return f"tel:+{digits}"
    return None

# ========== РАССЫЛКА ВАКАНСИЙ ПОДПИСЧИКАМ (без глобальных счётчиков) ==========

async def dispatch_vacancy_push(order: dict):
    """Очередь push: не блокирует Telethon-парсер на время рассылки."""
    async with _vacancy_push_sem:
        await send_vacancy_to_subscribers(order)


def schedule_vacancy_push(order: dict):
    spawn_background_task(dispatch_vacancy_push(order))


async def send_vacancy_to_subscribers(order: dict):
    category_code = order.get('category', detect_category(order['message_text']))
    dedupe_key = order.get("dedupe_key")
    vacancy_id = order.get("vacancy_id") or make_vacancy_id(
        order.get('chat_id', ''), order.get('message_id', ''), dedupe_key=dedupe_key
    )
    subscribers = get_subscribers_by_category(category_code)
    if not subscribers:
        logger.info(f"Нет подписчиков на категорию {category_code}")
        return

    published_raw = order.get("published_at")
    published_at = format_publication_time(published_raw)
    freshness = get_freshness_label(published_raw)
    cat_name = get_category_name(category_code)
    text = format_vacancy_card_html(
        category_emoji=get_category_emoji(category_code),
        category_name=cat_name,
        freshness=freshness,
        published_at=published_at,
        body=order.get("message_text", ""),
        source=order.get("chat_title") or "—",
        message_link=order.get("message_link"),
    )

    address = order.get('address') or extract_address_from_text(order.get('message_text', ''))
    keyboard = build_vacancy_keyboard(vacancy_id, address)

    sent_count = 0
    skipped_free = 0
    skipped_metro = 0
    for subscriber in subscribers:
        if not is_user_premium(subscriber['user_id']):
            skipped_free += 1
            continue
        if has_user_received_vacancy(subscriber['user_id'], vacancy_id):
            continue
        if not vacancy_matches_user_metro(
            order.get('message_text', ''),
            address,
            subscriber.get('metro_zones'),
        ):
            skipped_metro += 1
            continue
        try:
            await send_vacancy_card(subscriber['user_id'], text, reply_markup=keyboard)
            mark_vacancy_sent_to_user(vacancy_id, subscriber['user_id'])
            sent_count += 1
            await asyncio.sleep(SEND_DELAY)  # небольшая пауза, чтобы не флудить
        except Exception as e:
            if "bot was blocked by the user" in str(e):
                logger.info(f"Пользователь {subscriber['user_id']} заблокировал бота")
            else:
                logger.error(f"Ошибка отправки {subscriber['user_id']}: {e}")

    logger.info(
        f"Вакансия {vacancy_id} (категория {category_code}): push {sent_count}, "
        f"free skip {skipped_free}, metro skip {skipped_metro}"
    )
    if sent_count > 0:
        mark_vacancy_sent(vacancy_id)

# ========== УВЕДОМЛЕНИЕ О ЗАКРЫТИИ ВАКАНСИЙ ==========

async def notify_closed_vacancies(closed_data: list):
    for vacancy_id, user_ids in closed_data:
        if not vacancy_id or not user_ids:
            continue
        for uid in user_ids:
            try:
                await bot.send_message(uid, f"🔒 *Вакансия, на которую вы откликались или получали, больше не актуальна (закрыта).*\nID вакансии: `{vacancy_id}`", parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Не удалось уведомить пользователя {uid}: {e}")

# ========== КЛАВИАТУРЫ ==========

def get_main_keyboard(user_id: int):
    categories = get_user_categories(user_id)
    if categories:
        categories_text = ", ".join([f"{c['emoji']}{c['name']}" for c in categories])
        status_text = f"📌 Ваши категории: {categories_text}"
    else:
        status_text = "⚠️ Вы ещё не выбрали категории вакансий"
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Посмотреть новые вакансии")],
            [KeyboardButton(text="📨 Мои отклики"), KeyboardButton(text=BTN_SETTINGS)],
            [KeyboardButton(text="📍 Мои районы"), KeyboardButton(text="💎 Подписка")],
            [KeyboardButton(text=BTN_MY_DATA)],
            [KeyboardButton(text="📖 Как пользоваться"), KeyboardButton(text="❓ Поддержка")],
        ],
        resize_keyboard=True
    )
    return keyboard, status_text

def get_admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="🔍 Ручная проверка")],
            [KeyboardButton(text="📋 Список откликов"), KeyboardButton(text="📝 Отчёт парсера")],
            [KeyboardButton(text="👥 Список подписчиков"), KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="🗂️ Карточки пользователей"), KeyboardButton(text="💎 Запросы Premium")],
            [KeyboardButton(text="🧭 Маппинг категорий"), KeyboardButton(text="⚠️ Жалобы")],
            [KeyboardButton(text="❓ Поддержка (админ)"), KeyboardButton(text="➕ Добавить чат")],
            [KeyboardButton(text="📋 Список чатов парсинга"), KeyboardButton(text="📤 Отправить вакансию")],
            [KeyboardButton(text="📖 Как пользоваться")],
            [KeyboardButton(text="❌ Закрыть меню")]
        ],
        resize_keyboard=True
    )

ADMIN_MENU_BUTTONS = {
    "📊 Статистика", "🔍 Ручная проверка", "📋 Список откликов", "📝 Отчёт парсера",
    "👥 Список подписчиков", "📢 Рассылка", "🗂️ Карточки пользователей", "💎 Запросы Premium",
    "🧭 Маппинг категорий", "⚠️ Жалобы", "❓ Поддержка (админ)", "➕ Добавить чат",
    "📋 Список чатов парсинга", "💬 Чаты парсинга", "📤 Отправить вакансию",
    "📖 Как пользоваться", "❌ Закрыть меню",
}

# ========== КОМАНДЫ И ОБРАБОТЧИКИ ==========

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    user_id = message.from_user.id
    if user_id == YOUR_USER_ID:
        await message.answer(build_admin_help_html(), parse_mode="HTML")
        return
    await send_user_help(message, user_id)


@dp.message(lambda m: m.text == "📖 Как пользоваться")
async def help_menu_button(message: types.Message):
    if message.from_user.id == YOUR_USER_ID:
        await message.answer(build_admin_help_html(), parse_mode="HTML")
        return
    await send_user_help(message, message.from_user.id)


@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name

    if user_id == YOUR_USER_ID:
        await message.answer(
            f"👋 Здравствуйте, Администратор {first_name}!\n\n"
            f"📊 *Бот работает в штатном режиме.*\n"
            f"Сборка: `{APP_BUILD}`\n\n"
            f"Справка — кнопка «📖 Как пользоваться» или /help.\n"
            f"Используйте кнопки для управления:",
            parse_mode="Markdown",
            reply_markup=get_admin_keyboard()
        )
        return

    add_subscriber(user_id, username, first_name, last_name)
    expired_msg = downgrade_expired_premium(user_id)
    if expired_msg:
        await message.answer(expired_msg, parse_mode="Markdown")
    profile = get_subscriber_profile(user_id)
    if profile and profile.get("full_name") and profile.get("phone"):
        categories = get_user_categories(user_id)
        if categories:
            keyboard, status_text = get_main_keyboard(user_id)
            await message.answer(
                f"👋 С возвращением, {first_name}!\n\n{status_text}\n\n"
                f"Используйте кнопки меню. Инструкция — «📖 Как пользоваться» или /help",
                reply_markup=keyboard
            )
            return
        else:
            await send_category_picker(message.chat.id, user_id)
            return

    await message.answer(
        "👋 *Добро пожаловать в бот поиска работы!*\n\n"
        "Я помогу вам найти подходящие вакансии.\n\n"
        "📝 *Давайте заполним ваш профиль*\n\n"
        "Как вас зовут? (ФИО полностью)\n\nПример: *Иван Петров*",
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationState.waiting_for_name)


@dp.message(RegistrationState.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    full_name = message.text.strip()
    if len(full_name.split()) < 2:
        await message.answer("❌ Пожалуйста, введите полное имя и фамилию (минимум 2 слова).")
        return
    if not re.match(r'^[a-zA-Zа-яА-ЯёЁ\s\-\.]+$', full_name):
        await message.answer("❌ Имя может содержать только буквы, пробелы, дефисы и точки.")
        return
    await state.update_data(full_name=full_name)
    await message.answer(
        "🎂 *Дата рождения*\n\n"
        "Введите вашу дату рождения в формате: **ДД.ММ.ГГГГ**\n\n"
        "Пример: `25.12.1990`\n\n"
        "Я автоматически рассчитаю ваш возраст.",
        parse_mode="Markdown"
    )
    await state.set_state(RegistrationState.waiting_for_birthdate)


@dp.message(RegistrationState.waiting_for_birthdate)
async def process_birthdate(message: types.Message, state: FSMContext):
    birth_date_str = message.text.strip()
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', birth_date_str):
        await message.answer(
            "❌ Неверный формат!\n\n"
            "Пожалуйста, введите дату в формате: **ДД.ММ.ГГГГ**\n\n"
            "Пример: `25.12.1990`",
            parse_mode="Markdown"
        )
        return
    age = calculate_age(birth_date_str)
    if age is None:
        await message.answer(
            "❌ Неверная дата! Проверьте, что:\n"
            "- День от 1 до 31\n"
            "- Месяц от 1 до 12\n"
            "- Год реальный\n\n"
            "Пример: `25.12.1990`",
            parse_mode="Markdown"
        )
        return
    if age < 16 or age > 100:
        await message.answer(f"❌ Возраст должен быть от 16 до 100 лет. Ваш возраст: {age}.", parse_mode="Markdown")
        return
    await state.update_data(birth_date=birth_date_str, age=age)
    
    phone_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer(
        f"✅ Возраст: {age} лет\n\n"
        "📞 *Контактный телефон*\n\n"
        "Нажмите на кнопку ниже, чтобы отправить ваш номер телефона.\n"
        "Он будет передан работодателю при отклике на вакансию.",
        parse_mode="Markdown",
        reply_markup=phone_keyboard
    )
    await state.set_state(RegistrationState.waiting_for_phone)


@dp.message(RegistrationState.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if message.contact:
        phone = message.contact.phone_number
    else:
        phone = message.text.strip()
        digits_only = re.sub(r'\D', '', phone)
        if len(digits_only) < 10 or len(digits_only) > 15:
            await message.answer(
                "❌ Пожалуйста, введите корректный номер телефона.\n\n"
                "Примеры: +7 999 123-45-67 или 89991234567\n\n"
                "Или нажмите кнопку «Отправить мой номер телефона»",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="📱 Отправить мой номер телефона", request_contact=True)]],
                    resize_keyboard=True,
                    one_time_keyboard=True
                )
            )
            return

    data = await state.get_data()
    await state.update_data(phone=phone)   # <-- СОХРАНЯЕМ ТЕЛЕФОН В СОСТОЯНИЕ
    update_subscriber_profile(
        user_id, data['full_name'], data['age'], phone, birth_date=data['birth_date'],
    )
    update_candidate_questionnaire(user_id, rebuild_candidate_questionnaire(user_id))
    
    # Запрос фото (необязательно)
    await message.answer(
        "📸 *Фото для отклика*\n\n"
        "При отклике на вакансию вы можете приложить своё фото.\n"
        "Это повышает шансы на положительный ответ.\n\n"
        "Отправьте фото сейчас или нажмите «Пропустить»",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⏩ Пропустить")]],
            resize_keyboard=True
        )
    )
    await state.set_state(RegistrationState.waiting_for_photo)


@dp.message(RegistrationState.waiting_for_photo)
async def process_photo(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if message.text == "⏩ Пропустить":
        photo_file_id = None
    elif message.photo:
        photo_file_id = message.photo[-1].file_id
        storage_path, photo_file_id = await persist_user_photo(bot, user_id, photo_file_id)
        update_subscriber_photo_storage(user_id, photo_file_id, storage_path)
    else:
        await message.answer("Пожалуйста, отправьте фото или нажмите «Пропустить»")
        return

    # Профиль уже полностью сохранён в БД, просто завершаем регистрацию
    data = await state.get_data()
    await message.answer(
        "✅ *Профиль успешно создан!*\n\n"
        f"📝 ФИО: {data['full_name']}\n"
        f"🎂 Возраст: {data['age']} лет\n"
        f"📞 Телефон: {data['phone']}\n\n"
        "Выберите категории вакансий ниже 👇",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    await send_category_picker(message.chat.id, user_id)
    await state.clear()


# ========== ОБРАБОТКА КАТЕГОРИЙ (с отметками) ==========

@dp.callback_query(lambda c: c.data and c.data.startswith("cat_"))
async def select_category(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    user_id = callback.from_user.id
    category_code = callback.data.replace("cat_", "")
    current_codes = [c["code"] for c in await run_db(get_user_categories, user_id)]
    hint = ""
    if category_code in current_codes:
        current_codes.remove(category_code)
    elif not await run_db(is_user_premium, user_id) and len(current_codes) >= FREE_CATEGORY_LIMIT:
        hint = (
            f"⚠️ На Free — не больше *{FREE_CATEGORY_LIMIT}* категорий.\n"
            "Нужно больше? Нажмите *💎 Premium* ниже."
        )
    else:
        current_codes.append(category_code)
    if hint:
        await edit_category_picker(callback.message, current_codes, user_id, hint=hint)
        return
    await run_db(set_user_categories, user_id, current_codes)
    await edit_category_picker(callback.message, current_codes, user_id)


@dp.callback_query(lambda c: c.data == "back_to_categories")
async def back_to_categories(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    user_id = callback.from_user.id
    current_codes = [c["code"] for c in get_user_categories(user_id)]
    await edit_category_picker(callback.message, current_codes, user_id)


@dp.callback_query(lambda c: c.data == "subscription_from_categories")
async def subscription_from_categories(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    user_id = callback.from_user.id
    buttons = subscription_action_buttons(user_id)
    nav = [[InlineKeyboardButton(text="◀️ К выбору категорий", callback_data="back_to_categories")]]
    markup = InlineKeyboardMarkup(
        inline_keyboard=(buttons.inline_keyboard if buttons else []) + nav
    )
    try:
        await send_subscription_screen(
            callback.message, user_id, reply_markup=markup, edit=True,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"subscription_from_categories: {e}")


@dp.callback_query(lambda c: c.data == "disable_feed")
async def disable_feed_prompt(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    await callback.message.answer(
        "🔕 *Отключить рассылку вакансий?*\n\n"
        "Категории будут сброшены, push и лента остановятся.\n"
        "Профиль, отклики и подписка Premium сохранятся.\n\n"
        "Включить снова — «⚙️ Настройки» и выберите категории.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, отключить", callback_data="disable_feed_confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_categories"),
            ],
        ]),
    )


@dp.callback_query(lambda c: c.data == "disable_feed_confirm")
async def disable_feed_confirm(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await safe_callback_answer(callback, "Рассылка отключена")
    set_user_categories(user_id, [])
    keyboard, _ = get_main_keyboard(user_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await bot.send_message(
        user_id,
        "🔕 *Рассылка отключена.*\n\n"
        "Профиль и отклики сохранены. Чтобы снова получать вакансии — "
        "«⚙️ Настройки» и выберите категории.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@dp.callback_query(lambda c: c.data == "finish_categories")
async def finish_categories(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id in _processing_finish:
        await safe_callback_answer(callback, "⏳ Уже оформляем…")
        return
    _processing_finish.add(user_id)
    try:
        categories = await run_db(get_user_categories, user_id)
        if not categories:
            await safe_callback_answer(callback, "⚠️ Выберите хотя бы одну категорию!", show_alert=True)
            return
        if not await run_db(is_user_premium, user_id) and len(categories) > FREE_CATEGORY_LIMIT:
            await safe_callback_answer(
                callback,
                f"🆓 На Free — до {FREE_CATEGORY_LIMIT} категорий. Оформите Premium.",
                show_alert=True,
            )
            return
        await safe_callback_answer(callback)
        categories_text = "\n".join([f"• {c['emoji']} {c['name']}" for c in categories])
        keyboard, _ = get_main_keyboard(user_id)
        trial_granted = await run_db(grant_trial_if_eligible, user_id, TRIAL_DAYS)
        trial_line = ""
        if trial_granted:
            trial_line = f"\n\n🎁 *Пробный Premium на {TRIAL_DAYS} дн.* — push и фильтр по метро!"
        title = "✅ *Вы подписались на вакансии!*" if trial_granted else "✅ *Категории сохранены!*"
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
        await bot.send_message(
            user_id,
            f"{title}\n\n"
            f"📌 Ваши категории:\n{categories_text}\n\n"
            f"{'💎 Новые вакансии приходят моментально в чат.' if await run_db(is_user_premium, user_id) else '🔍 Free: новые вакансии — кнопка «Посмотреть новые».'}"
            f"{trial_line}\n\n"
            f"📖 Инструкция — «Как пользоваться» или /help\n\n"
            f"Используйте кнопки меню:",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    finally:
        _processing_finish.discard(user_id)


# ========== МОИ ОТКЛИКИ ==========

def _response_status_label(is_closed: bool) -> str:
    return "🔒 закрыта" if is_closed else "🟢 активна"


async def send_responses_page(message: types.Message, user_id: int, page: int = 0):
    total = count_user_responses(user_id)
    if total == 0:
        await message.answer(
            "📨 *Мои отклики*\n\n"
            "Пока нет откликов. Нажмите «✋ Откликнуться» под вакансией в ленте.",
            parse_mode="Markdown",
        )
        return

    start = page * RESPONSES_PAGE_SIZE
    responses = get_user_responses(user_id, limit=RESPONSES_PAGE_SIZE, offset=start)
    pages_total = (total - 1) // RESPONSES_PAGE_SIZE + 1
    user_response_pages[user_id] = {"page": page, "total": total}

    await message.answer(
        f"📨 *Мои отклики* — страница {page + 1}/{pages_total} (всего {total})",
        parse_mode="Markdown",
    )

    profile = get_subscriber_profile(user_id)
    draft_text = build_candidate_profile_text(profile) if profile else ""

    for i, resp in enumerate(responses, start=start + 1):
        preview = (resp.get("vacancy_text") or "—").strip()
        if len(preview) > 160:
            preview = preview[:160] + "…"
        source = resp.get("source_chat_title") or "—"
        text = (
            f"*{i}.* {format_db_datetime_short(resp.get('responded_at'))} · "
            f"{_response_status_label(resp.get('is_closed'))}\n"
            f"📢 {escape_markdown(source)}\n\n"
            f"{escape_markdown(preview)}"
        )
        buttons = []
        if resp.get("vacancy_link"):
            buttons.append([InlineKeyboardButton(text="🔗 Вакансия", url=resp["vacancy_link"])])
        contact = resp.get("author_contact")
        if contact and draft_text:
            contact_link = build_contact_link(contact, draft_text)
            if contact_link:
                buttons.append([InlineKeyboardButton(text="💬 Заказчик", url=contact_link)])
        if resp.get("vacancy_id"):
            buttons.append([
                InlineKeyboardButton(
                    text="⚠️ Пожаловаться",
                    callback_data=f"complain_{resp['vacancy_id']}",
                )
            ])
        markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        try:
            await message.answer(text, parse_mode="MarkdownV2", reply_markup=markup)
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.warning(f"send_responses_page item {i}: {e}")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"resp_page_{page - 1}"))
    if start + len(responses) < total:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"resp_page_{page + 1}"))
    if nav:
        await message.answer(
            "Навигация:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[nav]),
        )


@dp.message(lambda m: m.text == "📨 Мои отклики")
async def show_my_responses(message: types.Message):
    await send_responses_page(message, message.from_user.id, page=0)


@dp.callback_query(lambda c: c.data and c.data.startswith("resp_page_"))
async def responses_page_callback(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    try:
        page = int(callback.data.replace("resp_page_", ""))
    except ValueError:
        return
    await send_responses_page(callback.message, callback.from_user.id, page=page)


# ========== ПАГИНАЦИЯ ДЛЯ ПРОСМОТРА ВАКАНСИЙ ==========

def _feed_metro_context(user_id: int) -> tuple[bool, str | None]:
    profile = get_subscriber_profile(user_id)
    metro_zones = profile.get("metro_zones") if profile else None
    apply_metro = bool(is_user_premium(user_id) and metro_zones)
    return apply_metro, metro_zones


def _feed_vacancies_for_category(user_id: int, cat: dict, apply_metro: bool, metro_zones: str | None) -> list:
    vacancies = get_feed_vacancies_for_user(user_id, cat["code"])
    result = []
    for vac in vacancies:
        if apply_metro and not vacancy_matches_user_metro(
            vac.get("text", ""), vac.get("address"), metro_zones
        ):
            continue
        vac["category"] = cat
        result.append(vac)
    return result


def _collect_feed_vacancies(user_id: int, category_codes: list[str] | None = None) -> list:
    apply_metro, metro_zones = _feed_metro_context(user_id)
    user_categories = get_user_categories(user_id)
    if category_codes is not None:
        codes = set(category_codes)
        user_categories = [c for c in user_categories if c["code"] in codes]
    all_vacancies = []
    for cat in user_categories:
        all_vacancies.extend(_feed_vacancies_for_category(user_id, cat, apply_metro, metro_zones))
    all_vacancies.sort(key=lambda v: v.get("found_at") or "", reverse=True)
    return all_vacancies


def _feed_count_for_category(user_id: int, cat: dict, apply_metro: bool, metro_zones: str | None) -> int:
    return len(_feed_vacancies_for_category(user_id, cat, apply_metro, metro_zones))


def build_feed_category_keyboard(user_id: int) -> tuple[InlineKeyboardMarkup, int]:
    user_categories = get_user_categories(user_id)
    apply_metro, metro_zones = _feed_metro_context(user_id)
    buttons, row = [], []
    total = 0
    for i, cat in enumerate(user_categories):
        count = _feed_count_for_category(user_id, cat, apply_metro, metro_zones)
        total += count
        row.append(InlineKeyboardButton(
            text=f"{cat['emoji']} {cat['name']} ({count})",
            callback_data=f"feed_cat_{cat['code']}",
        ))
        if len(row) == 2 or i == len(user_categories) - 1:
            buttons.append(row)
            row = []
    if total > 0:
        buttons.append([InlineKeyboardButton(text=f"📋 Все категории ({total})", callback_data="feed_all")])
    return InlineKeyboardMarkup(inline_keyboard=buttons), total


async def show_feed_category_menu(message: types.Message, user_id: int):
    user_categories = get_user_categories(user_id)
    if not user_categories:
        await message.answer("⚠️ Вы ещё не выбрали категории вакансий. Используйте «⚙️ Настройки»")
        return
    markup, total = build_feed_category_keyboard(user_id)
    apply_metro, _ = _feed_metro_context(user_id)
    hint = ""
    if apply_metro:
        hint = "\n\n📍 Учитывается фильтр «Мои районы»."
    if total == 0:
        await message.answer(
            f"🔍 *Новых вакансий по вашим категориям пока нет.*{hint}\n\n"
            f"{'💎 Premium — push сразу в чат.' if is_user_premium(user_id) else 'Я продолжаю мониторинг.'}",
            parse_mode="Markdown",
        )
        return
    await message.answer(
        f"🔍 *Лента вакансий* — выберите категорию ({total} новых):{hint}",
        parse_mode="Markdown",
        reply_markup=markup,
    )


async def open_feed_vacancies(message: types.Message, user_id: int, category_codes: list[str] | None = None):
    all_vacancies = _collect_feed_vacancies(user_id, category_codes)
    if not all_vacancies:
        apply_metro, _ = _feed_metro_context(user_id)
        hint = "\n\nПопробуйте расширить «📍 Мои районы»." if apply_metro else ""
        await message.answer(f"🔍 В этой категории новых вакансий нет.{hint}", parse_mode="Markdown")
        return
    user_pages[user_id] = {
        "vacancies": all_vacancies,
        "page": 0,
        "total": len(all_vacancies),
        "feed_filter": category_codes,
    }
    await send_vacancy_page(message, user_id, 0)


@dp.message(lambda m: m.text == "🔍 Посмотреть новые вакансии")
async def show_new_vacancies(message: types.Message):
    await show_feed_category_menu(message, message.from_user.id)


@dp.callback_query(lambda c: c.data == "feed_all")
async def feed_all_callback(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    await open_feed_vacancies(callback.message, callback.from_user.id, category_codes=None)


@dp.callback_query(lambda c: c.data and c.data.startswith("feed_cat_"))
async def feed_category_callback(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    code = callback.data.replace("feed_cat_", "", 1)
    await open_feed_vacancies(callback.message, callback.from_user.id, category_codes=[code])


@dp.callback_query(lambda c: c.data == "feed_menu")
async def feed_menu_callback(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    await show_feed_category_menu(callback.message, callback.from_user.id)


async def send_vacancy_page(message: types.Message, user_id: int, page: int):
    data = user_pages.get(user_id)
    if not data:
        return
    vacancies = data["vacancies"]
    total = data["total"]
    start = page * 10
    end = min(start + 10, total)
    if start >= total:
        await message.answer("📭 Это последняя страница.")
        return

    await message.answer(f"📬 *Вакансии (страница {page+1} из {(total-1)//10 + 1})*", parse_mode="Markdown")
    for vac in vacancies[start:end]:
        raw_pub = vac.get('published_at') or vac.get('found_at')
        cat = vac.get("category") or {}
        text = format_vacancy_card_html(
            category_emoji=cat.get("emoji") or get_category_emoji("helper"),
            category_name=cat.get("name") or "Вакансия",
            freshness=get_freshness_label(raw_pub),
            published_at=format_publication_time(raw_pub),
            body=vac.get("text") or "",
            source=vac.get("source") or "—",
            message_link=vac.get("link"),
        )
        keyboard = build_vacancy_keyboard(vac["id"], vac.get("address"))
        try:
            await send_vacancy_card(message.chat.id, text, reply_markup=keyboard)
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"Ошибка отправки вакансии: {e}")

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"vac_page_{page-1}"))
    if end < total:
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"vac_page_{page+1}"))
    nav_buttons.append(InlineKeyboardButton(text="📋 К категориям", callback_data="feed_menu"))
    if nav_buttons:
        nav_markup = InlineKeyboardMarkup(inline_keyboard=[nav_buttons])
        await message.answer("📄 *Навигация*", parse_mode="Markdown", reply_markup=nav_markup)


@dp.callback_query(lambda c: c.data and c.data.startswith("vac_page_"))
async def vacancy_page_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    page = int(callback.data.split("_")[2])
    await send_vacancy_page(callback.message, user_id, page)
    await callback.answer()


# ========== ОСНОВНЫЕ КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ==========

@dp.message(lambda m: m.text == "💎 Подписка")
async def subscription_menu(message: types.Message):
    user_id = message.from_user.id
    if user_id == YOUR_USER_ID:
        n = count_premium_subscribers()
        pending = count_pending_premium_requests()
        await message.answer(
            f"💎 Premium-подписчиков: *{n}*\n"
            f"💳 Ожидают подтверждения: *{pending}*\n\n"
            f"Выдать: `/setplan USER_ID premium 30`\n"
            f"Снять: `/setplan USER_ID free`\n\n"
            f"Список запросов: кнопка «💎 Запросы Premium»",
            parse_mode="Markdown",
        )
        return
    buttons = subscription_action_buttons(user_id)
    await send_subscription_screen(message, user_id, reply_markup=buttons)


@dp.callback_query(lambda c: c.data in ("subscription_request", "subscription_renew"))
async def subscription_request_callback(callback: types.CallbackQuery, state: FSMContext):
    is_renewal = callback.data == "subscription_renew"
    await safe_callback_answer(callback, "Дальше — чек об оплате")
    user_id = callback.from_user.id
    profile = get_subscriber_profile(user_id)
    name = profile.get("full_name") if profile else callback.from_user.first_name
    phone = profile.get("phone") if profile else None
    cats = ", ".join(c["name"] for c in get_user_categories(user_id)) or "—"
    request_id = add_premium_request(
        user_id,
        callback.from_user.username,
        name,
        phone,
        cats,
        is_renewal=is_renewal,
    )
    await state.set_state(PremiumPaymentState.waiting_for_receipt)
    await state.update_data(premium_request_id=request_id, premium_is_renewal=is_renewal)
    pay_hint = (
        f"Переведите <b>{escape_html(SUBSCRIPTION_PRICE_RUB)} ₽</b> "
        f"по реквизитам из раздела 💎 Подписка.\n"
        f"В комментарии укажите ID: <code>{user_id}</code>\n\n"
    )
    if is_renewal:
        intro = "✅ <b>Запрос на продление Premium</b>\n\n"
        tail = "После проверки срок Premium будет <b>продлён</b>."
    else:
        intro = "✅ <b>Запрос на Premium</b>\n\n"
        tail = "После проверки включим Premium — можно будет выбрать больше категорий."
    await callback.message.answer(
        f"{intro}{pay_hint}"
        f"📎 <b>Пришлите скрин чека</b> — фото или PDF-документ.\n\n"
        f"{tail}\n\n"
        f"Отмена — напишите «Отмена».",
        parse_mode="HTML",
    )


@dp.message(PremiumPaymentState.waiting_for_receipt)
async def premium_payment_receipt(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    request_id = data.get("premium_request_id")
    if not request_id:
        await state.clear()
        await message.answer("❌ Сессия запроса истекла. Начните снова: 💎 Подписка.")
        return
    if message.text and message.text.strip().lower() in ("отмена", "cancel", "/cancel"):
        cancel_premium_request_awaiting(user_id, request_id)
        await state.clear()
        await message.answer("❌ Запрос отменён. Когда будете готовы — снова 💎 Подписка.")
        return
    file_id = None
    kind = None
    if message.photo:
        file_id = message.photo[-1].file_id
        kind = "photo"
    elif message.document:
        file_id = message.document.file_id
        kind = "document"
    if not file_id:
        await message.answer(
            "📎 Нужен скрин перевода — отправьте <b>фото</b> или <b>файл</b> (PDF).\n"
            "Отмена — «Отмена».",
            parse_mode="HTML",
        )
        return
    if not attach_premium_request_receipt(request_id, user_id, file_id, kind):
        await state.clear()
        await message.answer("❌ Запрос не найден или уже обработан. Начните снова: 💎 Подписка.")
        return
    await state.clear()
    is_renewal = data.get("premium_is_renewal")
    if is_renewal:
        confirm = (
            "✅ <b>Чек получен</b>\n\n"
            "Администратор проверит оплату и продлит Premium. "
            "Подтверждение придёт сюда."
        )
    else:
        confirm = (
            "✅ <b>Чек получен</b>\n\n"
            "Администратор проверит оплату и активирует Premium. "
            "Подтверждение придёт сюда."
        )
    await message.answer(confirm, parse_mode="HTML")
    await send_admin_premium_request_alert(request_id)


@dp.callback_query(lambda c: c.data and c.data.startswith("pr_a_"))
async def premium_request_approve_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await safe_callback_answer(callback, "Нет доступа", show_alert=True)
        return
    try:
        request_id = int(callback.data.split("_", 2)[2])
    except (ValueError, IndexError):
        await safe_callback_answer(callback, "Неверный запрос", show_alert=True)
        return
    req = get_premium_request(request_id)
    if not req or req.get("status") != "pending":
        await safe_callback_answer(callback, "Запрос уже обработан", show_alert=True)
        return
    target_id = req["user_id"]
    was_active = is_user_premium(target_id)
    notified = await activate_premium_for_user(target_id, PREMIUM_DEFAULT_DAYS)
    mode = "продлён" if was_active else "активирован"
    await safe_callback_answer(callback, f"Premium {mode}")
    try:
        suffix = f"\n\n✅ Premium {mode} на {PREMIUM_DEFAULT_DAYS} дн."
        if callback.message.photo or callback.message.document:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + suffix,
                reply_markup=None,
            )
        else:
            await callback.message.edit_text(
                (callback.message.text or "") + suffix,
                reply_markup=None,
            )
    except TelegramBadRequest:
        pass


@dp.callback_query(lambda c: c.data and c.data.startswith("pr_r_"))
async def premium_request_reject_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await safe_callback_answer(callback, "Нет доступа", show_alert=True)
        return
    try:
        request_id = int(callback.data.split("_", 2)[2])
    except (ValueError, IndexError):
        await safe_callback_answer(callback, "Неверный запрос", show_alert=True)
        return
    target_id = reject_premium_request(request_id)
    if not target_id:
        await safe_callback_answer(callback, "Запрос уже обработан", show_alert=True)
        return
    await safe_callback_answer(callback, "Отклонено")
    try:
        await bot.send_message(
            target_id,
            "❌ *Запрос Premium отклонён.*\n\n"
            "Если это ошибка или нужна помощь — ❓ Поддержка.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить {target_id} об отклонении Premium: {e}")
    try:
        suffix = "\n\n❌ Отклонено"
        if callback.message.photo or callback.message.document:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + suffix,
                reply_markup=None,
            )
        else:
            await callback.message.edit_text(
                (callback.message.text or "") + suffix,
                reply_markup=None,
            )
    except TelegramBadRequest:
        pass


@dp.message(lambda m: m.text == "📍 Мои районы")
async def metro_zones_menu(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_user_premium(user_id):
        await message.answer(
            "📍 Фильтр по метро — функция *Premium*.\n\n"
            "Оформите подписку в 💎 Подписка или дождитесь окончания пробного периода.",
            parse_mode="Markdown",
        )
        return
    profile = get_subscriber_profile(user_id)
    current = profile.get("metro_zones") if profile else None
    current_line = current if current else "не заданы (приходят все локации)"
    await message.answer(
        f"📍 *Мои станции метро*\n\n"
        f"Сейчас: {current_line}\n\n"
        f"Введите станции через запятую, например:\n"
        f"`Таганская, Беляево, Сокол`\n\n"
        f"Отправьте `-` чтобы сбросить фильтр.",
        parse_mode="Markdown",
    )
    await state.set_state(MetroState.waiting_for_zones)


@dp.message(MetroState.waiting_for_zones)
async def metro_zones_save(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text.strip()
    if text == "-":
        set_user_metro_zones(user_id, None)
        await message.answer("✅ Фильтр по метро сброшен — снова все локации.")
    else:
        zones = ", ".join(z.strip() for z in text.split(",") if z.strip())
        if not zones:
            await message.answer("❌ Укажите хотя бы одну станцию или `-` для сброса.")
            return
        set_user_metro_zones(user_id, zones)
        await message.answer(f"✅ Сохранено: *{zones}*\n\nPush и лента — только вакансии с этими станциями.", parse_mode="Markdown")
    await state.clear()

@dp.message(Command("setplan"))
async def setplan_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "Использование:\n"
            "`/setplan USER_ID premium 30` — premium на 30 дней\n"
            "`/setplan USER_ID free` — бесплатный план",
            parse_mode="Markdown",
        )
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("❌ USER_ID должен быть числом.")
        return
    plan = parts[2].lower()
    if plan == "free":
        set_user_plan(target_id, plan="free")
        await message.answer(f"✅ Пользователь {target_id} переведён на free.")
        return
    days = int(parts[3]) if len(parts) > 3 else 30
    was_active = is_user_premium(target_id)
    notified = await activate_premium_for_user(target_id, days)
    mode = "продлён" if was_active else "выдан"
    await message.answer(
        f"✅ Premium для {target_id} {mode} на {days} дн."
        + ("" if notified else " (уведомление пользователю не доставлено)")
    )

@dp.message(lambda m: m.text in {BTN_SETTINGS, BTN_SETTINGS_LEGACY, "📋 Мои категории", "✏️ Изменить категории"})
async def open_categories_menu(message: types.Message):
    await send_category_picker(message.chat.id, message.from_user.id)


@dp.message(lambda m: m.text in {BTN_MY_DATA, BTN_MY_DATA_LEGACY})
async def show_my_data(message: types.Message, state: FSMContext):
    await state.clear()
    await send_profile_data_screen(message.chat.id, message.from_user.id)


@dp.callback_query(lambda c: c.data == "profile_back_menu")
async def profile_back_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_callback_answer(callback)
    keyboard, status = get_main_keyboard(callback.from_user.id)
    await callback.message.answer(f"🏠 Главное меню\n\n{status}", reply_markup=keyboard)


@dp.callback_query(lambda c: c.data == "profile_edit_cancel")
async def profile_edit_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_callback_answer(callback, "Отменено")
    await send_profile_data_screen(callback.message.chat.id, callback.from_user.id)


@dp.callback_query(lambda c: c.data == "profile_edit_name")
async def profile_edit_name(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await callback.message.answer(
        "✏️ *Новое ФИО*\n\nВведите полное имя и фамилию (минимум 2 слова):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="profile_edit_cancel")],
        ]),
    )
    await state.set_state(ProfileEditState.waiting_for_name)


@dp.message(ProfileEditState.waiting_for_name)
async def profile_name_received(message: types.Message, state: FSMContext):
    full_name = (message.text or "").strip()
    if len(full_name.split()) < 2:
        await message.answer("❌ Введите полное имя и фамилию (минимум 2 слова).")
        return
    if not re.match(r'^[a-zA-Zа-яА-ЯёЁ\s\-\.]+$', full_name):
        await message.answer("❌ Имя может содержать только буквы, пробелы, дефисы и точки.")
        return
    user_id = message.from_user.id
    update_subscriber_name(user_id, full_name)
    await finish_profile_field_edit(message, state, user_id, f"✅ ФИО обновлено: *{escape_markdown(full_name)}*")


@dp.callback_query(lambda c: c.data == "profile_edit_age")
async def profile_edit_age(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await callback.message.answer(
        "🎂 *Дата рождения*\n\nВведите дату в формате ДД.ММ.ГГГГ (пример: `25.12.1990`):",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="profile_edit_cancel")],
        ]),
    )
    await state.set_state(ProfileEditState.waiting_for_birthdate)


@dp.message(ProfileEditState.waiting_for_birthdate)
async def profile_birthdate_received(message: types.Message, state: FSMContext):
    birth_date_str = (message.text or "").strip()
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', birth_date_str):
        await message.answer("❌ Формат: ДД.ММ.ГГГГ (пример: 25.12.1990)")
        return
    age = calculate_age(birth_date_str)
    if age is None or age < 16 or age > 100:
        await message.answer("❌ Некорректная дата или возраст вне диапазона 16–100 лет.")
        return
    user_id = message.from_user.id
    update_subscriber_age(user_id, age, birth_date_str)
    await finish_profile_field_edit(
        message, state, user_id,
        f"✅ Возраст обновлён: *{age}* лет ({escape_markdown(birth_date_str)})",
    )


@dp.callback_query(lambda c: c.data == "profile_edit_phone")
async def profile_edit_phone(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    phone_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await callback.message.answer(
        "📞 *Новый телефон*\n\nОтправьте номер текстом или кнопкой ниже.",
        parse_mode="Markdown",
        reply_markup=phone_keyboard,
    )
    await state.set_state(ProfileEditState.waiting_for_phone)


@dp.message(ProfileEditState.waiting_for_phone)
async def profile_phone_received(message: types.Message, state: FSMContext):
    if message.contact:
        phone = message.contact.phone_number
    else:
        phone = (message.text or "").strip()
        digits_only = re.sub(r'\D', '', phone)
        if len(digits_only) < 10 or len(digits_only) > 15:
            await message.answer("❌ Введите корректный номер или нажмите кнопку «Отправить мой номер телефона».")
            return
    user_id = message.from_user.id
    update_subscriber_phone(user_id, phone)
    await finish_profile_field_edit(
        message, state, user_id,
        f"✅ Телефон обновлён: {escape_markdown(phone)}",
    )


@dp.callback_query(lambda c: c.data == "profile_edit_photo")
async def profile_edit_photo(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await callback.message.answer(
        "📷 *Новое фото*\n\nОтправьте фото для откликов.\n"
        "Чтобы удалить фото — напишите «Удалить».",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="profile_edit_cancel")],
        ]),
    )
    await state.set_state(ProfileEditState.waiting_for_photo)


@dp.message(ProfileEditState.waiting_for_photo)
async def profile_photo_received(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if (message.text or "").strip().lower() in {"удалить", "delete", "нет"}:
        clear_subscriber_photo(user_id)
        await finish_profile_field_edit(message, state, user_id, "✅ Фото удалено из профиля.")
        return
    if not message.photo:
        await message.answer("Отправьте фото или напишите «Удалить».")
        return
    photo_file_id = message.photo[-1].file_id
    storage_path, photo_file_id = await persist_user_photo(bot, user_id, photo_file_id)
    update_subscriber_photo_storage(user_id, photo_file_id, storage_path)
    await finish_profile_field_edit(message, state, user_id, "✅ Фото профиля обновлено.")


@dp.callback_query(lambda c: c.data == "profile_edit_extra")
async def profile_edit_extra(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    await callback.message.answer(
        "📝 *Доп. информация для резюме*\n\n"
        "Рост, вес, опыт, навыки — одним сообщением.\n"
        "Чтобы очистить — напишите «Очистить».",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="profile_edit_cancel")],
        ]),
    )
    await state.set_state(ProfileEditState.waiting_for_extra)


@dp.message(ProfileEditState.waiting_for_extra)
async def profile_extra_received(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    if text.lower() in {"очистить", "удалить", "нет"}:
        update_resume_extra(user_id, None)
        await finish_profile_field_edit(message, state, user_id, "✅ Доп. информация очищена.")
        return
    if len(text) < 3:
        await message.answer("❌ Слишком коротко. Напишите хотя бы пару слов или «Очистить».")
        return
    if len(text) > 1500:
        await message.answer("❌ Слишком длинно (макс. 1500 символов).")
        return
    update_resume_extra(user_id, text)
    await finish_profile_field_edit(message, state, user_id, "✅ Доп. информация сохранена.")


@dp.message(lambda m: m.text == BTN_UNSUB_LEGACY)
async def unsubscribe_user_legacy(message: types.Message):
    user_id = message.from_user.id
    set_user_categories(user_id, [])
    keyboard, _ = get_main_keyboard(user_id)
    await message.answer(
        "🔕 *Рассылка отключена.*\n\n"
        "Профиль сохранён. Чтобы снова получать вакансии — «⚙️ Настройки».",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

@dp.message(lambda m: m.text == "❓ Поддержка")
async def support_menu(message: types.Message, state: FSMContext):
    await message.answer(
        "📞 *Поддержка*\n\n"
        "Опишите вашу проблему или вопрос, и администратор ответит вам в ближайшее время.\n\n"
        "Напишите ваше сообщение:",
        parse_mode="Markdown"
    )
    await state.set_state(SupportState.waiting_for_question)

@dp.message(SupportState.waiting_for_question)
async def process_support_question(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    add_support_request(user_id, message.text, username)
    await message.answer("✅ Ваш вопрос отправлен администратору. Ответ придёт сюда.")
    await state.clear()


# ========== ЖАЛОБЫ НА ВАКАНСИИ ==========

@dp.callback_query(lambda c: c.data and c.data.startswith("complain_"))
async def start_complaint(callback: types.CallbackQuery, state: FSMContext):
    vacancy_id = callback.data.replace("complain_", "")
    await state.update_data(vacancy_id=vacancy_id)
    await callback.message.answer(
        "⚠️ *Пожаловаться на вакансию*\n\n"
        "Выберите причину:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Вакансия неактуальна/закрыта", callback_data="complaint_reason_closed")],
            [InlineKeyboardButton(text="🤬 Грубость/неуважение", callback_data="complaint_reason_rude")],
            [InlineKeyboardButton(text="📛 Мошенничество/обман", callback_data="complaint_reason_scam")],
            [InlineKeyboardButton(text="📝 Другое (напишу)", callback_data="complaint_reason_other")]
        ])
    )
    await state.set_state(ComplaintState.waiting_for_reason)
    await callback.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("complaint_reason_"))
async def complaint_reason(callback: types.CallbackQuery, state: FSMContext):
    reason_map = {
        "complaint_reason_closed": "Вакансия неактуальна/закрыта",
        "complaint_reason_rude": "Грубость/неуважение",
        "complaint_reason_scam": "Мошенничество/обман",
        "complaint_reason_other": "Другое"
    }
    reason = reason_map.get(callback.data, "Другое")
    await state.update_data(reason=reason)
    if callback.data == "complaint_reason_other":
        await callback.message.answer("Напишите подробности жалобы (текст):")
        await state.set_state(ComplaintState.waiting_for_text)
    else:
        data = await state.get_data()
        vacancy_id = data.get("vacancy_id")
        user_id = callback.from_user.id
        add_complaint(user_id, vacancy_id, reason)
        await callback.message.answer("✅ Жалоба отправлена администратору. Спасибо, что помогаете улучшить сервис!")
        await state.clear()
    await callback.answer()

@dp.message(ComplaintState.waiting_for_text)
async def complaint_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    vacancy_id = data.get("vacancy_id")
    reason = data.get("reason")
    user_id = message.from_user.id
    add_complaint(user_id, vacancy_id, reason, message.text)
    await message.answer("✅ Жалоба отправлена администратору. Спасибо, что помогаете улучшить сервис!")
    await state.clear()


# ========== ОТКЛИКИ НА ВАКАНСИИ С ВОЗМОЖНОСТЬЮ ПРИКРЕПИТЬ ФОТО ==========

@dp.callback_query(
    lambda c: c.data
    and c.data.startswith("respond_")
    and not c.data.startswith("respond_add_")
    and c.data != "respond_cancel"
)
async def handle_response(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    vacancy_id = callback.data.replace("respond_", "")
    if is_already_responded(user_id, vacancy_id):
        await callback.answer("❌ Вы уже откликались на эту вакансию!", show_alert=True)
        return
    profile = get_subscriber_profile(user_id)
    if not profile or not profile.get("full_name") or not profile.get("phone"):
        await callback.answer("⚠️ Сначала заполните профиль! Нажмите /start", show_alert=True)
        return
    vacancy_row = get_vacancy_row(vacancy_id)
    if not vacancy_row:
        await callback.answer("❌ Вакансия не найдена", show_alert=True)
        return
    vacancy_text, vacancy_link, source_chat, saved_contact, address = vacancy_row
    employer_contact = saved_contact or extract_contact_from_text(vacancy_text or "")
    if not employer_contact:
        await callback.answer("⚠️ Контакт заказчика не найден. Отправлю отклик администратору.", show_alert=True)
        await send_to_admin(callback, profile, vacancy_row, build_candidate_profile_text(profile), profile.get('photo_file_id'))
        return
    required_fields = extract_required_fields_from_vacancy(vacancy_text or "")
    draft_text = build_candidate_profile_text(profile)
    contact_link = build_contact_link(employer_contact, draft_text)
    await state.update_data(vacancy_id=vacancy_id, contact=employer_contact, draft_text=draft_text)
    req_line = ", ".join(required_fields) if required_fields else "явных требований не найдено"
    msg = (
        "📨 *Черновик отклика готов*\n\n"
        f"👨‍💼 Контакт заказчика: `{escape_markdown(employer_contact)}`\n"
        f"📌 Источник: {escape_markdown(source_chat or '—')}\n"
        f"🧾 Что просит вакансия: {escape_markdown(req_line)}\n\n"
        "Нажмите кнопку ниже, откроется личный чат с заказчиком и готовым текстом анкеты.\n"
        "Перед отправкой можно отредактировать сообщение вручную."
    )
    buttons = []
    if contact_link:
        buttons.append([InlineKeyboardButton(text="✅ Открыть чат и отправить", url=contact_link)])
    buttons.append([InlineKeyboardButton(text="✏️ Добавить комментарий", callback_data=f"respond_add_{vacancy_id}")])
    buttons.append([InlineKeyboardButton(text="🚫 Отмена", callback_data="respond_cancel")])
    add_response(user_id, vacancy_id, vacancy_text[:200] if vacancy_text else None, vacancy_link, profile.get('photo_file_id'))
    await callback.message.answer(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.message.answer(
        "📨 Отклик сохранён — смотрите в «📨 Мои отклики».",
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_vacancy")
async def handle_admin_vacancy_response(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    vacancy_text = callback.message.caption or callback.message.text
    vacancy_id = f"admin_{callback.message.message_id}"
    if is_already_responded(user_id, vacancy_id):
        await callback.answer("❌ Вы уже откликались на эту вакансию!", show_alert=True)
        return
    profile = get_subscriber_profile(user_id)
    if not profile or not profile.get("full_name") or not profile.get("phone"):
        await callback.answer("⚠️ Сначала заполните профиль! Нажмите /start", show_alert=True)
        return
    add_response(user_id, vacancy_id, vacancy_text[:200] if vacancy_text else None, None, profile.get('photo_file_id'))
    candidate_questionnaire = profile.get('questionnaire') or f"""📝 *АНКЕТА КАНДИДАТА*

👤 *ФИО:* {profile['full_name']}
🎂 *Возраст:* {profile['age']} лет
📞 *Телефон:* {profile['phone']}
🆔 *Telegram:* @{profile['username'] if profile['username'] else 'нет'}
"""
    admin_message = (
        f"🔔 *НОВЫЙ ОТКЛИК НА АДМИНСКУЮ ВАКАНСИЮ!*\n\n"
        f"📝 *Текст вакансии:*\n{vacancy_text}\n\n"
        f"{candidate_questionnaire}\n"
        f"👤 *Ссылка на кандидата:* [Написать](tg://user?id={user_id})"
    )
    if profile.get('photo_file_id') or profile.get('photo_storage_path'):
        await send_profile_photo(
            bot, YOUR_USER_ID, profile, caption=admin_message, parse_mode="Markdown",
        )
    else:
        await bot.send_message(YOUR_USER_ID, admin_message, parse_mode="Markdown")
    await callback.answer("✅ Ваш отклик отправлен администратору!", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)

@dp.callback_query(lambda c: c.data == "skip_photo_respond")
async def skip_photo_respond(callback: types.CallbackQuery, state: FSMContext):
    await send_application(callback, callback.from_user.id, (await state.get_data()).get("vacancy_id"), None)
    await state.clear()
    await callback.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("respond_add_"))
async def respond_add_comment(callback: types.CallbackQuery, state: FSMContext):
    vacancy_id = callback.data.replace("respond_add_", "")
    await state.update_data(vacancy_id=vacancy_id)
    await callback.message.answer("✏️ Напишите, что добавить в отклик одним сообщением:")
    await state.set_state(ResponseDraftState.waiting_for_comment)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "respond_cancel")
async def respond_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Отклик отменён", show_alert=False)

@dp.message(ResponseDraftState.waiting_for_comment)
async def respond_comment_received(message: types.Message, state: FSMContext):
    data = await state.get_data()
    vacancy_id = data.get("vacancy_id")
    profile = get_subscriber_profile(message.from_user.id)
    row = get_vacancy_row(vacancy_id)
    if not row:
        await message.answer("❌ Вакансия не найдена.")
        await state.clear()
        return
    vacancy_text, saved_contact = row
    contact = saved_contact or extract_contact_from_text(vacancy_text or "")
    draft_text = build_candidate_profile_text(profile, extra_comment=message.text)
    link = build_contact_link(contact, draft_text)
    if not link:
        await message.answer(
            f"⚠️ Не удалось собрать ссылку для чата. Контакт: {contact or 'не найден'}\n\n"
            f"Скопируйте и отправьте вручную:\n\n{draft_text}"
        )
        await state.clear()
        return
    await message.answer(
        "✅ Обновил черновик. Откройте чат с заказчиком:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✅ Открыть чат и отправить", url=link)]]
        )
    )
    await state.clear()

@dp.message(RespondWithPhotoState.waiting_for_photo)
async def respond_photo_received(message: types.Message, state: FSMContext):
    if message.photo:
        photo_file_id = message.photo[-1].file_id
        data = await state.get_data()
        vacancy_id = data.get("vacancy_id")
        await send_application(message, message.from_user.id, vacancy_id, photo_file_id)
    else:
        await message.answer("Пожалуйста, отправьте фото или нажмите «Пропустить»")
        return
    await state.clear()

async def send_application(target, user_id: int, vacancy_id: str, photo_file_id: str = None):
    profile = get_subscriber_profile(user_id)
    if profile and photo_file_id:
        profile = dict(profile)
        profile["photo_file_id"] = photo_file_id
    vacancy_row = get_vacancy_row(vacancy_id)
    if not vacancy_row:
        await target.answer("❌ Вакансия не найдена")
        return
    vacancy_text, vacancy_link, source_chat, saved_contact, address = vacancy_row
    add_response(user_id, vacancy_id, vacancy_text[:200] if vacancy_text else None, vacancy_link, photo_file_id)
    candidate_questionnaire = profile.get('questionnaire') or f"""📝 *АНКЕТА КАНДИДАТА*

👤 *ФИО:* {profile['full_name']}
🎂 *Возраст:* {profile['age']} лет
📞 *Телефон:* {profile['phone']}
🆔 *Telegram:* @{profile['username'] if profile['username'] else 'нет'}
"""
    employer_contact = saved_contact
    if not employer_contact:
        employer_contact = extract_contact_from_text(vacancy_text)
    if employer_contact:
        try:
            msg = f"🔔 *Новый отклик на вакансию!*\n\n📢 Вакансия из канала: {source_chat}\n\n{candidate_questionnaire}\n\n🔗 Ссылка на сообщение: {vacancy_link}"
            if photo_file_id or profile.get("photo_storage_path"):
                await send_profile_photo(
                    bot, employer_contact, profile, caption=msg, parse_mode="Markdown",
                )
            else:
                await bot.send_message(employer_contact, msg, parse_mode="Markdown", disable_web_page_preview=True)
            await target.answer("✅ Ваша анкета отправлена работодателю!", show_alert=True)
            await bot.send_message(YOUR_USER_ID, f"✅ *Анкета отправлена заказчику!*\n\n👤 Кандидат: {profile['full_name']}\n📢 Вакансия: {source_chat}\n👨‍💼 Контакт: {employer_contact}", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Не удалось отправить заказчику {employer_contact}: {e}")
            await send_to_admin(target, profile, vacancy_row, candidate_questionnaire, photo_file_id)
    else:
        await send_to_admin(target, profile, vacancy_row, candidate_questionnaire, photo_file_id)

async def send_to_admin(target, profile: dict, vacancy_row: tuple, candidate_questionnaire: str, photo_file_id: str = None):
    vacancy_text, vacancy_link, source_chat, _, address = vacancy_row
    user_link = f"[{profile['full_name']}](tg://user?id={profile['user_id']})"
    admin_message = (
        f"🔔 *НОВЫЙ ОТКЛИК НА ВАКАНСИЮ!*\n\n"
        f"📢 Источник: {source_chat}\n"
        f"🔗 Ссылка: {vacancy_link}\n\n"
        f"👤 *Кандидат:*\n"
        f"• ФИО: {profile['full_name']}\n"
        f"• Возраст: {profile['age']} лет\n"
        f"• Телефон: {profile['phone']}\n"
        f"• Username: @{profile['username'] if profile['username'] else 'нет'}\n\n"
        f"📞 Свяжитесь с кандидатом: {user_link}\n\n"
        f"⚠️ *Контакт заказчика не найден!* Перешлите анкету вручную."
    )
    if photo_file_id or profile.get("photo_storage_path"):
        await send_profile_photo(
            bot, YOUR_USER_ID, profile,
            caption=admin_message, parse_mode="MarkdownV2", disable_web_page_preview=True,
        )
    else:
        await bot.send_message(YOUR_USER_ID, admin_message, parse_mode="MarkdownV2", disable_web_page_preview=True)
    await target.answer("✅ Отклик отправлен администратору! Он свяжется с вами.", show_alert=True)

@dp.callback_query(lambda c: c.data == "already_responded")
async def already_responded(callback: types.CallbackQuery):
    await callback.answer("Вы уже откликались на эту вакансию", show_alert=True)


# ========== АДМИНСКИЕ КОМАНДЫ ==========

@dp.message(Command("admin"))
async def admin_menu(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await bot.send_chat_action(message.chat.id, "typing")
    text = await run_db(build_admin_dashboard_text)
    await message.answer(
        f"{text}\n\n📖 Справка — «Как пользоваться» или /help",
        parse_mode="Markdown",
        reply_markup=get_admin_keyboard(),
    )

@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    stats = get_admin_stats()
    parser = get_parser_status_snapshot()
    await message.answer(
        f"📊 *Статус бота*\n\n✅ Polling активен\n"
        f"Сборка: `{APP_BUILD}`\n"
        f"{format_parser_status_line(parser)}\n\n"
        f"👥 Подписчиков: {stats['subscribers']} | 💬 Откликов: {stats['responses']}",
        parse_mode="Markdown",
        reply_markup=get_admin_keyboard(),
    )

@dp.message(Command("check_now"))
async def check_now_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    status_msg = await message.answer("🔍 Начинаю проверку...")
    try:
        orders, closed_data = await run_parser()
        if not orders and not closed_data:
            await status_msg.edit_text("✅ Новых вакансий не найдено.")
            return
        if closed_data:
            await notify_closed_vacancies(closed_data)
        for order in orders:
            await send_vacancy_to_subscribers(order)
        await status_msg.edit_text(f"✅ Проверка завершена. Найдено вакансий: {len(orders)}. Уведомлений о закрытии: {len(closed_data)}")
    except Exception as e:
        logger.error(f"Ошибка в check_now_cmd: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")

@dp.message(Command("debug_last"))
async def debug_last_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await message.answer(get_last_debug_report(), parse_mode="Markdown")

@dp.message(Command("usercards"))
async def usercards_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await show_admin_user_cards(message, page=0)

@dp.message(Command("catmap"))
async def catmap_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    mapping = get_user_category_mapping()
    if not mapping:
        await message.answer("📭 Маппинг пуст.")
        return
    lines = ["🧭 *Маппинг категорий (активные подписчики):*"]
    for row in mapping:
        lines.append(f"• {row['emoji']} {row['name']} (`{row['code']}`): *{row['subscribers_count']}*")
    await message.answer("\n".join(lines), parse_mode="Markdown")

@dp.message(Command("myid"))
async def show_my_id(message: types.Message):
    await message.answer(
        f"📌 Ваш Telegram ID: `{message.from_user.id}`\n"
        f"ID в .env: `{YOUR_USER_ID}`\n"
        f"{'✅ Совпадает' if message.from_user.id == YOUR_USER_ID else '❌ НЕ СОВПАДАЕТ! Обновите .env'}"
    )

@dp.message(Command("broadcast"))
async def broadcast_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "📢 Введите текст рассылки или нажмите кнопку «📢 Рассылка» в меню.",
            reply_markup=get_admin_keyboard(),
        )
        await state.set_state(BroadcastState.waiting_for_text)
        return
    status_msg = await message.answer(f"📢 Подготовка рассылки...")
    sent, failed, err = await run_broadcast(message.chat.id, parts[1], status_msg)
    if err:
        await status_msg.edit_text(err)
    else:
        await status_msg.edit_text(f"✅ Отправлено {sent} из {sent + failed} (ошибок: {failed})")

@dp.message(BroadcastState.waiting_for_text)
async def broadcast_text_received(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    if message.text in ADMIN_MENU_BUTTONS:
        await state.clear()
        return
    preview = message.text[:500]
    await state.update_data(broadcast_text=message.text)
    await message.answer(
        f"📢 *Предпросмотр рассылки:*\n\n{escape_markdown(preview)}"
        + ("…" if len(message.text) > 500 else "")
        + "\n\nОтправить всем подписчикам?",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Отправить", callback_data="broadcast_confirm"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel"),
                ]
            ]
        ),
    )

@dp.callback_query(lambda c: c.data == "broadcast_confirm")
async def broadcast_confirm(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        await callback.answer("Текст не найден", show_alert=True)
        return
    await state.clear()
    await callback.answer("Рассылка запущена")
    status_msg = await callback.message.edit_text("📢 Рассылка началась...")
    sent, failed, err = await run_broadcast(callback.from_user.id, text, status_msg)
    if err:
        await status_msg.edit_text(err)
    else:
        await status_msg.edit_text(
            f"✅ Рассылка завершена.\nОтправлено: {sent}\nОшибок: {failed}",
            reply_markup=None,
        )

@dp.callback_query(lambda c: c.data == "broadcast_cancel")
async def broadcast_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена.")
    await callback.answer()

@dp.message(Command("clean_old"))
async def clean_old_vacancies(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM vacancies WHERE found_at < {now_minus_days(3)}")
        cur.execute(f"DELETE FROM processed_messages WHERE processed_at < {now_minus_days(3)}")
    await message.answer("✅ Удалены вакансии и обработанные сообщения старше 3 дней.")

@dp.message(Command("addchat"))
async def add_chat_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    await message.answer("➕ *Добавление нового чата для парсинга*\n\nВведите ссылку на чат (например, https://t.me/chatname):", parse_mode="Markdown")
    await state.set_state(AddChatState.waiting_for_link)

@dp.message(AddChatState.waiting_for_link)
async def process_add_chat(message: types.Message, state: FSMContext):
    if message.from_user.id == YOUR_USER_ID and message.text in ADMIN_MENU_BUTTONS:
        await state.clear()
        return
    chat_link = normalize_chat_link(message.text)
    if not chat_link:
        await message.answer("❌ Неверный формат. Отправьте @username, username или ссылку t.me/...")
        return
    if add_target_chat(chat_link):
        await message.answer(
            f"✅ Чат {chat_link} добавлен для парсинга.\n"
            "Новые сообщения будут подхватываться автоматически.",
            reply_markup=get_admin_keyboard(),
        )
    else:
        await message.answer(f"⚠️ Чат {chat_link} уже существует.", reply_markup=get_admin_keyboard())
    await state.clear()

@dp.message(Command("removechat"))
async def remove_chat_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("❌ Использование: `/removechat https://t.me/chatname`", parse_mode="Markdown")
        return
    chat_link = parts[1]
    remove_target_chat(chat_link)
    await message.answer(f"🗑️ Чат {chat_link} удалён из парсинга.")

@dp.message(Command("listchats"))
@dp.message(Command("chats"))
async def list_chats_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    status_msg = await message.answer("🔍 Проверяю доступ к чатам… (до ~1 мин)")
    try:
        chats, parser_status = await inspect_parser_chats()
        report = format_parser_chats_report(chats, parser_status)
        if len(report) > 4000:
            await status_msg.edit_text(report[:4000], parse_mode="Markdown")
            await message.answer(report[4000:], parse_mode="Markdown")
        else:
            await status_msg.edit_text(report, parse_mode="Markdown")
    except Exception as e:
        logger.exception("list_chats_cmd")
        await status_msg.edit_text(f"❌ Ошибка проверки чатов: {str(e)[:120]}")

@dp.message(Command("postvacancy"))
async def post_vacancy_cmd(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    await message.answer(
        "📤 *Отправка вакансии подписчикам*\n\nВыберите категорию:",
        parse_mode="Markdown",
        reply_markup=get_postvacancy_categories_keyboard(),
    )
    await state.set_state(PostVacancyState.waiting_for_category)

@dp.callback_query(lambda c: c.data and c.data.startswith("postcat_"))
async def post_vacancy_category(callback: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state != PostVacancyState.waiting_for_category:
        await callback.answer("❌ Сначала выполните команду /postvacancy", show_alert=True)
        return

    category_code = callback.data.replace("postcat_", "")
    all_cats = get_all_categories()
    cat_name = next((cat['name'] for cat in all_cats if cat['code'] == category_code), category_code)
    await state.update_data(category_code=category_code, category_name=cat_name)
    await callback.message.answer(f"📝 Введите текст вакансии (категория: {cat_name}):")
    await state.set_state(PostVacancyState.waiting_for_text)
    await callback.answer()

@dp.message(PostVacancyState.waiting_for_text)
async def post_vacancy_text(message: types.Message, state: FSMContext):
    if message.from_user.id == YOUR_USER_ID and message.text in ADMIN_MENU_BUTTONS:
        await state.clear()
        return
    await state.update_data(message_text=message.text)
    await message.answer("🖼️ Отправьте фото для вакансии (необязательно) или нажмите «Пропустить»:", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⏩ Пропустить")]], resize_keyboard=True))
    await state.set_state(PostVacancyState.waiting_for_photo)

@dp.message(PostVacancyState.waiting_for_photo)
async def post_vacancy_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    category_code = data['category_code']
    category_name = data['category_name']
    vacancy_text = data['message_text']
    photo_file_id = None
    if message.photo:
        photo_file_id = message.photo[-1].file_id
    elif message.text == "⏩ Пропустить":
        pass
    else:
        await message.answer("Пожалуйста, отправьте фото или нажмите «Пропустить»")
        return
    subscribers = get_subscribers_by_category(category_code)
    if not subscribers:
        await message.answer(
            f"⚠️ Нет подписчиков на категорию {category_name}. Вакансия не отправлена.",
            reply_markup=get_admin_keyboard(),
        )
        await state.clear()
        return
    text = f"{get_category_emoji(category_code)} *Вакансия от администратора:*\n\n{escape_markdown(vacancy_text[:500])}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✋ Откликнуться", callback_data="admin_vacancy")]])
    sent = 0
    for sub in subscribers:
        try:
            if photo_file_id:
                await bot.send_photo(sub['user_id'], photo_file_id, caption=text, parse_mode="MarkdownV2", reply_markup=keyboard)
            else:
                await bot.send_message(sub['user_id'], text, parse_mode="MarkdownV2", reply_markup=keyboard)
            sent += 1
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.error(f"Ошибка отправки админ-вакансии {sub['user_id']}: {e}")
    await message.answer(
        f"✅ Вакансия отправлена {sent} подписчикам категории {category_name}.",
        reply_markup=get_admin_keyboard(),
    )
    await state.clear()


# ========== КНОПКИ АДМИН-МЕНЮ ==========

@dp.message(lambda m: m.text == "💬 Чаты парсинга")
@dp.message(lambda m: m.text == "📋 Список чатов парсинга")
async def admin_chats_button(message: types.Message):
    if message.from_user.id == YOUR_USER_ID:
        await list_chats_cmd(message)

@dp.message(lambda m: m.text == "📊 Статистика")
async def admin_stats_button(message: types.Message):
    if message.from_user.id == YOUR_USER_ID:
        await admin_menu(message)

@dp.message(lambda m: m.text == "🔍 Ручная проверка")
async def admin_check_button(message: types.Message):
    if message.from_user.id == YOUR_USER_ID:
        await check_now_cmd(message)

@dp.message(lambda m: m.text == "📋 Список откликов")
async def admin_responses_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    recent = get_recent_responses(10)
    if not recent:
        await message.answer("📭 Нет откликов.")
        return
    text = "📋 *Последние отклики:*\n\n"
    for resp in recent:
        time = escape_markdown(format_db_datetime_short(resp[0]))
        name = resp[3] or resp[2] or "Пользователь"
        preview = (resp[1][:50] + "...") if resp[1] and len(resp[1]) > 50 else (resp[1] or "—")
        text += f"• {time} — {escape_markdown(name)}: {escape_markdown(preview)}\n"
    await message.answer(text, parse_mode="MarkdownV2")

@dp.message(lambda m: m.text == "📝 Отчёт парсера")
async def admin_debug_button(message: types.Message):
    if message.from_user.id == YOUR_USER_ID:
        await debug_last_cmd(message)

@dp.message(lambda m: m.text == "👥 Список подписчиков")
async def admin_subscribers_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await bot.send_chat_action(message.chat.id, "typing")
    subs = await run_db(get_subscribers_display, 20)
    if not subs:
        await message.answer("📭 Нет подписчиков.")
        return
    text = "👥 *Список подписчиков:*\n\n"
    for i, row in enumerate(subs, 1):
        text += f"{i}. {row['name']}\n"
    total = await run_db(lambda: len(get_all_subscribers()))
    if total > len(subs):
        text += f"\n... всего активных: {total}"
    await message.answer(text, parse_mode="Markdown")

@dp.message(lambda m: m.text == "📢 Рассылка")
async def admin_broadcast_button(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    await message.answer(
        "📢 *Рассылка всем подписчикам*\n\nВведите текст одним сообщением:",
        parse_mode="Markdown",
    )
    await state.set_state(BroadcastState.waiting_for_text)

async def show_admin_user_cards(message: types.Message, page: int = 0, edit: bool = False):
    limit = 5
    offset = page * limit
    if not edit:
        await bot.send_chat_action(message.chat.id, "typing")
    cards = await run_db(get_subscriber_cards, limit, offset)
    if not cards:
        text = "📭 Карточек больше нет."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return
    total_subs = await run_db(lambda: len(get_all_subscribers()))
    pages_total = max(1, (total_subs + limit - 1) // limit)
    lines = [f"🗂️ *Карточки пользователей* (страница {page + 1}/{pages_total})"]
    for i, card in enumerate(cards, 1):
        lines.append("")
        lines.append(format_user_card(card, idx=offset + i))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_cards_{page-1}"))
    if page + 1 < pages_total:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin_cards_{page+1}"))
    markup = InlineKeyboardMarkup(inline_keyboard=[nav]) if nav else None
    body = "\n".join(lines)
    if edit:
        try:
            await message.edit_text(body, parse_mode="Markdown", reply_markup=markup)
        except Exception as e:
            if "message is not modified" not in str(e):
                logger.warning(f"Не удалось обновить карточки: {e}")
    else:
        await message.answer(body, parse_mode="Markdown", reply_markup=markup)

@dp.message(lambda m: m.text == "💎 Запросы Premium")
async def admin_premium_requests_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    requests = get_pending_premium_requests(30)
    if not requests:
        await message.answer("📭 Нет ожидающих запросов Premium.")
        return
    await message.answer(f"💳 *Запросы Premium* — {len(requests)} в очереди:", parse_mode="Markdown")
    for req in requests:
        caption = format_premium_request_admin_caption(req)
        markup = premium_request_admin_keyboard(req["id"])
        file_id = req.get("receipt_file_id")
        kind = req.get("receipt_kind")
        try:
            if file_id and kind == "photo":
                await bot.send_photo(
                    message.chat.id, file_id, caption=caption,
                    parse_mode="Markdown", reply_markup=markup,
                )
            elif file_id and kind == "document":
                await bot.send_document(
                    message.chat.id, file_id, caption=caption,
                    parse_mode="Markdown", reply_markup=markup,
                )
            else:
                await message.answer(caption, parse_mode="Markdown", reply_markup=markup)
        except Exception as e:
            logger.warning(f"admin premium request card #{req['id']}: {e}")


@dp.message(lambda m: m.text == "🗂️ Карточки пользователей")
async def admin_user_cards_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await show_admin_user_cards(message, page=0)

@dp.message(lambda m: m.text == "🧭 Маппинг категорий")
async def admin_category_mapping_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await catmap_cmd(message)

@dp.callback_query(lambda c: c.data and c.data.startswith("admin_cards_"))
async def admin_cards_page_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    page = int(callback.data.split("_")[2])
    await show_admin_user_cards(callback.message, page=page, edit=True)
    await callback.answer()

@dp.message(lambda m: m.text == "⚠️ Жалобы")
async def admin_complaints_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    complaints = get_recent_complaints(20)
    if not complaints:
        await message.answer("📭 Нет жалоб.")
        return
    text = "⚠️ *Новые жалобы:*\n\n"
    for c in complaints:
        text += f"ID: {c[0]}, от {c[2]} (user {c[1]})\nВакансия: {c[3]}\nПричина: {c[4]}\nТекст: {c[5] or '—'}\nДата: {c[6]}\n\n"
    await send_long_message(message.chat.id, text, parse_mode="Markdown")

@dp.message(lambda m: m.text == "❓ Поддержка (админ)")
async def admin_support_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    requests = get_unanswered_support_requests(20)
    if not requests:
        await message.answer("📭 Нет новых обращений в поддержку.")
        return
    text = "❓ *Новые обращения:*\n\n"
    for req in requests:
        text += f"ID: {req[0]}, от {req[2] or req[1]} (user {req[1]})\nСообщение: {req[3]}\nДата: {req[4]}\n\n"
    await send_long_message(message.chat.id, text, parse_mode="Markdown")
    await message.answer("Чтобы ответить, используйте команду `/answer ID_обращения ответ`")

@dp.message(Command("answer"))
async def answer_support(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("❌ Использование: `/answer ID_обращения текст_ответа`", parse_mode="Markdown")
        return
    try:
        req_id = int(parts[1])
        answer_text = parts[2]
    except:
        await message.answer("❌ Неверный ID обращения")
        return
    row = fetchone(
        f"SELECT user_id FROM support_requests WHERE id = ? AND answered = {bool_false()}",
        (req_id,),
    )
    if not row:
        await message.answer("❌ Обращение не найдено или уже отвечено.")
        return
    user_id = row[0]
    mark_support_answered(req_id, answer_text)
    try:
        await bot.send_message(user_id, f"📞 *Ответ от администратора:*\n\n{answer_text}", parse_mode="Markdown")
        await message.answer(f"✅ Ответ отправлен пользователю {user_id}")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить ответ: {e}")

@dp.message(lambda m: m.text == "➕ Добавить чат")
async def admin_add_chat_button(message: types.Message, state: FSMContext):
    if message.from_user.id == YOUR_USER_ID:
        await add_chat_cmd(message, state)

@dp.message(lambda m: m.text == "📤 Отправить вакансию")
async def admin_post_vacancy_button(message: types.Message, state: FSMContext):
    if message.from_user.id == YOUR_USER_ID:
        await post_vacancy_cmd(message, state)

@dp.message(lambda m: m.text == "❌ Закрыть меню")
async def admin_close_menu(message: types.Message):
    if message.from_user.id == YOUR_USER_ID:
        await message.answer("Меню закрыто. Для открытия напишите /start", reply_markup=ReplyKeyboardRemove())


# ========== ЗАПУСК И ОСТАНОВКА ==========

async def on_startup():
    logger.info("🚀 Запуск бота... (сборка %s)", APP_BUILD)
    init_db()
    migrated = migrate_legacy_vacancy_ids()
    if migrated:
        logger.info(f"🔄 Миграция ID вакансий: обновлено {migrated} записей")
    logger.info("📁 База данных инициализирована")

    from config import get_shared_dir
    from parser import session_file_path

    shared = get_shared_dir()
    if shared:
        logger.info(f"📂 Shared volume: {shared}")
    session_path = session_file_path()
    logger.info(f"📎 Telethon session: {session_path}")
    if shared and session_path.startswith(shared) and os.path.isfile(session_path):
        logger.info("✅ Сессия в shared — переживёт git-deploy")
    elif not os.path.isfile(session_path):
        logger.warning(
            "⚠️ Файл сессии не найден — парсер не стартует. "
            "Загрузите user_session.session в /app/shared на Bothost."
        )

    # Парсер групп (Telethon) — reconnect, health alerts, session lock
    spawn_background_task(
        start_realtime_listener(
            schedule_vacancy_push,
            notify_closed_vacancies,
            health_notify_callback=notify_admin_parser_issue,
        )
    )
    logger.info(f"📡 {PARSER_LABEL} запущен (резервный опрос каждые 5 мин)")

    logger.info("📷 Фото профилей: %s", get_user_photos_dir())

    async def notify_photo_issue(user_id: int, text: str):
        try:
            await bot.send_message(user_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.warning("notify_photo_issue user=%s: %s", user_id, e)

    spawn_background_task(photo_health_loop(bot, notify_photo_issue))

    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="🏠 Главное меню"),
            BotCommand(command="help", description="📖 Как пользоваться"),
        ])
        logger.info("✅ Меню команд Telegram: /start, /help")
    except Exception as e:
        logger.warning(f"Не удалось set_my_commands: {e}")

    logger.info("📡 Запуск polling...")
    await dp.start_polling(bot)

async def on_shutdown():
    logger.info("🛑 Остановка бота...")
    await stop_realtime_listener()
    from session_lock import release_session_lock
    release_session_lock()
    await bot.session.close()
    logger.info("👋 Бот остановлен")

async def main():
    try:
        await on_startup()
    except KeyboardInterrupt:
        logger.info("⚠️ Получен сигнал остановки")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
    finally:
        await on_shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен пользователем")
