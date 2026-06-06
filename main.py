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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, BotCommand, BufferedInputFile, LabeledPrice, ChatMemberUpdated
from aiogram import F
from db import *
from db_backend import db_conn, fetchone, now_minus_days, bool_false, run_db
from parser import (
    run_parser, get_last_debug_report, detect_category, extract_contact_from_text,
    start_realtime_listener, stop_realtime_listener, get_new_messages, extract_address_from_text,
    make_vacancy_id, PARSER_LABEL, inspect_parser_chats, format_parser_chats_report,
    get_parser_status_snapshot, format_parser_status_line, vacancy_matches_user_metro,
    spawn_background_task, vacancy_matches_category, evaluate_vacancy,
    resolve_vacancy_contact, build_vacancy_dedupe_key,
    format_chat_noise_report, parser_scan_in_progress,
)
from admin_exports import (
    build_subscribers_xlsx, build_vacancies_xlsx, build_employers_xlsx,
    build_notfit_xlsx, export_filename,
)
from config import (
    BOT_TOKEN, YOUR_USER_ID, SUBSCRIPTION_PAY_URL, SUBSCRIPTION_SUPPORT,
    SUBSCRIPTION_PRICE_RUB, SUBSCRIPTION_CARD_HINT, TRIAL_DAYS, VACANCY_MAX_AGE_HOURS,
    FEED_FRESH_HOURS, FEED_ARCHIVE_MAX_HOURS,
    FORUM_TOPICS_ENABLED, CHANNEL_CROSSPOST_ENABLED, HUNTER_CHANNEL_ID,
    LLM_ENABLED, LLM_DAILY_LIMIT_PREMIUM, STARS_ENABLED, STARS_EXTENDED_RESPONSE_PRICE,
)
from profile_photos import (
    get_user_photos_dir, persist_user_photo, photo_health_loop, send_profile_photo,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Метка сборки — в логах и /status, чтобы проверить деплой на Bothost.
APP_BUILD = os.getenv("APP_BUILD", "tool-v4")


def configure_third_party_logging():
    """Telethon на INFO сыпет «Got difference…» — в проде оставляем WARNING+."""
    for name in (
        "telethon",
        "telethon.client",
        "telethon.client.updates",
        "telethon.network",
        "aiogram",
        "aiohttp",
        "httpx",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


configure_third_party_logging()

BTN_EMPLOYER_POST = "📤 Разместить вакансию"
BTN_SWITCH_CANDIDATE = "👷 Режим исполнителя"

def build_role_picker_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👷 Ищу работу", callback_data="role_candidate"),
            InlineKeyboardButton(text="🏢 Ищу персонал", callback_data="role_employer"),
        ],
    ])


def poster_from_tg_user(user: types.User) -> dict:
    display = " ".join(p for p in (user.first_name, user.last_name) if p).strip()
    return {
        "user_id": user.id,
        "username": user.username,
        "display_name": display or user.first_name,
    }


def poster_from_order(order: dict) -> dict | None:
    if order.get("poster"):
        return order["poster"]
    uid = order.get("poster_user_id")
    uname = order.get("poster_username")
    if uid or uname:
        return {"user_id": uid, "username": uname}
    contact = (order.get("author_contact") or "").strip()
    if contact.startswith("tg://user?id="):
        try:
            raw = contact.split("id=")[1].split("&")[0]
            return {"user_id": int(raw)}
        except (ValueError, IndexError):
            pass
    if contact.startswith("@"):
        return {"username": contact[1:]}
    return None


async def publish_employer_vacancy(user: types.User, category_code: str, vacancy_text: str) -> tuple[bool, str]:
    """Публикация заказчиком → модерация, push после одобрения админом."""
    poster = poster_from_tg_user(user)
    profile = get_subscriber_profile(user.id)
    phone = (profile or {}).get("phone")
    body = vacancy_text.strip()
    if phone and phone not in body:
        body = f"{body}\n\n📞 {phone}"

    accepted, cat, reason, _ = evaluate_vacancy(body, poster, force_category=category_code)
    if not accepted or cat != category_code:
        hints = {
            "no_hiring": "Укажите, кого ищете (промоутер, хелпер и т.д.).",
            "no_payment": "Укажите оплату (ставка, ₽/час, сумма).",
            "no_contact": "Добавьте @username или телефон — или войдите с аккаунтом с username.",
            "ambiguous_category": "Текст не совпадает с выбранной категорией — уточните роль.",
        }
        base = hints.get(reason.split(":")[0], reason)
        if reason.startswith("quality_gate"):
            base = f"Текст не подходит под категорию «{get_category_name(category_code)}». Уточните роль и задачи."
        return False, base

    author_contact, contact_source = resolve_vacancy_contact(body, poster)
    address = extract_address_from_text(body)
    dedupe_key = build_vacancy_dedupe_key(body, author_contact)
    ts = str(int(datetime.now().timestamp()))
    vacancy_id = make_vacancy_id(f"employer_{user.id}", ts, dedupe_key)
    published_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    display_name = poster.get("display_name")

    employer_id = upsert_employer_from_post(
        telegram_user_id=user.id,
        username=user.username,
        display_name=display_name,
        contact_text=author_contact,
        contact_source=contact_source,
        category_code=category_code,
        bot_user_id=user.id,
    )
    save_vacancy(
        vacancy_id,
        f"employer_{user.id}",
        "PromoStaff Hunter · заказчик",
        category_code,
        body[:2000],
        None,
        author_contact,
        address,
        False,
        dedupe_key,
        published_at,
        user.id,
        user.username,
        display_name,
        contact_source,
        employer_id,
        user.id,
        "pending",
    )
    return True, vacancy_id


async def notify_admin_moderation(vacancy_id: str, category_code: str, preview: str, employer_user_id: int):
    if not YOUR_USER_ID:
        return
    cat_name = get_category_name(category_code)
    text = (
        f"📝 *Модерация вакансии заказчика*\n\n"
        f"ID: `{vacancy_id}`\n"
        f"Категория: {cat_name}\n"
        f"Заказчик user_id: `{employer_user_id}`\n\n"
        f"{escape_markdown(preview[:400])}"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"mod_ok_{vacancy_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"mod_no_{vacancy_id}"),
        ],
    ])
    try:
        await bot.send_message(YOUR_USER_ID, text, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        logger.warning("notify_admin_moderation: %s", e)


async def channel_post_for_vacancy(vacancy_id: str, *, force: bool = False) -> str:
    """Кросс-пост превью в @promostaff_agency_job. Возвращает текст для админа."""
    if not CHANNEL_CROSSPOST_ENABLED or not HUNTER_CHANNEL_ID:
        return (
            "❌ Канал не настроен. Задайте `CHANNEL_CROSSPOST_ENABLED=1` "
            "и `HUNTER_CHANNEL_ID` на Bothost."
        )
    row = get_vacancy_push_row(vacancy_id)
    if not row:
        return f"❌ Вакансия `{vacancy_id}` не найдена."
    category_code = row[5] or "promoter"
    body = row[0] or ""
    from services.channel_post import post_vacancy_preview_to_channel
    from services.channel_policy import evaluate_channel_crosspost, format_skip_reason

    ok = await post_vacancy_preview_to_channel(
        bot,
        vacancy_id=vacancy_id,
        category_code=category_code,
        category_name=get_category_name(category_code),
        category_emoji=get_category_emoji(category_code),
        body=body,
        source=row[2] or "—",
        freshness=get_freshness_label(row[8]),
        force=force,
    )
    if ok:
        return f"✅ Опубликовано в канал: `{vacancy_id}`"
    already = is_vacancy_channel_posted(vacancy_id) and not force
    allowed, reason = evaluate_channel_crosspost(
        category_code, body, force=force, already_posted=already,
    )
    if not allowed:
        return f"⏸ Не опубликовано: {format_skip_reason(reason)}"
    return "❌ Не удалось опубликовать. Проверьте права бота в канале."


def build_order_from_vacancy_row(vacancy_id: str, row) -> dict | None:
    if not row:
        return None
    return {
        "vacancy_id": vacancy_id,
        "chat_title": row[2] or "PromoStaff Hunter · заказчик",
        "message_text": row[0],
        "message_link": row[1],
        "category": row[5],
        "chat_id": row[6],
        "author_contact": row[3],
        "address": row[4],
        "dedupe_key": row[7],
        "published_at": row[8],
        "poster_user_id": row[9],
        "poster_username": row[10],
        "from_bot_employer": True,
    }

BTN_SETTINGS = "⚙️ Настройки"
BTN_METRO = "📍 Станции метро"
BTN_SETTINGS_CATEGORIES = "📌 Категории вакансий"
BTN_SETTINGS_BACK = "◀️ В главное меню"
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


def greeting_display_name(profile: dict | None, user: types.User) -> str:
    """Имя для приветствия: ФИО из анкеты, иначе имя из Telegram."""
    if profile and (profile.get("full_name") or "").strip():
        return profile["full_name"].strip()
    parts = [p for p in (user.first_name, user.last_name) if p]
    if parts:
        return " ".join(parts)
    if user.username:
        return user.username
    return "друг"


def build_maps_url(address: str) -> str | None:
    if not address or not address.strip():
        return None
    url = f"https://yandex.ru/maps/?text={quote(address.strip())}"
    if len(url) > 2048 or not url.startswith("https://"):
        return None
    return url


def _inline_btn(
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    style: str | None = None,
) -> InlineKeyboardButton:
    kwargs: dict = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style is not None:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


NOTFIT_REASONS = {
    "wrong_category": "Не та категория / роль",
    "low_pay": "Мало платят",
    "wrong_area": "Не мой район / далеко",
    "spam": "Спам или не вакансия",
    "duplicate": "Уже видел / повтор",
    "other": "Другое (напишу)",
}


def build_notfit_reason_keyboard(vacancy_id: str) -> InlineKeyboardMarkup:
    rows = [
        [_inline_btn(label, callback_data=f"nfr:{code}:{vacancy_id}")]
        for code, label in NOTFIT_REASONS.items()
    ]
    rows.append([_inline_btn("Отмена", callback_data=f"notfit_cancel:{vacancy_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_vacancy_keyboard(vacancy_id: str, address: str | None = None) -> InlineKeyboardMarkup:
    """Inline-кнопки с цветами Bot API 9.4 (style на InlineKeyboardButton)."""
    buttons = [[_inline_btn("Откликнуться", callback_data=f"respond_{vacancy_id}", style="success")]]
    maps_url = build_maps_url(address) if address else None
    if maps_url:
        buttons.append([_inline_btn("На карте", url=maps_url, style="primary")])
    buttons.append([
        _inline_btn("Не подходит", callback_data=f"notfit_{vacancy_id}"),
        _inline_btn("Жалоба", callback_data=f"complain_{vacancy_id}", style="danger"),
    ])
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
    del published_at, source, message_link  # публичная карточка — без источника и ссылки на чужую группу
    from services.vacancy_public_text import sanitize_vacancy_public_body

    description = sanitize_vacancy_public_body(body or "", max_len=500)
    if not description:
        description = "Откройте карточку в боте — там кнопка «Отклик»."
    return (
        f"{category_emoji} <b>{escape_html(category_name)}</b> · {escape_html(freshness)}\n\n"
        f"{escape_html(description)}"
    )


async def send_vacancy_card(
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    topic_key: str | None = "vacancies",
):
    """HTML-карточка вакансии с fallback без разметки."""
    extra: dict = {}
    if topic_key and FORUM_TOPICS_ENABLED:
        if not get_user_topic_thread_id(chat_id, topic_key):
            await setup_forum_topics_for_user(chat_id)
        from services.forum_topics import topic_message_kwargs
        extra = topic_message_kwargs(chat_id, topic_key)
    try:
        await send_message_with_retry(
            chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
            **extra,
        )
    except TelegramBadRequest as e:
        err = str(e).lower()
        if extra.get("message_thread_id") and ("thread" in err or "topic" in err or "not found" in err):
            logger.warning("send_vacancy_card: тема недоступна user=%s, fallback в общий чат", chat_id)
            extra = {}
            try:
                await send_message_with_retry(
                    chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                return
            except TelegramBadRequest:
                pass
        if "parse" in err:
            plain = re.sub(r"<[^>]*>", "", text)
            await send_message_with_retry(
                chat_id,
                text=plain,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
                **extra,
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
            "• фильтр по метро (⚙️ Настройки → 📍 Станции метро)\n\n"
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


async def setup_forum_topics_for_user(user_id: int):
    if not FORUM_TOPICS_ENABLED:
        return
    try:
        from services.forum_topics import ensure_user_forum_topics
        await ensure_user_forum_topics(bot, user_id)
    except Exception as e:
        logger.warning("forum topics setup user=%s: %s", user_id, e)


async def send_user_message(user_id: int, *, topic_key: str | None = None, **kwargs):
    """send_message в тему лички (или general без topic_key)."""
    extra: dict = {}
    if topic_key and FORUM_TOPICS_ENABLED:
        if not get_user_topic_thread_id(user_id, topic_key):
            await setup_forum_topics_for_user(user_id)
        from services.forum_topics import topic_message_kwargs
        extra = topic_message_kwargs(user_id, topic_key)
    return await send_message_with_retry(user_id, **extra, **kwargs)


async def show_vacancy_by_deeplink(message: types.Message, user_id: int, vacancy_id: str):
    row = get_vacancy_push_row(vacancy_id)
    if not row:
        await message.answer("❌ Вакансия не найдена или уже снята.")
        return
    if row[11] not in (None, "approved"):
        await message.answer("⏳ Вакансия на модерации.")
        return
    msg_text, message_link, source_title, _contact, address = row[0], row[1], row[2], row[3], row[4]
    category_code = row[5] or "promoter"
    published_raw = row[8]
    text = format_vacancy_card_html(
        category_emoji=get_category_emoji(category_code),
        category_name=get_category_name(category_code),
        freshness=get_freshness_label(published_raw),
        published_at=format_publication_time(published_raw),
        body=msg_text or "",
        source=source_title or row[6] or "—",
        message_link=message_link,
    )
    keyboard = build_vacancy_keyboard(vacancy_id, address)
    await send_vacancy_card(user_id, text, reply_markup=keyboard)

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


async def answer_admin_report(message: types.Message, text: str):
    """Отчёт админу: Markdown, при ошибке разметки — plain text."""
    try:
        if len(text) > 3800:
            await send_long_message(message.chat.id, text, parse_mode="Markdown")
        else:
            await message.answer(text, parse_mode="Markdown", disable_web_page_preview=True)
    except TelegramBadRequest as e:
        logger.warning("answer_admin_report Markdown failed: %s", e)
        plain = re.sub(r"[*_`]", "", text)
        if len(plain) > 3800:
            await send_long_message(message.chat.id, plain, parse_mode=None)
        else:
            await message.answer(plain, disable_web_page_preview=True)


async def admin_fsm_menu_escape(message: types.Message, state: FSMContext) -> bool:
    """Выход из FSM по кнопке админ-меню + сразу выполнить действие."""
    if message.from_user.id != YOUR_USER_ID or message.text not in ADMIN_MENU_BUTTONS:
        return False
    await state.clear()
    text = message.text
    if text == ADMIN_BTN_BACK:
        await message.answer("🏠 Главное админ-меню", reply_markup=get_admin_hub_keyboard())
    elif text == "📝 Отчёт парсера":
        await send_parser_debug_report(message)
    elif text == "📊 Шум по чатам":
        await send_chat_noise_report(message)
    elif text == "📡 Парсер":
        await message.answer(
            "📡 *Парсер* — чаты, прогон, качество ленты.",
            parse_mode="Markdown",
            reply_markup=get_admin_parser_keyboard(),
        )
    elif text == "📺 Канал":
        await message.answer(
            "📺 *Канал* — лимиты, промо, новости и статистика.",
            parse_mode="Markdown",
            reply_markup=get_admin_channel_keyboard(),
        )
    elif text == "✏️ Тексты промо":
        await message.answer("✏️ Редактор текстов автопромо.", reply_markup=get_admin_channel_keyboard())
        await send_promo_texts_admin_screen(message)
    else:
        await message.answer(
            f"Сценарий отменён. Нажмите «{text}» ещё раз, если нужно.",
            reply_markup=get_admin_hub_keyboard(),
        )
    return True


async def send_parser_debug_report(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await answer_admin_report(message, get_last_debug_report())


async def send_chat_noise_report(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    from parser import LAST_DEBUG_STATS, format_chat_noise_report
    await answer_admin_report(message, format_chat_noise_report(LAST_DEBUG_STATS))

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
        f"• Вакансий открытых: {stats['pending_vacancies']}\n"
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
                _mark_subscriber_blocked_if_needed(uid)
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
        f"• фильтр по метро/району (⚙️ Настройки → 📍 Станции метро)\n\n"
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

def get_postvacancy_categories_keyboard(prefix: str = "postcat"):
    categories = get_all_categories()
    buttons, row = [], []
    for i, cat in enumerate(categories):
        row.append(InlineKeyboardButton(
            text=f"{cat['emoji']} {cat['name']}", callback_data=f"{prefix}_{cat['code']}"
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


class EmployerRegState(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()


class EmployerPostState(StatesGroup):
    waiting_for_category = State()
    waiting_for_text = State()


class NotfitReasonState(StatesGroup):

    waiting_other = State()


class ChannelPostState(StatesGroup):
    waiting_vacancy_id = State()


class ChannelCustomPostState(StatesGroup):
    waiting_content = State()


class ChannelPromoTextState(StatesGroup):
    waiting_text = State()

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
        "«📍 Станции метро» в ⚙️ Настройках — push и лента только по выбранным станциям "
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
        "Меню разбито на разделы — «📡 Парсер», «👥 Пользователи», «📥 Excel». "
        "«◀️ Назад» возвращает в главное админ-меню.\n\n"
        "<b>📡 Парсер</b>\n"
        "• «🔍 Ручная проверка» или /check_now — прогон всех чатов\n"
        "• «📝 Отчёт парсера» — статистика последнего прогона\n"
        "• «📋 Список чатов парсинга» — доступ и мониторинг\n"
        "• «📊 Шум по чатам» — отсеяно vs в ленту по каждому чату\n"
        "• `/setchatroles ссылка promoter,helper` — ожидаемые роли чата\n\n"
        "<b>👥 Пользователи</b>\n"
        "• «🗂️ Карточки пользователей» — список и карточка с активностью\n"
        "• «💎 Запросы Premium» — чек + ✅/❌\n"
        "• /user USER_ID — открыть карточку по ID\n"
        "• /setplan USER_ID premium 30 — выдать Premium из чата\n\n"
        "<b>📥 Excel</b>\n"
        "• подписчики, вакансии, заказчики\n"
        "• «📥 Excel: не подходит» — feedback «👎 Не подходит» с причинами\n\n"
        "<b>📝 Модерация и канал</b>\n"
        "• «📺 Канал» — лимиты, промо, новости, статистика\n"
        "• «📣 В канал» — вакансия по ID (без лимитов)\n\n"
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
    name = (card.get("full_name") or "").strip()
    if not name:
        name = " ".join(p for p in (card.get("first_name"), card.get("last_name")) if p).strip()
    if not name:
        name = "—"
    username = f"@{card['username']}" if card.get("username") else "нет"
    cats = ", ".join(card.get("categories") or []) or "не выбраны"
    profile_note = ""
    if name == "—" or (not card.get("full_name") and not card.get("phone")):
        profile_note = "\n⚠️ Анкета не заполнена (зашли из канала / не завершили регистрацию)"
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
        f"Категории: {escape_markdown(cats)}{profile_note}"
    )


def _admin_user_short_label(profile: dict, max_len: int = 18) -> str:
    name = (profile.get("full_name") or "").strip()
    if not name:
        name = " ".join(p for p in (profile.get("first_name"), profile.get("last_name")) if p).strip()
    if not name and profile.get("username"):
        name = f"@{profile['username']}"
    if not name:
        name = str(profile.get("user_id") or "?")
    if len(name) > max_len:
        return name[: max_len - 1] + "…"
    return name


def build_admin_user_detail_html(user_id: int) -> str:
    profile = get_subscriber_profile(user_id)
    if not profile:
        return f"❌ Пользователь <code>{user_id}</code> не найден."
    name = _admin_user_short_label(profile, max_len=120)
    username = profile.get("username")
    uname_line = f"@{escape_html(username)}" if username else "—"
    role = profile.get("user_role") or "candidate"
    role_label = "🏢 Заказчик" if role == "employer" else "👷 Исполнитель"
    premium = is_user_premium(user_id)
    plan = profile.get("plan") or "free"
    if premium:
        until = format_db_date_short(profile.get("paid_until"))
        plan_line = f"💎 Premium до <b>{escape_html(until)}</b>" if until else "💎 Premium"
    elif plan == "premium":
        until = format_db_date_short(profile.get("paid_until"))
        plan_line = f"💎 Premium истёк ({escape_html(until)})" if until else "💎 Premium (истёк)"
    else:
        plan_line = "🆓 Free"
    active = bool(profile.get("is_active"))
    if active:
        status_line = "✅ Активен (бот не заблокирован)"
    else:
        status_line = "⛔ Неактивен (заблокировал бота или снят вручную)"
    cats = ", ".join(f"{c.get('emoji', '')} {c['name']}".strip() for c in get_user_categories(user_id)) or "—"
    metro = (profile.get("metro_zones") or "").strip() or "все локации"
    registered = format_db_datetime_short(get_subscriber_registered_at(user_id))
    resp_n = count_user_responses(user_id)
    push_n = count_user_sent_vacancies(user_id)
    notfit_n = count_user_notfit_feedback(user_id)
    trial = "да" if profile.get("trial_used") else "нет"
    lines = [
        f"<b>👤 {escape_html(name)}</b>",
        f"ID: <code>{user_id}</code>",
        f"Username: {uname_line}",
        f"Телефон: {escape_html(profile.get('phone') or '—')}",
        f"Роль: {role_label}",
        f"Тариф: {plan_line}",
        f"Статус: {status_line}",
        f"Регистрация: {escape_html(registered)}",
        f"Trial использован: {trial}",
        "",
        f"<b>Активность</b>",
        f"• Push-вакансий получено: <b>{push_n}</b>",
        f"• Откликов: <b>{resp_n}</b>",
        f"• «Не подходит»: <b>{notfit_n}</b>",
        "",
        f"<b>Настройки</b>",
        f"Категории: {escape_html(cats)}",
        f"Метро: {escape_html(metro)}",
    ]
    recent = get_user_responses(user_id, limit=3)
    if recent:
        lines.append("")
        lines.append("<b>Последние отклики</b>")
        for r in recent:
            when = format_db_datetime_short(r.get("responded_at"))
            closed = " 🔒" if r.get("is_closed") else ""
            snippet = (r.get("vacancy_text") or "—").replace("\n", " ")[:70]
            lines.append(f"• {escape_html(when)}{closed} — {escape_html(snippet)}")
    return "\n".join(lines)


def admin_user_detail_keyboard(user_id: int, profile: dict, cards_page: int = 0) -> InlineKeyboardMarkup:
    active = bool(profile.get("is_active"))
    p = cards_page
    rows = [
        [
            InlineKeyboardButton(
                text=f"💎 +{PREMIUM_DEFAULT_DAYS} дн.",
                callback_data=f"adm_p_{user_id}_{PREMIUM_DEFAULT_DAYS}_{p}",
            ),
            InlineKeyboardButton(text="💎 +90 дн.", callback_data=f"adm_p_{user_id}_90_{p}"),
        ],
        [InlineKeyboardButton(text="🆓 Снять Premium", callback_data=f"adm_f_{user_id}_{p}")],
        [
            InlineKeyboardButton(
                text="✅ Сделать активным" if not active else "⛔ Снять активность",
                callback_data=f"adm_a_{user_id}_{p}",
            ),
        ],
        [InlineKeyboardButton(text="📨 Все отклики", callback_data=f"adm_r_{user_id}_{p}")],
        [InlineKeyboardButton(text="◀️ К списку карточек", callback_data=f"admin_cards_{p}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_admin_user_detail(
    message: types.Message,
    user_id: int,
    cards_page: int = 0,
    edit: bool = False,
):
    profile = await run_db(get_subscriber_profile, user_id)
    if not profile:
        text = f"❌ Пользователь <code>{user_id}</code> не найден."
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К списку", callback_data=f"admin_cards_{cards_page}")],
        ])
        if edit:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await message.answer(text, parse_mode="HTML", reply_markup=markup)
        return
    text = build_admin_user_detail_html(user_id)
    markup = admin_user_detail_keyboard(user_id, profile, cards_page)
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


def _mark_subscriber_blocked_if_needed(user_id: int):
    profile = get_subscriber_profile(user_id)
    if profile and profile.get("is_active"):
        set_subscriber_active(user_id, False)
        logger.info(f"Пользователь {user_id} помечен неактивным (заблокировал бота)")


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
        now = datetime.now(timezone.utc)
        delta = now - published.astimezone(timezone.utc)
        minutes = delta.total_seconds() / 60
        hours = minutes / 60
        pub_msk = published.astimezone(MSK_TZ).date()
        today_msk = now.astimezone(MSK_TZ).date()
        if minutes <= 30:
            return "🟢 Свежая: только что"
        if hours <= 6:
            return "🟢 Свежая: несколько часов назад"
        if pub_msk == today_msk:
            return "🟢 Свежая: сегодня"
        if pub_msk == today_msk - timedelta(days=1):
            return "🟡 Вчера"
        days = (today_msk - pub_msk).days
        if days <= 7:
            return f"🟠 {days} дн. назад"
        return "🔴 Давно"
    except Exception:
        return "🟢 Актуальна"


def _vacancy_age_hours(vac: dict) -> float | None:
    raw = vac.get("published_at") or vac.get("found_at")
    dt = _coerce_db_datetime(raw)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600


def _vacancy_in_feed_mode(vac: dict, feed_mode: str) -> bool:
    age = _vacancy_age_hours(vac)
    if age is None:
        return feed_mode == "fresh"
    if feed_mode == "fresh":
        return age <= FEED_FRESH_HOURS
    return FEED_FRESH_HOURS < age <= FEED_ARCHIVE_MAX_HOURS


def _detected_category_display(text: str) -> tuple[str, str]:
    """Эмодзи и название по тексту поста (не только код в БД)."""
    code = detect_category(text or "")
    if not code:
        return "📌", "—"
    return get_category_emoji(code), get_category_name(code)

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

def build_contact_link(contact: str, text: str) -> str | None:
    """URL для inline-кнопки «Открыть чат». tg://user?id= Bot API отклоняет (BUTTON_USER_INVALID)."""
    if not contact:
        return None
    contact = contact.strip()
    if contact.startswith("@"):
        username = contact[1:].strip()
        if not re.fullmatch(r"[a-zA-Z0-9_]{5,32}", username):
            return None
        return f"https://t.me/{username}?text={quote(text)}"
    if contact.startswith("tg://user?id="):
        return None
    if contact.startswith("https://t.me/") or contact.startswith("http://t.me/"):
        base = contact.split("?", 1)[0]
        return f"{base}?text={quote(text)}"
    if contact.startswith("https://wa.me/") or contact.startswith("http://wa.me/"):
        return contact
    digits = re.sub(r"\D", "", contact)
    if digits:
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if len(digits) == 11 and digits.startswith("7"):
            return f"tel:+{digits}"
    return None


def manual_contact_hint(contact: str | None, draft_text: str) -> str:
    """Подсказка, если кнопку deeplink к заказчику собрать нельзя."""
    if not contact:
        return ""
    if contact.startswith("tg://user?id="):
        uid = contact.split("=", 1)[-1].split("&", 1)[0]
        return (
            f"\n\n_Контакт без @username (ID `{uid}`). Telegram не даёт кнопку «Открыть чат» — "
            f"скопируйте черновик ниже и найдите заказчика вручную._\n\n"
            f"```\n{draft_text}\n```"
        )
    if contact.startswith("@"):
        return (
            f"\n\nЕсли кнопка не открылась — напишите {escape_markdown(contact)} вручную "
            f"и вставьте черновик:\n\n```\n{draft_text}\n```"
        )
    return (
        f"\n\nКонтакт: `{escape_markdown(contact)}`. Скопируйте черновик:\n\n"
        f"```\n{draft_text}\n```"
    )


async def send_user_message_safe_buttons(
    user_id: int,
    *,
    topic_key: str | None = None,
    text: str,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    """send_user_message; при BUTTON_USER_INVALID убирает url-кнопки и шлёт снова."""
    try:
        return await send_user_message(
            user_id,
            topic_key=topic_key,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "button_user_invalid" not in err and "button_url_invalid" not in err:
            raise
        logger.warning("Bad inline url button for user %s: %s", user_id, e)
        safe_rows = []
        if reply_markup and reply_markup.inline_keyboard:
            for row in reply_markup.inline_keyboard:
                if all(getattr(btn, "url", None) is None for btn in row):
                    safe_rows.append(row)
        safe_markup = InlineKeyboardMarkup(inline_keyboard=safe_rows) if safe_rows else None
        extra = (
            "\n\n_Не удалось добавить кнопку «Открыть чат» — используйте контакт из сообщения "
            "и скопируйте черновик вручную._"
        )
        plain_parse = parse_mode
        if parse_mode == "Markdown":
            text = text + extra
        else:
            text = text + re.sub(r"[*_`]", "", extra)
            plain_parse = None
        return await send_user_message(
            user_id,
            topic_key=topic_key,
            text=text,
            parse_mode=plain_parse,
            reply_markup=safe_markup,
        )

# ========== РАССЫЛКА ВАКАНСИЙ ПОДПИСЧИКАМ (без глобальных счётчиков) ==========

async def dispatch_vacancy_push(order: dict):
    """Очередь push: не блокирует Telethon-парсер на время рассылки."""
    async with _vacancy_push_sem:
        await send_vacancy_to_subscribers(order)


def schedule_vacancy_push(order: dict):
    spawn_background_task(dispatch_vacancy_push(order))


async def send_vacancy_to_subscribers(order: dict):
    msg_text = order.get('message_text') or ''
    poster = poster_from_order(order)
    force_cat = order.get('category') if order.get('from_bot_employer') else None
    accepted, category_code, gate_reason, _ = evaluate_vacancy(
        msg_text, poster, force_category=force_cat,
    )
    if not accepted or not category_code:
        vacancy_id = order.get("vacancy_id") or make_vacancy_id(
            order.get('chat_id', ''), order.get('message_id', ''), dedupe_key=order.get("dedupe_key")
        )
        logger.info(
            f"Push skip {vacancy_id}: quality re-check ({gate_reason}), "
            f"stored={order.get('category')}"
        )
        return
    dedupe_key = order.get("dedupe_key")
    vacancy_id = order.get("vacancy_id") or make_vacancy_id(
        order.get('chat_id', ''), order.get('message_id', ''), dedupe_key=order.get("dedupe_key")
    )
    mod_row = get_vacancy_push_row(vacancy_id)
    if mod_row and mod_row[11] not in (None, "approved"):
        logger.info(f"Push skip {vacancy_id}: moderation_status={mod_row[11]}")
        return
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
        body=msg_text,
        source=order.get("chat_title") or "—",
        message_link=order.get("message_link"),
    )

    address = order.get('address') or extract_address_from_text(msg_text)
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
        if not vacancy_matches_user_metro(msg_text, address, subscriber.get('metro_zones')):
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
                _mark_subscriber_blocked_if_needed(subscriber['user_id'])
            else:
                logger.error(f"Ошибка отправки {subscriber['user_id']}: {e}")

    logger.info(
        f"Вакансия {vacancy_id} (категория {category_code}): push {sent_count}, "
        f"free skip {skipped_free}, metro skip {skipped_metro}"
    )
    if sent_count > 0:
        mark_vacancy_sent(vacancy_id)
    if CHANNEL_CROSSPOST_ENABLED and HUNTER_CHANNEL_ID:
        from services.channel_post import post_vacancy_preview_to_channel
        spawn_background_task(post_vacancy_preview_to_channel(
            bot,
            vacancy_id=vacancy_id,
            category_code=category_code,
            category_name=cat_name,
            category_emoji=get_category_emoji(category_code),
            body=msg_text,
            source=order.get("chat_title") or "—",
            freshness=freshness,
        ))

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

def get_employer_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_EMPLOYER_POST)],
            [KeyboardButton(text=BTN_MY_DATA), KeyboardButton(text="❓ Поддержка")],
            [KeyboardButton(text=BTN_SWITCH_CANDIDATE), KeyboardButton(text="📖 Как пользоваться")],
        ],
        resize_keyboard=True,
    )


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
            [KeyboardButton(text="💎 Подписка"), KeyboardButton(text=BTN_MY_DATA)],
            [KeyboardButton(text="📖 Как пользоваться"), KeyboardButton(text="❓ Поддержка")],
        ],
        resize_keyboard=True
    )
    return keyboard, status_text


def get_settings_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SETTINGS_CATEGORIES)],
            [KeyboardButton(text=BTN_METRO)],
            [KeyboardButton(text=BTN_SETTINGS_BACK)],
        ],
        resize_keyboard=True,
    )


USER_MENU_BUTTONS = {
    "🔍 Посмотреть новые вакансии",
    "📨 Мои отклики", BTN_SETTINGS, BTN_SETTINGS_LEGACY,
    BTN_SETTINGS_CATEGORIES, BTN_METRO, BTN_SETTINGS_BACK,
    "📍 Мои районы",
    "💎 Подписка", BTN_MY_DATA, BTN_MY_DATA_LEGACY,
    "📖 Как пользоваться", "❓ Поддержка",
    "📋 Мои категории", "✏️ Изменить категории",
}

ADMIN_BTN_HUB_PARSER = "📡 Парсер"
ADMIN_BTN_HUB_USERS = "👥 Пользователи"
ADMIN_BTN_HUB_EXPORT = "📥 Excel"
ADMIN_BTN_HUB_MOD = "📝 Модерация"
ADMIN_BTN_BACK = "◀️ Назад"


def get_admin_hub_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ADMIN_BTN_HUB_PARSER), KeyboardButton(text=ADMIN_BTN_HUB_USERS)],
            [KeyboardButton(text=ADMIN_BTN_HUB_EXPORT), KeyboardButton(text=ADMIN_BTN_HUB_MOD)],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="📖 Справка"), KeyboardButton(text="❌ Закрыть меню")],
        ],
        resize_keyboard=True,
    )


def get_admin_parser_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Ручная проверка"), KeyboardButton(text="📝 Отчёт парсера")],
            [KeyboardButton(text="📋 Список чатов парсинга"), KeyboardButton(text="📊 Шум по чатам")],
            [KeyboardButton(text="➕ Добавить чат"), KeyboardButton(text="📤 Отправить вакансию")],
            [KeyboardButton(text="🧭 Маппинг категорий")],
            [KeyboardButton(text=ADMIN_BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def get_admin_users_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👥 Список подписчиков"), KeyboardButton(text="🗂️ Карточки пользователей")],
            [KeyboardButton(text="💎 Запросы Premium"), KeyboardButton(text="📋 Список откликов")],
            [KeyboardButton(text="⚠️ Жалобы"), KeyboardButton(text="❓ Поддержка (админ)")],
            [KeyboardButton(text=ADMIN_BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def get_admin_export_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📥 Excel: подписчики"), KeyboardButton(text="📥 Excel: вакансии")],
            [KeyboardButton(text="📥 Excel: заказчики"), KeyboardButton(text="📥 Excel: не подходит")],
            [KeyboardButton(text=ADMIN_BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def get_admin_mod_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Модерация вакансий"), KeyboardButton(text="📺 Канал")],
            [KeyboardButton(text=ADMIN_BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def get_admin_channel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📺 Статус канала"), KeyboardButton(text="📊 Статистика канала")],
            [KeyboardButton(text="📣 Вакансия в канал"), KeyboardButton(text="📝 Новость в канал")],
            [KeyboardButton(text="📢 Промо в канал"), KeyboardButton(text="✏️ Тексты промо")],
            [KeyboardButton(text=ADMIN_BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def _channel_env_ok() -> bool:
    return bool(CHANNEL_CROSSPOST_ENABLED and HUNTER_CHANNEL_ID)


def build_channel_admin_status_html() -> str:
    from services.channel_policy import is_within_channel_posting_hours, msk_now

    total_hour = count_channel_vacancy_posts_in_msk_hour()
    loader_hour = count_channel_vacancy_posts_in_msk_hour("loader")
    lim_total = get_channel_hourly_limit_total()
    lim_loader = get_channel_hourly_limit_loader()
    min_rate = get_channel_loader_min_rate()
    xpost = is_channel_crosspost_enabled()
    promo = is_channel_promo_enabled()
    promo_times = ", ".join(get_channel_promo_times())
    qstart, qend = get_channel_quiet_hours()
    posting_ok = is_within_channel_posting_hours()
    env_line = "✅ Канал подключён" if _channel_env_ok() else "❌ Нет HUNTER_CHANNEL_ID / env"
    now_msk = msk_now().strftime("%H:%M")
    return (
        f"<b>📺 Канал @promostaff_agency_job</b>\n"
        f"{env_line} · сейчас {escape_html(now_msk)} МСК\n\n"
        f"<b>Автопост вакансий:</b> {'🟢 вкл' if xpost else '🔴 выкл'}\n"
        f"<b>Промо ({escape_html(promo_times)}):</b> {'🟢 вкл' if promo else '🔴 выкл'}\n\n"
        f"В этом часе: <b>{total_hour}/{lim_total}</b> вакансий"
        f" (грузчик: {loader_hour}/{lim_loader}, от {min_rate} ₽/ч)\n"
        f"Окно публикаций: {qstart:02d}:00–{qend:02d}:00 МСК — "
        f"{'✅ можно постить' if posting_ok else '⏸ тихие часы'}\n\n"
        f"<i>Ручная публикация по ID — без лимитов. "
        f"Кнопки ниже меняют настройки без Bothost.</i>"
    )


def build_channel_admin_inline_keyboard() -> InlineKeyboardMarkup:
    xpost = is_channel_crosspost_enabled()
    promo = is_channel_promo_enabled()
    lim_total = get_channel_hourly_limit_total()
    lim_loader = get_channel_hourly_limit_loader()
    min_rate = get_channel_loader_min_rate()
    return InlineKeyboardMarkup(inline_keyboard=[
        [_inline_btn(
            "🔴 Автопост: ВЫКЛ" if not xpost else "🟢 Автопост: ВКЛ",
            callback_data="ch_t_xpost",
        )],
        [_inline_btn(
            "🔴 Промо 09/14/20: ВЫКЛ" if not promo else "🟢 Промо 09/14/20: ВКЛ",
            callback_data="ch_t_promo",
        )],
        [
            _inline_btn("−", callback_data="ch_lim_tot_dec"),
            _inline_btn(f"Всего/ч: {lim_total}", callback_data="ch_refresh"),
            _inline_btn("+", callback_data="ch_lim_tot_inc"),
        ],
        [
            _inline_btn("−", callback_data="ch_lim_ldr_dec"),
            _inline_btn(f"Грузчик/ч: {lim_loader}", callback_data="ch_refresh"),
            _inline_btn("+", callback_data="ch_lim_ldr_inc"),
        ],
        [
            _inline_btn("−50", callback_data="ch_rate_dec"),
            _inline_btn(f"Грузчик от {min_rate} ₽/ч", callback_data="ch_refresh"),
            _inline_btn("+50", callback_data="ch_rate_inc"),
        ],
    ])


async def send_channel_admin_status(message: types.Message, *, edit: bool = False):
    text = build_channel_admin_status_html()
    markup = build_channel_admin_inline_keyboard()
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=markup)


def build_promo_texts_inline_keyboard() -> InlineKeyboardMarkup:
    from db import get_channel_promo_times

    times = get_channel_promo_times()
    rows = []
    for i, slot_time in enumerate(times[:6]):
        rows.append([_inline_btn(f"✏️ Слот {i + 1} ({slot_time})", callback_data=f"ch_promo_edit_{i}")])
    rows.append([
        _inline_btn("📂 Из файла", callback_data="ch_promo_file"),
        _inline_btn("🗑 Сброс", callback_data="ch_promo_reset"),
    ])
    rows.append([_inline_btn("👁 Превью всех", callback_data="ch_promo_preview")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_promo_texts_admin_screen(message: types.Message, *, edit: bool = False):
    from services.channel_promo_texts import format_promo_texts_admin_summary

    text = format_promo_texts_admin_summary()
    markup = build_promo_texts_inline_keyboard()
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=markup)


def build_custom_post_confirm_keyboard(with_bot_button: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_inline_btn("✅ Опубликовать", callback_data="ch_custom_pub", style="success")],
        [_inline_btn(
            "🔘 Без кнопки бота" if with_bot_button else "➕ Кнопка «Открыть бота»",
            callback_data="ch_custom_btn",
        )],
        [_inline_btn("❌ Отмена", callback_data="ch_custom_cancel", style="danger")],
    ])


async def send_custom_post_preview(message: types.Message, state: FSMContext):
    data = await state.get_data()
    text = data.get("custom_text") or ""
    with_btn = bool(data.get("custom_with_bot_button", True))
    from services.channel_custom_post import format_custom_post_preview
    preview = format_custom_post_preview(text, with_bot_button=with_btn)
    if data.get("custom_photo_file_id"):
        preview += "\n\n📷 <i>К посту будет приложено фото</i>"
    await message.answer(
        preview,
        parse_mode="HTML",
        reply_markup=build_custom_post_confirm_keyboard(with_btn),
    )


def get_admin_keyboard():
    return get_admin_hub_keyboard()


ADMIN_MENU_BUTTONS = {
    ADMIN_BTN_HUB_PARSER, ADMIN_BTN_HUB_USERS, ADMIN_BTN_HUB_EXPORT, ADMIN_BTN_HUB_MOD,
    ADMIN_BTN_BACK,
    "📊 Статистика", "🔍 Ручная проверка", "📋 Список откликов", "📝 Отчёт парсера",
    "👥 Список подписчиков", "📢 Рассылка", "🗂️ Карточки пользователей", "💎 Запросы Premium",
    "🧭 Маппинг категорий", "⚠️ Жалобы", "❓ Поддержка (админ)", "➕ Добавить чат",
    "📋 Список чатов парсинга", "💬 Чаты парсинга", "📤 Отправить вакансию",
    "📥 Excel: подписчики", "📥 Excel: вакансии", "📥 Excel: заказчики",
    "📥 Excel: не подходит", "📊 Шум по чатам", "📝 Модерация вакансий",
    "📺 Канал", "📺 Статус канала", "📊 Статистика канала",
    "📣 Вакансия в канал", "📣 В канал", "📝 Новость в канал", "📢 Промо в канал",
    "✏️ Тексты промо",
    "📣 В канал", "📣 Вакансия в канал", "📝 Новость в канал", "📢 Промо в канал",
    "📖 Как пользоваться", "📖 Справка", "❌ Закрыть меню",
}

# ========== КОМАНДЫ И ОБРАБОТЧИКИ ==========

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    user_id = message.from_user.id
    if user_id == YOUR_USER_ID:
        await message.answer(build_admin_help_html(), parse_mode="HTML")
        return
    await send_user_help(message, user_id)


@dp.message(lambda m: m.text in ("📖 Как пользоваться", "📖 Справка"))
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
    parts = (message.text or "").split(maxsplit=1)
    start_payload = parts[1].strip().lower() if len(parts) > 1 else ""

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
    if start_payload == "employer":
        set_subscriber_role(user_id, "employer")

    if start_payload.startswith("vac_"):
        vacancy_id = start_payload[4:].strip()
        if vacancy_id:
            await show_vacancy_by_deeplink(message, user_id, vacancy_id)

    expired_msg = downgrade_expired_premium(user_id)
    if expired_msg:
        await message.answer(expired_msg, parse_mode="Markdown")

    profile = get_subscriber_profile(user_id)
    role = get_subscriber_role(user_id)

    if profile and profile.get("full_name") and profile.get("phone"):
        greet_name = escape_markdown(greeting_display_name(profile, message.from_user))
        if role == "employer":
            await setup_forum_topics_for_user(user_id)
            await message.answer(
                f"👋 С возвращением, {greet_name}!\n\n"
                f"🏢 Режим заказчика — разместите вакансию или обновите контакты в «{BTN_MY_DATA}».",
                reply_markup=get_employer_keyboard(),
            )
            return
        categories = get_user_categories(user_id)
        if categories:
            await setup_forum_topics_for_user(user_id)
            keyboard, status_text = get_main_keyboard(user_id)
            await message.answer(
                f"👋 С возвращением, {greet_name}!\n\n{status_text}\n\n"
                f"Используйте кнопки меню. Инструкция — «📖 Как пользоваться» или /help",
                reply_markup=keyboard
            )
            return
        await send_category_picker(message.chat.id, user_id)
        return

    if role == "employer" or start_payload == "employer":
        set_subscriber_role(user_id, "employer")
        await message.answer(
            "🏢 *PromoStaff Hunter — для заказчиков*\n\n"
            "Размещайте вакансии — их увидят исполнители с Premium-подпиской.\n\n"
            "Как вас представить? (компания или ФИО контактного лица)",
            parse_mode="Markdown",
        )
        await state.set_state(EmployerRegState.waiting_for_name)
        return

    await message.answer(
        "👋 *Добро пожаловать!*\n\n"
        "Выберите, как будете пользоваться ботом:",
        parse_mode="Markdown",
        reply_markup=build_role_picker_markup(),
    )


@dp.callback_query(lambda c: c.data == "role_candidate")
async def role_candidate_pick(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    user_id = callback.from_user.id
    set_subscriber_role(user_id, "candidate")
    await callback.message.edit_text(
        "👷 *Режим исполнителя*\n\n"
        "Я помогу найти подходящие вакансии.\n\n"
        "Как вас зовут? (ФИО полностью)\n\nПример: *Иван Петров*",
        parse_mode="Markdown",
    )
    await state.set_state(RegistrationState.waiting_for_name)


@dp.callback_query(lambda c: c.data == "role_employer")
async def role_employer_pick(callback: types.CallbackQuery, state: FSMContext):
    await safe_callback_answer(callback)
    user_id = callback.from_user.id
    set_subscriber_role(user_id, "employer")
    await callback.message.edit_text(
        "🏢 *Режим заказчика*\n\n"
        "Как вас представить? (компания или ФИО контактного лица)",
        parse_mode="Markdown",
    )
    await state.set_state(EmployerRegState.waiting_for_name)


@dp.message(EmployerRegState.waiting_for_name)
async def employer_reg_name(message: types.Message, state: FSMContext):
    full_name = message.text.strip()
    if len(full_name) < 2:
        await message.answer("❌ Введите название компании или ФИО (минимум 2 символа).")
        return
    await state.update_data(full_name=full_name)
    phone_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "📞 *Контактный телефон*\n\n"
        "Номер для связи с кандидатами при откликах.",
        parse_mode="Markdown",
        reply_markup=phone_keyboard,
    )
    await state.set_state(EmployerRegState.waiting_for_phone)


@dp.message(EmployerRegState.waiting_for_phone)
async def employer_reg_phone(message: types.Message, state: FSMContext):
    user = message.from_user
    if message.contact:
        phone = message.contact.phone_number
    else:
        phone = message.text.strip()
        digits_only = re.sub(r"\D", "", phone)
        if len(digits_only) < 10 or len(digits_only) > 15:
            await message.answer("❌ Введите корректный номер или нажмите кнопку отправки контакта.")
            return

    data = await state.get_data()
    update_subscriber_profile(user.id, data["full_name"], None, phone, birth_date=None)
    poster = poster_from_tg_user(user)
    contact_text, contact_source = resolve_vacancy_contact("", poster)
    upsert_employer_from_post(
        telegram_user_id=user.id,
        username=user.username,
        display_name=data["full_name"],
        contact_text=contact_text or phone,
        contact_source=contact_source or "profile_phone",
        category_code=None,
        bot_user_id=user.id,
    )
    await state.clear()
    await message.answer(
        f"✅ *Профиль заказчика создан*\n\n"
        f"🏢 {data['full_name']}\n"
        f"📞 {phone}\n\n"
        f"Нажмите «{BTN_EMPLOYER_POST}», чтобы опубликовать вакансию.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer("Меню заказчика:", reply_markup=get_employer_keyboard())


@dp.message(lambda m: m.text == BTN_EMPLOYER_POST)
async def employer_post_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id == YOUR_USER_ID:
        return
    if get_subscriber_role(user_id) != "employer":
        await message.answer("Эта функция доступна в режиме заказчика. Напишите /start и выберите «Ищу персонал».")
        return
    profile = get_subscriber_profile(user_id)
    if not profile or not profile.get("phone"):
        await message.answer("Сначала заполните профиль — /start")
        return
    await message.answer(
        "📤 *Размещение вакансии*\n\n"
        "Выберите категорию персонала:",
        parse_mode="Markdown",
        reply_markup=get_postvacancy_categories_keyboard(prefix="employercat"),
    )
    await state.set_state(EmployerPostState.waiting_for_category)


@dp.callback_query(lambda c: c.data and c.data.startswith("employercat_"))
async def employer_post_category(callback: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state != EmployerPostState.waiting_for_category:
        await callback.answer("Сначала нажмите «Разместить вакансию»", show_alert=True)
        return
    category_code = callback.data.replace("employercat_", "")
    cat_name = get_category_name(category_code)
    await state.update_data(category_code=category_code, category_name=cat_name)
    await callback.message.answer(
        f"📝 Введите текст вакансии (категория: *{cat_name}*).\n\n"
        "Обязательно: роль, оплата, адрес/дата. Контакт подставится из вашего профиля.",
        parse_mode="Markdown",
    )
    await state.set_state(EmployerPostState.waiting_for_text)
    await callback.answer()


@dp.message(EmployerPostState.waiting_for_text)
async def employer_post_text(message: types.Message, state: FSMContext):
    if message.text in ADMIN_MENU_BUTTONS:
        return
    data = await state.get_data()
    category_code = data.get("category_code")
    if not category_code:
        await state.clear()
        return
    ok, info = await publish_employer_vacancy(message.from_user, category_code, message.text)
    await state.clear()
    if not ok:
        await message.answer(
            f"❌ Вакансия не принята: {info}\n\n"
            "Проверьте текст и попробуйте снова.",
            reply_markup=get_employer_keyboard(),
        )
        return
    cat_name = data.get("category_name") or get_category_name(category_code)
    await notify_admin_moderation(info, category_code, message.text, message.from_user.id)
    await message.answer(
        f"✅ Вакансия «{cat_name}» отправлена на модерацию.\n"
        f"ID: `{info}`\n\n"
        "После проверки администратором её увидят Premium-подписчики.",
        parse_mode="Markdown",
        reply_markup=get_employer_keyboard(),
    )


@dp.message(lambda m: m.text == BTN_SWITCH_CANDIDATE)
async def employer_switch_to_candidate(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id == YOUR_USER_ID:
        return
    set_subscriber_role(user_id, "candidate")
    profile = get_subscriber_profile(user_id)
    if profile and profile.get("full_name") and profile.get("phone"):
        categories = get_user_categories(user_id)
        if categories:
            keyboard, status_text = get_main_keyboard(user_id)
            await message.answer(
                f"👷 Режим исполнителя.\n\n{status_text}",
                reply_markup=keyboard,
            )
            return
        await send_category_picker(message.chat.id, user_id)
        return
    await message.answer(
        "👷 Режим исполнителя. Заполните профиль — как вас зовут? (ФИО)",
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
        await setup_forum_topics_for_user(user_id)
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
            f"{'💎 Новые вакансии — в теме «📬 Вакансии».' if FORUM_TOPICS_ENABLED else '💎 Новые вакансии приходят моментально в чат.'}"
            f"{' Push Premium.' if await run_db(is_user_premium, user_id) else ' Free: «Посмотреть новые».'}"
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
        responded = format_db_datetime_short(resp.get("responded_at"))
        text = (
            f"<b>{i}.</b> {escape_html(responded)} · "
            f"{escape_html(_response_status_label(resp.get('is_closed')))}\n"
            f"📢 {escape_html(source)}\n\n"
            f"{escape_html(preview)}"
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
            await message.answer(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
            await asyncio.sleep(0.2)
        except TelegramBadRequest as e:
            logger.warning(f"send_responses_page item {i}: {e}")
            plain = re.sub(r"<[^>]*>", "", text)
            try:
                await message.answer(plain, reply_markup=markup, disable_web_page_preview=True)
            except Exception as e2:
                logger.warning(f"send_responses_page item {i} fallback: {e2}")
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


def _feed_vacancies_for_category(
    user_id: int, cat: dict, apply_metro: bool, metro_zones: str | None, feed_mode: str = "fresh",
) -> list:
    vacancies = get_feed_vacancies_for_user(user_id, cat["code"])
    result = []
    for vac in vacancies:
        if not _vacancy_in_feed_mode(vac, feed_mode):
            continue
        if not vacancy_matches_category(vac.get("text") or "", cat["code"]):
            continue
        if apply_metro and not vacancy_matches_user_metro(
            vac.get("text", ""), vac.get("address"), metro_zones
        ):
            continue
        vac["category"] = cat
        result.append(vac)
    return result


def _collect_feed_vacancies(
    user_id: int, category_codes: list[str] | None = None, feed_mode: str = "fresh",
) -> list:
    apply_metro, metro_zones = _feed_metro_context(user_id)
    user_categories = get_user_categories(user_id)
    if category_codes is not None:
        codes = set(category_codes)
        user_categories = [c for c in user_categories if c["code"] in codes]
    all_vacancies = []
    for cat in user_categories:
        all_vacancies.extend(
            _feed_vacancies_for_category(user_id, cat, apply_metro, metro_zones, feed_mode)
        )
    all_vacancies.sort(
        key=lambda v: v.get("published_at") or v.get("found_at") or "",
        reverse=True,
    )
    return all_vacancies


def _feed_count_for_category(
    user_id: int, cat: dict, apply_metro: bool, metro_zones: str | None, feed_mode: str = "fresh",
) -> int:
    return len(_feed_vacancies_for_category(user_id, cat, apply_metro, metro_zones, feed_mode))


def _feed_mode_totals(user_id: int) -> tuple[int, int]:
    apply_metro, metro_zones = _feed_metro_context(user_id)
    fresh_total = archive_total = 0
    for cat in get_user_categories(user_id):
        fresh_total += _feed_count_for_category(user_id, cat, apply_metro, metro_zones, "fresh")
        archive_total += _feed_count_for_category(user_id, cat, apply_metro, metro_zones, "archive")
    return fresh_total, archive_total


def build_feed_mode_keyboard(fresh_total: int, archive_total: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🟢 Свежие до {FEED_FRESH_HOURS} ч ({fresh_total})",
            callback_data="feed_mode_fresh",
        )],
        [InlineKeyboardButton(
            text=f"📂 Ранее {FEED_FRESH_HOURS}–{FEED_ARCHIVE_MAX_HOURS // 24} дн. ({archive_total})",
            callback_data="feed_mode_archive",
        )],
    ])


def build_feed_category_keyboard(user_id: int, feed_mode: str) -> tuple[InlineKeyboardMarkup, int]:
    user_categories = get_user_categories(user_id)
    apply_metro, metro_zones = _feed_metro_context(user_id)
    buttons, row = [], []
    total = 0
    mode_label = "свежие" if feed_mode == "fresh" else "ранее"
    for i, cat in enumerate(user_categories):
        count = _feed_count_for_category(user_id, cat, apply_metro, metro_zones, feed_mode)
        total += count
        row.append(InlineKeyboardButton(
            text=f"{cat['emoji']} {cat['name']} ({count})",
            callback_data=f"feed_{feed_mode}_{cat['code']}",
        ))
        if len(row) == 2 or i == len(user_categories) - 1:
            buttons.append(row)
            row = []
    if total > 0:
        buttons.append([InlineKeyboardButton(
            text=f"📋 Все {mode_label} ({total})",
            callback_data=f"feed_{feed_mode}_all",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Период", callback_data="feed_pick_mode")])
    return InlineKeyboardMarkup(inline_keyboard=buttons), total


async def show_feed_mode_menu(message: types.Message, user_id: int):
    user_categories = get_user_categories(user_id)
    if not user_categories:
        await message.answer("⚠️ Вы ещё не выбрали категории вакансий. Используйте «⚙️ Настройки»")
        return
    fresh_total, archive_total = _feed_mode_totals(user_id)
    apply_metro, _ = _feed_metro_context(user_id)
    hint = "\n\n📍 Учитывается фильтр «Станции метро»." if apply_metro else ""
    if fresh_total == 0 and archive_total == 0:
        await message.answer(
            f"🔍 *Новых вакансий по вашим категориям пока нет.*{hint}\n\n"
            f"{'💎 Premium — push сразу в чат.' if is_user_premium(user_id) else 'Я продолжаю мониторинг.'}",
            parse_mode="Markdown",
        )
        return
    await message.answer(
        f"🔍 *Лента вакансий* — выберите период:{hint}\n\n"
        f"🟢 *Свежие* — опубликованы за последние {FEED_FRESH_HOURS} ч.\n"
        f"📂 *Ранее* — от {FEED_FRESH_HOURS} ч до {FEED_ARCHIVE_MAX_HOURS // 24} дн.",
        parse_mode="Markdown",
        reply_markup=build_feed_mode_keyboard(fresh_total, archive_total),
    )


async def show_feed_category_menu(message: types.Message, user_id: int, feed_mode: str):
    user_categories = get_user_categories(user_id)
    if not user_categories:
        await message.answer("⚠️ Вы ещё не выбрали категории вакансий. Используйте «⚙️ Настройки»")
        return
    markup, total = build_feed_category_keyboard(user_id, feed_mode)
    apply_metro, _ = _feed_metro_context(user_id)
    hint = "\n\n📍 Учитывается фильтр «Станции метро»." if apply_metro else ""
    mode_title = f"🟢 Свежие ({FEED_FRESH_HOURS} ч)" if feed_mode == "fresh" else "📂 Ранее"
    if total == 0:
        await message.answer(
            f"{mode_title} — в этой категории вакансий нет.{hint}\n\n"
            "Попробуйте другой период или категорию.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Период", callback_data="feed_pick_mode")],
            ]),
        )
        return
    await message.answer(
        f"🔍 *{mode_title}* — выберите категорию ({total}):{hint}",
        parse_mode="Markdown",
        reply_markup=markup,
    )


async def open_feed_vacancies(
    message: types.Message,
    user_id: int,
    feed_mode: str,
    category_codes: list[str] | None = None,
):
    all_vacancies = _collect_feed_vacancies(user_id, category_codes, feed_mode)
    if not all_vacancies:
        apply_metro, _ = _feed_metro_context(user_id)
        hint = "\n\nПопробуйте расширить список станций в ⚙️ Настройки → 📍 Станции метро." if apply_metro else ""
        await message.answer(f"🔍 В этой категории вакансий нет.{hint}", parse_mode="Markdown")
        return
    user_pages[user_id] = {
        "vacancies": all_vacancies,
        "page": 0,
        "total": len(all_vacancies),
        "feed_filter": category_codes,
        "feed_mode": feed_mode,
    }
    await send_vacancy_page(message, user_id, 0)


@dp.message(lambda m: m.text == "🔍 Посмотреть новые вакансии")
async def show_new_vacancies(message: types.Message):
    await show_feed_mode_menu(message, message.from_user.id)


@dp.callback_query(lambda c: c.data == "feed_pick_mode")
async def feed_pick_mode_callback(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    await show_feed_mode_menu(callback.message, callback.from_user.id)


@dp.callback_query(lambda c: c.data in {"feed_mode_fresh", "feed_mode_archive"})
async def feed_mode_callback(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    mode = "fresh" if callback.data == "feed_mode_fresh" else "archive"
    await show_feed_category_menu(callback.message, callback.from_user.id, mode)


@dp.callback_query(lambda c: c.data and c.data.startswith("feed_fresh_"))
async def feed_fresh_routes(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    suffix = callback.data.replace("feed_fresh_", "", 1)
    if suffix == "all":
        await open_feed_vacancies(callback.message, callback.from_user.id, "fresh", None)
    else:
        await open_feed_vacancies(callback.message, callback.from_user.id, "fresh", [suffix])


@dp.callback_query(lambda c: c.data and c.data.startswith("feed_archive_"))
async def feed_archive_routes(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    suffix = callback.data.replace("feed_archive_", "", 1)
    if suffix == "all":
        await open_feed_vacancies(callback.message, callback.from_user.id, "archive", None)
    else:
        await open_feed_vacancies(callback.message, callback.from_user.id, "archive", [suffix])


@dp.callback_query(lambda c: c.data == "feed_menu")
async def feed_menu_callback(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    data = user_pages.get(callback.from_user.id) or {}
    mode = data.get("feed_mode") or "fresh"
    await show_feed_category_menu(callback.message, callback.from_user.id, mode)


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
        emoji, cat_name = _detected_category_display(vac.get("text") or "")
        text = format_vacancy_card_html(
            category_emoji=emoji,
            category_name=cat_name,
            freshness=get_freshness_label(raw_pub),
            published_at=format_publication_time(raw_pub),
            body=vac.get("text") or "",
            source=vac.get("source") or "—",
            message_link=vac.get("link"),
        )
        keyboard = build_vacancy_keyboard(vac["id"], vac.get("address"))
        try:
            await send_vacancy_card(message.chat.id, text, reply_markup=keyboard)
            mark_vacancy_sent_to_user(vac["id"], user_id)
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"Ошибка отправки вакансии: {e}")

    nav_rows = []
    pager = []
    if page > 0:
        pager.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"vac_page_{page-1}"))
    if end < total:
        pager.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"vac_page_{page+1}"))
    if pager:
        nav_rows.append(pager)
    nav_rows.append([
        InlineKeyboardButton(text="📋 К категориям", callback_data="feed_menu"),
        InlineKeyboardButton(text="🏠 Период", callback_data="feed_pick_mode"),
    ])
    await message.answer(
        "📄 *Навигация*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=nav_rows),
    )


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


def _is_metro_reset(text: str) -> bool:
    return text.strip().lower() in {"-", "0", "сброс", "reset", "отмена", "нет"}


async def _cancel_metro_input(message: types.Message, state: FSMContext):
    """Выход из ввода метро без сохранения текста кнопки меню."""
    await state.clear()
    text = (message.text or "").strip()
    user_id = message.from_user.id
    if text in {BTN_MY_DATA, BTN_MY_DATA_LEGACY}:
        await send_profile_data_screen(message.chat.id, user_id)
        return
    if text == BTN_SETTINGS_CATEGORIES:
        await send_category_picker(message.chat.id, user_id)
        return
    if text in {BTN_SETTINGS, BTN_SETTINGS_LEGACY, BTN_SETTINGS_BACK}:
        await message.answer("⚙️ Настройки", reply_markup=get_settings_keyboard())
        return
    if text == BTN_METRO or text == "📍 Мои районы":
        await metro_zones_menu(message, state)
        return
    keyboard, status = get_main_keyboard(user_id)
    await message.answer("Ввод станций отменён.", reply_markup=keyboard)


@dp.message(lambda m: m.text in {BTN_METRO, "📍 Мои районы"})
async def metro_zones_menu(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if not is_user_premium(user_id):
        await message.answer(
            "📍 Фильтр по метро — функция *Premium*.\n\n"
            "Оформите подписку в 💎 Подписка или дождитесь окончания пробного периода.",
            parse_mode="Markdown",
            reply_markup=get_settings_keyboard() if message.text == BTN_METRO else get_main_keyboard(user_id)[0],
        )
        return
    profile = get_subscriber_profile(user_id)
    current = profile.get("metro_zones") if profile else None
    current_line = current if current else "не заданы (приходят все локации)"
    await message.answer(
        f"📍 *Станции метро*\n\n"
        f"Сейчас: {current_line}\n\n"
        f"Введите станции через запятую, например:\n"
        f"`Таганская, Беляево, Сокол`\n\n"
        f"Чтобы *сбросить* фильтр — отправьте `0` или `-`.",
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard(),
    )
    await state.set_state(MetroState.waiting_for_zones)


@dp.message(MetroState.waiting_for_zones)
async def metro_zones_save(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    if text in USER_MENU_BUTTONS:
        await _cancel_metro_input(message, state)
        return
    if _is_metro_reset(text):
        set_user_metro_zones(user_id, None)
        await state.clear()
        await message.answer(
            "✅ Фильтр по метро сброшен — снова все локации.",
            reply_markup=get_settings_keyboard(),
        )
        return
    zones = ", ".join(z.strip() for z in text.split(",") if z.strip())
    if not zones or "👤" in zones or "⚙️" in zones:
        await message.answer(
            "❌ Укажите названия станций через запятую.\n"
            "Или отправьте `0`, чтобы показывать все локации.",
            parse_mode="Markdown",
        )
        return
    set_user_metro_zones(user_id, zones)
    await state.clear()
    await message.answer(
        f"✅ Сохранено: *{zones}*\n\nPush и лента — только вакансии с этими станциями.",
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard(),
    )

@dp.message(Command("user"))
async def user_admin_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "Использование: <code>/user USER_ID</code>\n\n"
            "Пример: <code>/user 227713003</code>",
            parse_mode="HTML",
        )
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("❌ USER_ID должен быть числом.")
        return
    await show_admin_user_detail(message, target_id, cards_page=0, edit=False)


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

@dp.message(lambda m: m.text == BTN_SETTINGS_BACK)
async def settings_back_to_main(message: types.Message, state: FSMContext):
    await state.clear()
    keyboard, status = get_main_keyboard(message.from_user.id)
    await message.answer(f"🏠 Главное меню\n\n{status}", reply_markup=keyboard)


@dp.message(lambda m: m.text in {BTN_SETTINGS, BTN_SETTINGS_LEGACY, "📋 Мои категории", "✏️ Изменить категории"})
async def open_settings_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "⚙️ *Настройки*\n\n"
        "• 📌 *Категории* — какие вакансии присылать\n"
        "• 📍 *Станции метро* — фильтр локаций (Premium)\n\n"
        "Выберите пункт ниже.",
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard(),
    )


@dp.message(lambda m: m.text == BTN_SETTINGS_CATEGORIES)
async def open_categories_from_settings(message: types.Message, state: FSMContext):
    await state.clear()
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
    await send_user_message(
        message.from_user.id,
        topic_key="support",
        text=(
            "📞 *Поддержка*\n\n"
            "Опишите вашу проблему или вопрос, и администратор ответит вам в ближайшее время.\n\n"
            "Напишите ваше сообщение:"
        ),
        parse_mode="Markdown",
    )
    await state.set_state(SupportState.waiting_for_question)

@dp.message(SupportState.waiting_for_question)
async def process_support_question(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    add_support_request(user_id, message.text, username)
    await send_user_message(
        user_id,
        topic_key="support",
        text="✅ Ваш вопрос отправлен администратору. Ответ придёт в эту тему.",
    )
    await state.clear()


# ========== ЖАЛОБЫ НА ВАКАНСИИ / FEEDBACK «НЕ ПОДХОДИТ» ==========

async def _finalize_notfit_feedback(
    user_id: int,
    vacancy_id: str,
    reason_code: str,
    reason_text: str | None,
    *,
    callback: types.CallbackQuery | None = None,
    message: types.Message | None = None,
):
    row = fetchone("SELECT category_code FROM vacancies WHERE id = ?", (vacancy_id,))
    vac_cat = row[0] if row else ""
    user_cats = [c["code"] for c in get_user_categories(user_id)]
    record_vacancy_notfit(
        user_id, vacancy_id, vac_cat or "", user_cats,
        reason_code=reason_code, reason_text=reason_text,
    )
    mark_vacancy_sent_to_user(vacancy_id, user_id)
    thank = (
        "Спасибо! Эту вакансию больше не покажем.\n"
        "Причина сохранена — по ней настраиваем фильтры и Excel-отчёт для админа."
    )
    if callback:
        await callback.answer("Учтено")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer(thank)
    elif message:
        await message.answer(thank)


@dp.callback_query(lambda c: c.data and re.fullmatch(r"notfit_[^:]+", c.data))
async def notfit_vacancy_pick(callback: types.CallbackQuery):
    vacancy_id = callback.data.replace("notfit_", "", 1)
    await callback.answer()
    kb = build_notfit_reason_keyboard(vacancy_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer("Почему не подходит?", reply_markup=kb)


@dp.callback_query(lambda c: c.data and c.data.startswith("notfit_cancel:"))
async def notfit_vacancy_cancel(callback: types.CallbackQuery):
    vacancy_id = callback.data.split(":", 1)[1]
    row = fetchone("SELECT address FROM vacancies WHERE id = ?", (vacancy_id,))
    address = row[0] if row else None
    try:
        await callback.message.edit_reply_markup(
            reply_markup=build_vacancy_keyboard(vacancy_id, address),
        )
    except TelegramBadRequest:
        pass
    await callback.answer("Отменено")


@dp.callback_query(lambda c: c.data and c.data.startswith("nfr:"))
async def notfit_reason_chosen(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Ошибка", show_alert=True)
        return
    _, reason_code, vacancy_id = parts
    if reason_code not in NOTFIT_REASONS:
        await callback.answer("Неизвестная причина", show_alert=True)
        return
    if reason_code == "other":
        await state.set_state(NotfitReasonState.waiting_other)
        await state.update_data(notfit_vacancy_id=vacancy_id)
        await callback.answer()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer("Напишите одним сообщением, почему не подходит:")
        return
    await _finalize_notfit_feedback(
        callback.from_user.id, vacancy_id, reason_code, None, callback=callback,
    )


@dp.message(NotfitReasonState.waiting_other)
async def notfit_reason_other_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    vacancy_id = data.get("notfit_vacancy_id")
    if not vacancy_id:
        await state.clear()
        await message.answer("Сессия устарела. Нажмите «Не подходит» на вакансии ещё раз.")
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напишите коротко, что не так, или /start чтобы отменить.")
        return
    await state.clear()
    await _finalize_notfit_feedback(
        message.from_user.id, vacancy_id, "other", text[:500], message=message,
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("mod_ok_"))
async def moderation_approve(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    vacancy_id = callback.data.replace("mod_ok_", "", 1)
    row = get_vacancy_push_row(vacancy_id)
    if not row:
        await callback.answer("Вакансия не найдена", show_alert=True)
        return
    set_vacancy_moderation(vacancy_id, "approved")
    order = build_order_from_vacancy_row(vacancy_id, row)
    if order:
        schedule_vacancy_push(order)
    employer_uid = row[12]
    if employer_uid:
        try:
            await bot.send_message(
                employer_uid,
                f"✅ Ваша вакансия одобрена и отправлена подписчикам.\nID: `{vacancy_id}`",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("moderation notify employer: %s", e)
    await callback.message.edit_text(f"✅ Вакансия `{vacancy_id}` опубликована.", parse_mode="Markdown")
    await callback.answer("Опубликовано")


@dp.callback_query(lambda c: c.data and c.data.startswith("mod_ch_"))
async def moderation_channel_post(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    vacancy_id = callback.data.replace("mod_ch_", "", 1)
    await callback.answer("Публикую…")
    result = await channel_post_for_vacancy(vacancy_id, force=True)
    await callback.message.answer(result, parse_mode="Markdown")


@dp.callback_query(lambda c: c.data and c.data.startswith("mod_no_"))
async def moderation_reject(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    vacancy_id = callback.data.replace("mod_no_", "", 1)
    row = get_vacancy_push_row(vacancy_id)
    set_vacancy_moderation(vacancy_id, "rejected")
    employer_uid = row[12] if row else None
    if employer_uid:
        try:
            await bot.send_message(
                employer_uid,
                f"❌ Вакансия не прошла модерацию.\nID: `{vacancy_id}`\n\n"
                "Уточните роль, оплату и контакт и отправьте снова.",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("moderation reject notify: %s", e)
    await callback.message.edit_text(f"❌ Вакансия `{vacancy_id}` отклонена.", parse_mode="Markdown")
    await callback.answer("Отклонено")


@dp.message(Command("setchatroles"))
async def setchatroles_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Использование:\n`/setchatroles https://t.me/chatname promoter,helper,loader`",
            parse_mode="Markdown",
        )
        return
    chat_link = normalize_chat_link(parts[1])
    if not chat_link:
        await message.answer("❌ Неверная ссылка на чат.")
        return
    set_target_chat_expected_roles(chat_link, parts[2].replace(" ", ""))
    await message.answer(
        f"✅ Профиль чата `{chat_link}`:\nожидаемые роли: *{parts[2]}*",
        parse_mode="Markdown",
    )


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
    and not c.data.startswith("respond_llm_")
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
    )
    if contact_link:
        msg += (
            "Нажмите кнопку ниже, откроется личный чат с заказчиком и готовым текстом анкеты.\n"
            "Перед отправкой можно отредактировать сообщение вручную."
        )
    else:
        msg += manual_contact_hint(employer_contact, draft_text).lstrip("\n")
    buttons = []
    if contact_link:
        buttons.append([InlineKeyboardButton(text="✅ Открыть чат и отправить", url=contact_link)])
    buttons.append([InlineKeyboardButton(text="✏️ Добавить комментарий", callback_data=f"respond_add_{vacancy_id}")])
    if LLM_ENABLED and is_user_premium(user_id):
        buttons.append([_inline_btn("✨ Улучшить текст", callback_data=f"respond_llm_{vacancy_id}", style="primary")])
    if STARS_ENABLED and not has_star_purchase_for_vacancy(user_id, vacancy_id):
        buttons.append([_inline_btn("⭐ Расширенный отклик", callback_data=f"star_resp_{vacancy_id}")])
    buttons.append([InlineKeyboardButton(text="🚫 Отмена", callback_data="respond_cancel")])
    add_response(user_id, vacancy_id, vacancy_text[:200] if vacancy_text else None, vacancy_link, profile.get('photo_file_id'))
    from services.forum_topics import TOPIC_RESPONSES
    await send_user_message_safe_buttons(
        user_id,
        topic_key=TOPIC_RESPONSES,
        text=msg,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await send_user_message(
        user_id,
        topic_key=TOPIC_RESPONSES,
        text="📨 Отклик сохранён — смотрите в «📨 Мои отклики».",
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data and c.data.startswith("respond_llm_"))
async def respond_llm_enhance(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    vacancy_id = callback.data.replace("respond_llm_", "", 1)
    if not is_user_premium(user_id):
        await callback.answer("Доступно с Premium", show_alert=True)
        return
    usage_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if get_llm_usage_today(user_id, usage_day) >= LLM_DAILY_LIMIT_PREMIUM:
        await callback.answer("Лимит LLM на сегодня исчерпан", show_alert=True)
        return
    profile = get_subscriber_profile(user_id)
    row = get_vacancy_row(vacancy_id)
    if not profile or not row:
        await callback.answer("Ошибка данных", show_alert=True)
        return
    vacancy_text = row[0] or ""
    employer_contact = row[3] or extract_contact_from_text(vacancy_text)
    await callback.answer("Составляю текст…")
    await bot.send_chat_action(callback.message.chat.id, "typing")
    from services.llm_client import ask_llm
    from services.llm_prompts import build_response_draft_prompt
    from services.forum_topics import TOPIC_RESPONSES
    cat_row = fetchone("SELECT category_code FROM vacancies WHERE id = ?", (vacancy_id,))
    cat_code = cat_row[0] if cat_row else "promoter"
    prompt = build_response_draft_prompt(
        vacancy_text=vacancy_text,
        category_name=get_category_name(cat_code),
        profile_summary=build_candidate_profile_text(profile),
    )
    draft = await ask_llm(prompt)
    if not draft:
        await send_user_message(
            user_id, topic_key=TOPIC_RESPONSES,
            text="Не удалось улучшить текст — используйте стандартный черновик выше.",
        )
        return
    increment_llm_usage(user_id, usage_day)
    link = build_contact_link(employer_contact, draft) if employer_contact else None
    lines = ["✨ *Улучшенный черновик:*", "", draft]
    if not link and employer_contact:
        lines.append(manual_contact_hint(employer_contact, draft).lstrip("\n"))
    kb = [[_inline_btn("Открыть чат", url=link, style="success")]] if link else []
    await send_user_message_safe_buttons(
        user_id,
        topic_key=TOPIC_RESPONSES,
        text="\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None,
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("star_resp_"))
async def star_response_invoice(callback: types.CallbackQuery):
    if not STARS_ENABLED:
        await callback.answer("Оплата Stars временно недоступна", show_alert=True)
        return
    user_id = callback.from_user.id
    vacancy_id = callback.data.replace("star_resp_", "", 1)
    if has_star_purchase_for_vacancy(user_id, vacancy_id):
        await callback.answer("Уже оплачено для этой вакансии", show_alert=True)
        return
    payload = f"ext_resp:{vacancy_id}"
    create_star_purchase(user_id, vacancy_id, STARS_EXTENDED_RESPONSE_PRICE, payload)
    await bot.send_invoice(
        chat_id=user_id,
        title="Расширенный отклик",
        description="Приоритетный отклик с улучшенным текстом для заказчика",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Расширенный отклик", amount=STARS_EXTENDED_RESPONSE_PRICE)],
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout.id, ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    payment = message.successful_payment
    payload = payment.invoice_payload or ""
    if not payload.startswith("ext_resp:"):
        return
    purchase = complete_star_purchase(payload)
    if not purchase:
        await message.answer("Платёж получен, но запись не найдена. Напишите в поддержку.")
        return
    user_id = purchase["user_id"]
    vacancy_id = purchase["vacancy_id"]
    set_response_star_boost(user_id, vacancy_id)
    profile = get_subscriber_profile(user_id)
    row = get_vacancy_row(vacancy_id)
    if not profile or not row:
        await message.answer("✅ Оплата прошла. Откройте вакансию в «Мои отклики».")
        return
    vacancy_text = row[0] or ""
    employer_contact = row[3] or extract_contact_from_text(vacancy_text)
    prefix = "⭐ Приоритетный отклик через Promostaff Hunter\n\n"
    draft = build_candidate_profile_text(profile)
    if LLM_ENABLED and is_user_premium(user_id):
        from services.llm_client import ask_llm
        from services.llm_prompts import build_response_draft_prompt
        cat_row = fetchone("SELECT category_code FROM vacancies WHERE id = ?", (vacancy_id,))
        cat_code = cat_row[0] if cat_row else "promoter"
        enhanced = await ask_llm(build_response_draft_prompt(
            vacancy_text=vacancy_text,
            category_name=get_category_name(cat_code),
            profile_summary=build_candidate_profile_text(profile),
        ))
        if enhanced:
            draft = enhanced
    draft = prefix + draft
    link = build_contact_link(employer_contact, draft) if employer_contact else None
    from services.forum_topics import TOPIC_RESPONSES
    body = "✅ *Расширенный отклик активирован!*\n\n" + draft
    if not link and employer_contact:
        body += manual_contact_hint(employer_contact, draft)
    kb = [[_inline_btn("Открыть чат и отправить", url=link, style="success")]] if link else []
    await send_user_message_safe_buttons(
        user_id,
        topic_key=TOPIC_RESPONSES,
        text=body,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None,
    )


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
            "✅ Обновил черновик."
            + manual_contact_hint(contact, draft_text),
            parse_mode="Markdown",
        )
        await state.clear()
        return
    try:
        await message.answer(
            "✅ Обновил черновик. Откройте чат с заказчиком:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✅ Открыть чат и отправить", url=link)]]
            ),
        )
    except TelegramBadRequest as e:
        if "button_user_invalid" in str(e).lower() or "button_url_invalid" in str(e).lower():
            await message.answer(
                "✅ Обновил черновик."
                + manual_contact_hint(contact, draft_text),
                parse_mode="Markdown",
            )
        else:
            raise
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
    if parser_scan_in_progress():
        await status_msg.edit_text(
            "🔍 Ожидание парсера…\n"
            "Идёт синхронизация чатов (стартовая или ручная). Обычно 1–3 мин."
        )
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
    await send_parser_debug_report(message)

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
    if await admin_fsm_menu_escape(message, state):
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
    if await admin_fsm_menu_escape(message, state):
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
    await send_parser_debug_report(message)

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
    user_rows = []
    for card in cards:
        label = _admin_user_short_label(card)
        user_rows.append([
            InlineKeyboardButton(
                text=f"👤 {label}",
                callback_data=f"adm_u_{card['user_id']}_{page}",
            ),
        ])
    inline = user_rows + ([nav] if nav else [])
    markup = InlineKeyboardMarkup(inline_keyboard=inline) if inline else None
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


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_u_"))
async def admin_user_open_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    parts = callback.data.split("_")
    target_id = int(parts[2])
    cards_page = int(parts[3]) if len(parts) > 3 else 0
    await safe_callback_answer(callback)
    await show_admin_user_detail(callback.message, target_id, cards_page=cards_page, edit=True)


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_p_"))
async def admin_user_premium_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    parts = callback.data.split("_")
    target_id = int(parts[2])
    days = int(parts[3])
    cards_page = int(parts[4]) if len(parts) > 4 else 0
    was_active = is_user_premium(target_id)
    notified = await activate_premium_for_user(target_id, days)
    mode = "продлён" if was_active else "выдан"
    note = "" if notified else " (уведомление не доставлено)"
    await safe_callback_answer(callback, f"Premium {mode} +{days} дн.{note}", show_alert=not notified)
    await show_admin_user_detail(callback.message, target_id, cards_page=cards_page, edit=True)


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_f_"))
async def admin_user_free_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    parts = callback.data.split("_")
    target_id = int(parts[2])
    cards_page = int(parts[3]) if len(parts) > 3 else 0
    set_user_plan(target_id, plan="free")
    await safe_callback_answer(callback, "Premium снят")
    await show_admin_user_detail(callback.message, target_id, cards_page=cards_page, edit=True)


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_a_"))
async def admin_user_active_toggle_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    parts = callback.data.split("_")
    target_id = int(parts[2])
    cards_page = int(parts[3]) if len(parts) > 3 else 0
    profile = get_subscriber_profile(target_id)
    if not profile:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    new_active = not bool(profile.get("is_active"))
    set_subscriber_active(target_id, new_active)
    label = "активен" if new_active else "неактивен"
    await safe_callback_answer(callback, f"Статус: {label}")
    await show_admin_user_detail(callback.message, target_id, cards_page=cards_page, edit=True)


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_r_"))
async def admin_user_responses_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    parts = callback.data.split("_")
    target_id = int(parts[2])
    profile = get_subscriber_profile(target_id)
    name = _admin_user_short_label(profile or {"user_id": target_id})
    await safe_callback_answer(callback)
    await callback.message.answer(
        f"📨 Отклики пользователя <b>{escape_html(name)}</b> "
        f"(<code>{target_id}</code>):",
        parse_mode="HTML",
    )
    await send_responses_page(callback.message, target_id, page=0)

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
        await send_user_message(
            user_id,
            topic_key="support",
            text=f"📞 *Ответ от администратора:*\n\n{answer_text}",
            parse_mode="Markdown",
        )
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


async def _send_admin_xlsx(message: types.Message, caption: str, prefix: str, builder, rows_getter):
    if message.from_user.id != YOUR_USER_ID:
        return
    status = await message.answer("📥 Готовлю Excel…")
    try:
        rows = rows_getter()
        payload = builder(rows)
        fname = export_filename(prefix)
        await message.answer_document(
            BufferedInputFile(payload, filename=fname),
            caption=f"{caption}: {len(rows)} строк",
            reply_markup=get_admin_export_keyboard(),
        )
        await status.delete()
    except Exception as e:
        logger.exception("admin xlsx %s", prefix)
        await status.edit_text(f"❌ Ошибка выгрузки: {str(e)[:120]}")


@dp.message(lambda m: m.text == "📥 Excel: подписчики")
async def admin_export_subscribers(message: types.Message):
    await _send_admin_xlsx(
        message, "Подписчики", "subscribers",
        build_subscribers_xlsx, get_subscribers_export_rows,
    )


@dp.message(lambda m: m.text == "📥 Excel: вакансии")
async def admin_export_vacancies(message: types.Message):
    await _send_admin_xlsx(
        message, "Вакансии", "vacancies",
        build_vacancies_xlsx, get_vacancies_export_rows,
    )


@dp.message(lambda m: m.text == "📥 Excel: заказчики")
async def admin_export_employers(message: types.Message):
    await _send_admin_xlsx(
        message, "Заказчики", "employers",
        build_employers_xlsx, get_employers_export_rows,
    )


@dp.message(lambda m: m.text == "📥 Excel: не подходит")
async def admin_export_notfit(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await _send_admin_xlsx(
        message, "Не подходит", "notfit",
        build_notfit_xlsx, get_notfit_export_rows,
    )


@dp.message(lambda m: m.from_user.id == YOUR_USER_ID and m.text == ADMIN_BTN_HUB_PARSER)
async def admin_nav_parser(message: types.Message):
    await message.answer(
        "📡 *Парсер* — чаты, прогон, качество ленты.",
        parse_mode="Markdown",
        reply_markup=get_admin_parser_keyboard(),
    )


@dp.message(lambda m: m.from_user.id == YOUR_USER_ID and m.text == ADMIN_BTN_HUB_USERS)
async def admin_nav_users(message: types.Message):
    await message.answer(
        "👥 *Пользователи* — подписчики, Premium, отклики, поддержка.",
        parse_mode="Markdown",
        reply_markup=get_admin_users_keyboard(),
    )


@dp.message(lambda m: m.from_user.id == YOUR_USER_ID and m.text == ADMIN_BTN_HUB_EXPORT)
async def admin_nav_export(message: types.Message):
    await message.answer(
        "📥 *Excel* — выгрузки для анализа.",
        parse_mode="Markdown",
        reply_markup=get_admin_export_keyboard(),
    )


@dp.message(lambda m: m.from_user.id == YOUR_USER_ID and m.text == ADMIN_BTN_HUB_MOD)
async def admin_nav_mod(message: types.Message):
    await message.answer(
        "📝 *Модерация и канал* — очередь заказчиков и ручной кросс-пост.",
        parse_mode="Markdown",
        reply_markup=get_admin_mod_keyboard(),
    )


@dp.message(lambda m: m.from_user.id == YOUR_USER_ID and m.text == ADMIN_BTN_BACK)
async def admin_nav_back(message: types.Message):
    await message.answer("🏠 Главное админ-меню", reply_markup=get_admin_hub_keyboard())


@dp.message(lambda m: m.text == "📊 Шум по чатам")
async def admin_chat_noise_button(message: types.Message):
    await send_chat_noise_report(message)


@dp.message(lambda m: m.from_user.id == YOUR_USER_ID and m.text == "📺 Канал")
async def admin_nav_channel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "📺 *Канал* — лимиты, промо, новости и статистика.\n"
        "Настройки сохраняются в БД (без правок env на Bothost).",
        parse_mode="Markdown",
        reply_markup=get_admin_channel_keyboard(),
    )


@dp.message(lambda m: m.text == "📺 Статус канала")
async def admin_channel_status_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await send_channel_admin_status(message)


@dp.message(lambda m: m.text in {"📣 В канал", "📣 Вакансия в канал"})
async def admin_channel_post_start(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    if not _channel_env_ok():
        await message.answer(
            "❌ Канал не настроен на сервере (`CHANNEL_CROSSPOST_ENABLED` + `HUNTER_CHANNEL_ID`).",
            reply_markup=get_admin_channel_keyboard(),
        )
        return
    await state.set_state(ChannelPostState.waiting_vacancy_id)
    await message.answer(
        "📣 Отправьте *ID вакансии* для публикации в @promostaff_agency_job.\n"
        "Ручной пост — *без лимитов* и вне тихих часов.",
        parse_mode="Markdown",
    )


@dp.message(lambda m: m.text == "📢 Промо в канал")
async def admin_channel_promo_now(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    if not _channel_env_ok():
        await message.answer("❌ Канал не настроен.", reply_markup=get_admin_channel_keyboard())
        return
    from services.channel_promo import post_channel_promo
    ok = await post_channel_promo(bot, manual=True)
    if ok:
        await message.answer("✅ Промо-пост опубликован.", reply_markup=get_admin_channel_keyboard())
    else:
        await message.answer("❌ Не удалось опубликовать промо.", reply_markup=get_admin_channel_keyboard())


@dp.message(lambda m: m.text == "✏️ Тексты промо")
async def admin_channel_promo_texts_button(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    await state.clear()
    await send_promo_texts_admin_screen(message)


@dp.callback_query(lambda c: c.data and c.data.startswith("ch_promo_"))
async def admin_channel_promo_texts_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    data = callback.data
    from services.channel_promo_texts import (
        get_promo_variants,
        import_promo_from_file_to_db,
        pick_promo_text,
        reset_promo_texts_to_file_or_defaults,
    )
    from db import get_channel_promo_times

    if data == "ch_promo_file":
        variants, err = import_promo_from_file_to_db()
        if err:
            await safe_callback_answer(callback, err, show_alert=True)
            return
        await safe_callback_answer(callback, f"Загружено {len(variants)} текст(ов) из файла")
        await send_promo_texts_admin_screen(callback.message, edit=True)
        return
    if data == "ch_promo_reset":
        variants, source = reset_promo_texts_to_file_or_defaults()
        await safe_callback_answer(callback, f"Сброшено → {source}, слотов: {len(variants)}")
        await send_promo_texts_admin_screen(callback.message, edit=True)
        return
    if data == "ch_promo_preview":
        times = get_channel_promo_times()
        parts = ["<b>👁 Превью автопромо</b>", ""]
        for i, slot_time in enumerate(times):
            parts.append(f"<b>—— {i + 1}. {slot_time} ——</b>")
            parts.append(pick_promo_text(i))
            parts.append("")
        await callback.message.answer("\n".join(parts), parse_mode="HTML", disable_web_page_preview=True)
        await safe_callback_answer(callback, "Превью отправлено")
        return
    if data.startswith("ch_promo_edit_"):
        try:
            idx = int(data.replace("ch_promo_edit_", "", 1))
        except ValueError:
            await safe_callback_answer(callback, "Ошибка слота", show_alert=True)
            return
        times = get_channel_promo_times()
        slot_label = times[idx] if idx < len(times) else str(idx + 1)
        current = pick_promo_text(idx)
        await state.update_data(promo_text_slot=idx)
        await state.set_state(ChannelPromoTextState.waiting_text)
        await callback.message.answer(
            f"<b>✏️ Слот {idx + 1} ({slot_label})</b>\n\n"
            f"Текущий текст:\n{current}\n\n"
            "Отправьте новый текст (HTML: <code>&lt;b&gt;</code>, "
            "<code>&lt;a href=\"…\"&gt;</code>). «◀️ Назад» — отмена.",
            parse_mode="HTML",
            reply_markup=get_admin_channel_keyboard(),
        )
        await safe_callback_answer(callback)
        return
    await safe_callback_answer(callback)


@dp.message(ChannelPromoTextState.waiting_text)
async def admin_channel_promo_text_save(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    if await admin_fsm_menu_escape(message, state):
        return
    if message.text == ADMIN_BTN_BACK:
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_admin_channel_keyboard())
        return
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("Отправьте текст или «◀️ Назад».")
        return
    data = await state.get_data()
    idx = int(data.get("promo_text_slot", 0))
    from services.channel_promo_texts import update_promo_variant_at

    update_promo_variant_at(idx, text)
    await state.clear()
    await message.answer(
        f"✅ Слот {idx + 1} сохранён в БД. Следующий автопромо возьмёт этот текст.",
        reply_markup=get_admin_channel_keyboard(),
    )
    await send_promo_texts_admin_screen(message)


@dp.message(lambda m: m.text == "📊 Статистика канала")
async def admin_channel_stats_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    if not _channel_env_ok():
        await message.answer("❌ Канал не настроен.", reply_markup=get_admin_channel_keyboard())
        return
    from services.channel_stats import build_channel_stats_report
    report = await build_channel_stats_report(bot, HUNTER_CHANNEL_ID)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [_inline_btn("🔄 Обновить", callback_data="ch_stats_refresh")],
    ])
    await message.answer(report, parse_mode="HTML", reply_markup=markup)


@dp.message(lambda m: m.text == "📝 Новость в канал")
async def admin_channel_custom_post_start(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    if not _channel_env_ok():
        await message.answer("❌ Канал не настроен.", reply_markup=get_admin_channel_keyboard())
        return
    await state.set_state(ChannelCustomPostState.waiting_content)
    await state.update_data(custom_with_bot_button=True, custom_text="", custom_photo_file_id=None)
    await message.answer(
        "📝 *Новость / пост в канал*\n\n"
        "Отправьте текст (HTML: &lt;b&gt;, &lt;i&gt;, ссылки).\n"
        "Можно *фото с подписью* одним сообщением.\n\n"
        "«◀️ Назад» — отмена.",
        parse_mode="HTML",
    )


@dp.message(ChannelCustomPostState.waiting_content)
async def admin_channel_custom_post_content(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    if await admin_fsm_menu_escape(message, state):
        return
    if message.text == ADMIN_BTN_BACK:
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_admin_channel_keyboard())
        return
    text = (message.text or message.caption or "").strip()
    photo_id = message.photo[-1].file_id if message.photo else None
    if not text and not photo_id:
        await message.answer("Нужен текст или фото с подписью.")
        return
    await state.update_data(custom_text=text, custom_photo_file_id=photo_id)
    await send_custom_post_preview(message, state)


@dp.callback_query(lambda c: c.data and c.data.startswith("ch_custom"))
async def admin_channel_custom_post_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    if callback.data == "ch_custom_cancel":
        await state.clear()
        await safe_callback_answer(callback, "Отменено")
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Отменено.", reply_markup=get_admin_channel_keyboard())
        return
    data = await state.get_data()
    if callback.data == "ch_custom_btn":
        with_btn = not bool(data.get("custom_with_bot_button", True))
        await state.update_data(custom_with_bot_button=with_btn)
        from services.channel_custom_post import format_custom_post_preview
        text = data.get("custom_text") or ""
        preview = format_custom_post_preview(text, with_bot_button=with_btn)
        if data.get("custom_photo_file_id"):
            preview += "\n\n📷 <i>К посту будет приложено фото</i>"
        await callback.message.edit_text(
            preview,
            parse_mode="HTML",
            reply_markup=build_custom_post_confirm_keyboard(with_btn),
        )
        await safe_callback_answer(callback)
        return
    if callback.data == "ch_custom_pub":
        from services.channel_custom_post import post_custom_to_channel
        ok, result = await post_custom_to_channel(
            bot,
            text=data.get("custom_text") or "",
            photo_file_id=data.get("custom_photo_file_id"),
            with_bot_button=bool(data.get("custom_with_bot_button", True)),
        )
        await state.clear()
        await safe_callback_answer(callback, "Опубликовано" if ok else "Ошибка", show_alert=not ok)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        if ok:
            await callback.message.answer(
                f"✅ Пост опубликован в канал (`{result}`).",
                parse_mode="Markdown",
                reply_markup=get_admin_channel_keyboard(),
            )
        else:
            await callback.message.answer(
                f"❌ Не удалось: {escape_html(str(result))}",
                parse_mode="HTML",
                reply_markup=get_admin_channel_keyboard(),
            )


@dp.callback_query(lambda c: c.data == "ch_stats_refresh")
async def admin_channel_stats_refresh(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    if not _channel_env_ok():
        await callback.answer("Канал не настроен", show_alert=True)
        return
    from services.channel_stats import build_channel_stats_report
    report = await build_channel_stats_report(bot, HUNTER_CHANNEL_ID)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [_inline_btn("🔄 Обновить", callback_data="ch_stats_refresh")],
    ])
    try:
        await callback.message.edit_text(report, parse_mode="HTML", reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
    await safe_callback_answer(callback, "Обновлено")


@dp.callback_query(lambda c: c.data and c.data.startswith("ch_") and not c.data.startswith("ch_custom") and not c.data.startswith("ch_promo_") and c.data != "ch_stats_refresh")
async def admin_channel_settings_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    data = callback.data
    if data == "ch_t_xpost":
        set_channel_setting(
            "crosspost_enabled",
            "0" if is_channel_crosspost_enabled() else "1",
        )
    elif data == "ch_t_promo":
        set_channel_setting(
            "promo_enabled",
            "0" if is_channel_promo_enabled() else "1",
        )
    elif data == "ch_lim_tot_dec":
        set_channel_setting("hourly_limit_total", str(max(1, get_channel_hourly_limit_total() - 1)))
    elif data == "ch_lim_tot_inc":
        set_channel_setting("hourly_limit_total", str(min(24, get_channel_hourly_limit_total() + 1)))
    elif data == "ch_lim_ldr_dec":
        set_channel_setting("hourly_limit_loader", str(max(0, get_channel_hourly_limit_loader() - 1)))
    elif data == "ch_lim_ldr_inc":
        set_channel_setting("hourly_limit_loader", str(min(6, get_channel_hourly_limit_loader() + 1)))
    elif data == "ch_rate_dec":
        set_channel_setting("loader_min_rate", str(max(200, get_channel_loader_min_rate() - 50)))
    elif data == "ch_rate_inc":
        set_channel_setting("loader_min_rate", str(min(2000, get_channel_loader_min_rate() + 50)))
    await safe_callback_answer(callback, "Сохранено")
    await send_channel_admin_status(callback.message, edit=True)


@dp.message(ChannelPostState.waiting_vacancy_id)
async def admin_channel_post_vacancy_id(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    if await admin_fsm_menu_escape(message, state):
        return
    if message.text == ADMIN_BTN_BACK:
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_admin_channel_keyboard())
        return
    vacancy_id = (message.text or "").strip()
    if not vacancy_id:
        await message.answer("Укажите ID вакансии или «◀️ Назад».")
        return
    await state.clear()
    result = await channel_post_for_vacancy(vacancy_id, force=True)
    await message.answer(result, parse_mode="Markdown", reply_markup=get_admin_channel_keyboard())


@dp.chat_member()
async def channel_member_update(event: ChatMemberUpdated):
    if not HUNTER_CHANNEL_ID or event.chat.id != HUNTER_CHANNEL_ID:
        return
    old_s = event.old_chat_member.status
    new_s = event.new_chat_member.status
    user = event.new_chat_member.user
    uid = user.id if user else None
    uname = user.username if user else None
    if new_s in ("member", "administrator") and old_s in ("left", "kicked", "banned"):
        record_channel_member_event("join", uid, uname)
        logger.info("Channel join user_id=%s", uid)
    elif old_s in ("member", "restricted", "administrator") and new_s in ("left", "kicked", "banned"):
        record_channel_member_event("leave", uid, uname)
        logger.info("Channel leave user_id=%s", uid)


@dp.message(lambda m: m.text == "📝 Модерация вакансий")
async def admin_moderation_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    pending = get_pending_moderation_vacancies(10)
    if not pending:
        await message.answer("✅ Очередь модерации пуста.", reply_markup=get_admin_keyboard())
        return
    await message.answer(f"📝 *На модерации:* {len(pending)}", parse_mode="Markdown")
    for v in pending:
        preview = (v.get("message_text") or "")[:350]
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            _inline_btn("Опубликовать", callback_data=f"mod_ok_{v['id']}", style="success"),
            _inline_btn("Отклонить", callback_data=f"mod_no_{v['id']}", style="danger"),
            _inline_btn("📣 Канал", callback_data=f"mod_ch_{v['id']}"),
        ]])
        await message.answer(
            f"*{get_category_name(v['category_code'])}* · `{v['id']}`\n"
            f"Контакт: {v.get('author_contact') or '—'}\n\n"
            f"{escape_markdown(preview)}",
            parse_mode="Markdown",
            reply_markup=markup,
        )


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

    from services.channel_promo import channel_promo_scheduler_loop
    spawn_background_task(channel_promo_scheduler_loop(bot))
    logger.info("📺 Планировщик промо канала: 09:00, 14:00, 20:00 МСК")

    async def channel_snapshot_loop():
        import asyncio
        from services.channel_stats import fetch_and_store_member_count
        while True:
            try:
                if HUNTER_CHANNEL_ID:
                    await fetch_and_store_member_count(bot, HUNTER_CHANNEL_ID)
            except Exception as e:
                logger.warning("channel_snapshot_loop: %s", e)
            await asyncio.sleep(6 * 3600)

    spawn_background_task(channel_snapshot_loop())

    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="🏠 Главное меню"),
            BotCommand(command="help", description="📖 Как пользоваться"),
        ])
        logger.info("✅ Меню команд Telegram: /start, /help")
    except Exception as e:
        logger.warning(f"Не удалось set_my_commands: {e}")

    logger.info("📡 Запуск polling...")
    allowed = list(set(dp.resolve_used_update_types() + ["chat_member"]))
    await dp.start_polling(bot, allowed_updates=allowed)

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
