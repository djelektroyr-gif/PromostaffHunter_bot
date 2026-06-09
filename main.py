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
    format_parser_wait_message, format_scan_finished_summary, LAST_DEBUG_STATS,
    format_reject_samples_report, format_channel_coverage_report, run_parser_audit,
    get_stats_for_filter_reports,
    PARSER_SCAN_TIMEOUT_SEC,
)
from admin_exports import (
    build_subscribers_xlsx, build_vacancies_xlsx, build_employers_xlsx,
    build_notfit_xlsx, build_responses_xlsx, export_filename,
)
from config import (
    BOT_TOKEN, YOUR_USER_ID, SUBSCRIPTION_PAY_URL, SUBSCRIPTION_SUPPORT,
    SUBSCRIPTION_PRICE_RUB, SUBSCRIPTION_CARD_HINT, TRIAL_DAYS, PREMIUM_RENEWAL_REMIND_DAYS,
    FREE_CATEGORY_LIMIT,
    VACANCY_MAX_AGE_HOURS,
    FEED_FRESH_HOURS, FEED_ARCHIVE_MAX_HOURS,
    FORUM_TOPICS_ENABLED, CHANNEL_CROSSPOST_ENABLED, HUNTER_CHANNEL_ID,
    LLM_ENABLED, LLM_DAILY_LIMIT_PREMIUM, STARS_ENABLED, STARS_EXTENDED_RESPONSE_PRICE,
)
from profile_photos import (
    get_user_photos_dir, persist_user_photo, photo_health_loop, send_profile_photo,
)
from services.fsm_storage import create_fsm_storage

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
    from services.vacancy_enrichment import enrich_vacancy_text

    enrichment = enrich_vacancy_text(body, legacy_address=address)
    if enrichment.address_normalized:
        address = enrichment.address_normalized
    enrich_kwargs = enrichment.to_db_kwargs()
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
        **enrich_kwargs,
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
        [
            InlineKeyboardButton(text="🗑 Удалить из бота", callback_data=f"mod_del_{vacancy_id}"),
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
        "address_normalized": row[13] if len(row) > 13 else None,
        "location_lat": row[14] if len(row) > 14 else None,
        "location_lon": row[15] if len(row) > 15 else None,
        "dedupe_key": row[7],
        "published_at": row[8],
        "poster_user_id": row[9],
        "poster_username": row[10],
        "from_bot_employer": True,
    }

BTN_SETTINGS = "⚙️ Настройки"
BTN_METRO = "📍 Станции метро"  # legacy alias → 🎯 Фильтры Premium
BTN_PREMIUM_FILTERS = "🎯 Фильтры Premium"
BTN_SETTINGS_CATEGORIES = "📌 Категории вакансий"
BTN_SETTINGS_BACK = "◀️ В главное меню"
BTN_MY_DATA = "👤 Мои данные"
BTN_SETTINGS_LEGACY = "📋 Категории"
BTN_MY_DATA_LEGACY = "📞 Мои контакты"
BTN_UNSUB_LEGACY = "❌ Отписаться"

bot = Bot(token=BOT_TOKEN)
storage = create_fsm_storage()
dp = Dispatcher(storage=storage)

from handlers.premium_filters import router as premium_filters_router

if premium_filters_router.parent_router is None:
    dp.include_router(premium_filters_router)

# Flood control для рассылки (пауза между отправками)
SEND_DELAY = 1  # секунда между push-вакансиями одному пользователю
MSK_TZ = timezone(timedelta(hours=3))
_vacancy_push_sem = asyncio.Semaphore(2)  # не более 2 параллельных push-рассылок
BROADCAST_DELAY = 0.08  # ~12 msg/s — безопаснее для Bot API при массовой рассылке
RESPONSES_PAGE_SIZE = 5
PREMIUM_DEFAULT_DAYS = 30
GIFT_PRESET_DAYS = (7, 14, 30, 90)
_processing_finish: set[int] = set()
_broadcast_lock = asyncio.Lock()


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


def build_maps_url(
    address: str | None = None,
    *,
    address_normalized: str | None = None,
    location_lat: float | None = None,
    location_lon: float | None = None,
) -> str | None:
    from services.vacancy_enrichment import build_maps_url as _build

    return _build(
        address=address,
        address_normalized=address_normalized,
        location_lat=location_lat,
        location_lon=location_lon,
    )


def _map_fields_from_vacancy(
    vac: dict | None = None,
    *,
    address: str | None = None,
    address_normalized: str | None = None,
    location_lat: float | None = None,
    location_lon: float | None = None,
) -> dict:
    if vac:
        address = vac.get("address", address)
        address_normalized = vac.get("address_normalized", address_normalized)
        location_lat = vac.get("location_lat", location_lat)
        location_lon = vac.get("location_lon", location_lon)
    return {
        "address": address,
        "address_normalized": address_normalized,
        "location_lat": location_lat,
        "location_lon": location_lon,
    }


def _map_fields_from_push_row(row) -> dict:
    if not row:
        return _map_fields_from_vacancy()
    return _map_fields_from_vacancy(
        address=row[4],
        address_normalized=row[13] if len(row) > 13 else None,
        location_lat=row[14] if len(row) > 14 else None,
        location_lon=row[15] if len(row) > 15 else None,
    )


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


def build_vacancy_keyboard(
    vacancy_id: str,
    address: str | None = None,
    *,
    address_normalized: str | None = None,
    location_lat: float | None = None,
    location_lon: float | None = None,
    responded: bool = False,
) -> InlineKeyboardMarkup:
    """Inline-кнопки с цветами Bot API 9.4 (style на InlineKeyboardButton)."""
    if responded:
        buttons = [[InlineKeyboardButton(text="✅ Отклик отправлен", callback_data="already_responded")]]
    else:
        buttons = [[_inline_btn("Откликнуться", callback_data=f"respond_{vacancy_id}", style="success")]]
    maps_url = build_maps_url(
        address,
        address_normalized=address_normalized,
        location_lat=location_lat,
        location_lon=location_lon,
    )
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
    del source, message_link  # в боте не показываем группу-источник и ссылку на чужой чат
    from services.vacancy_public_text import sanitize_vacancy_public_body

    description = sanitize_vacancy_public_body(body or "", max_len=500)
    if not description:
        description = "Откройте карточку в боте — там кнопка «Отклик»."
    pub_line = ""
    if published_at and published_at not in ("сейчас", "—"):
        pub_line = f"🕐 <i>Опубликовано: {escape_html(published_at)}</i>\n\n"
    return (
        f"{category_emoji} <b>{escape_html(category_name)}</b> · {escape_html(freshness)}\n"
        f"{pub_line}"
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


async def _mark_vacancy_card_responded(
    callback: types.CallbackQuery,
    vacancy_id: str,
    address: str | None = None,
) -> None:
    """Убирает повторный отклик — меняет inline-клавиатуру на карточке вакансии."""
    if not callback.message:
        return
    try:
        await callback.message.edit_reply_markup(
            reply_markup=build_vacancy_keyboard(vacancy_id, address, responded=True),
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.debug("mark_vacancy_card_responded: %s", e)


async def _offer_feed_session_restart(message: types.Message) -> None:
    await message.answer(
        "⏱ *Сессия ленты истекла* — бот перезапускался или прошло много времени.\n\n"
        "Откройте вакансии заново:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Открыть ленту", callback_data="feed_pick_mode")],
        ]),
    )


def _free_category_hint_short() -> str:
    if FREE_CATEGORY_LIMIT == 1:
        return "1 категория бесплатно"
    return f"до {FREE_CATEGORY_LIMIT} категорий на Free"


def category_picker_text(selected_count: int, user_id: int, hint: str = "") -> str:
    profile = get_subscriber_profile(user_id)
    trial_used = bool(profile and profile.get("trial_used"))
    if is_user_premium(user_id):
        limit_line = f"💎 Premium: все категории и push. Выбрано: *{selected_count}*."
    else:
        trial_line = ""
        if not trial_used and TRIAL_DAYS > 0:
            trial_line = (
                f"\n🎁 Пробный Premium *{TRIAL_DAYS} дн.* — все категории и push "
                f"(один раз после «✅ Завершить выбор»)."
            )
        limit_line = (
            f"🆓 *Free:* {_free_category_hint_short()} — только лента «🔍 Посмотреть…», без push.\n"
            f"💎 *2 категории и больше* + моментальные push — Premium "
            f"({escape_markdown(SUBSCRIPTION_PRICE_RUB)} ₽/мес).{trial_line}\n"
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
            text="💎 Premium — несколько категорий и push",
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


async def send_category_picker(
    chat_id: int,
    user_id: int,
    selected_codes: list = None,
    *,
    hint: str = "",
    deeplink_category: str | None = None,
):
    if deeplink_category and not get_user_categories(user_id):
        from services.onboarding_deeplink import apply_vacancy_deeplink_category_preselect
        pre_hint = await run_db(
            apply_vacancy_deeplink_category_preselect,
            user_id,
            deeplink_category,
            free_limit=FREE_CATEGORY_LIMIT,
        )
        if pre_hint:
            hint = f"{pre_hint}\n\n{hint}" if hint else pre_hint
    if selected_codes is None:
        selected_codes = [c["code"] for c in get_user_categories(user_id)]
    return await bot.send_message(
        chat_id,
        category_picker_text(len(selected_codes), user_id, hint),
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


def premium_request_admin_keyboard(request_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"✅ Оплата · {PREMIUM_DEFAULT_DAYS} дн.",
                callback_data=f"pr_a_{request_id}",
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"pr_r_{request_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🎁 +7 дн.",
                callback_data=f"pr_g_{request_id}_7",
            ),
            InlineKeyboardButton(
                text="🎁 +14 дн.",
                callback_data=f"pr_g_{request_id}_14",
            ),
            InlineKeyboardButton(
                text="🎁 +30 дн.",
                callback_data=f"pr_g_{request_id}_30",
            ),
        ],
        [
            InlineKeyboardButton(
                text="👤 Карточка",
                callback_data=f"adm_u_{user_id}_0",
            ),
        ],
    ])


def format_premium_payment_details_html(user_id: int) -> str:
    """Реквизиты + сумма + ID для экрана подписки и запроса Premium."""
    lines = []
    if SUBSCRIPTION_CARD_HINT:
        lines.append(f"💳 <b>Реквизиты:</b> {escape_html(SUBSCRIPTION_CARD_HINT)}")
    lines.append(f"💰 <b>Сумма:</b> {escape_html(SUBSCRIPTION_PRICE_RUB)} ₽/мес")
    lines.append(f"В комментарии к переводу укажите ID: <code>{user_id}</code>")
    return "\n".join(lines)


def format_premium_request_admin_caption(req: dict) -> str:
    """Legacy Markdown — предпочтительно HTML-версия ниже."""
    title = "💳 *Запрос продления Premium*" if req.get("is_renewal") else "💳 *Запрос Premium*"
    pending = count_pending_premium_requests()
    uname = req.get("username")
    uname_line = f"@{escape_markdown(uname)}" if uname else "—"
    return (
        f"{title} #{req['id']} (в очереди: {pending})\n\n"
        f"👤 {escape_markdown(req.get('full_name') or '—')}\n"
        f"ID: `{req['user_id']}`\n"
        f"Username: {uname_line}\n"
        f"📞 {escape_markdown(str(req.get('phone') or '—'))}\n"
        f"📋 {escape_markdown(str(req.get('category_codes') or '—'))}\n"
        f"🕐 {req.get('created_at') or '—'}"
    )


def format_premium_request_admin_caption_html(req: dict) -> str:
    title = "💳 <b>Запрос продления Premium</b>" if req.get("is_renewal") else "💳 <b>Запрос Premium</b>"
    pending = count_pending_premium_requests()
    uname = req.get("username")
    uname_line = f"@{escape_html(uname)}" if uname else "—"
    return (
        f"{title} #{req['id']} (в очереди: {pending})\n\n"
        f"👤 {escape_html(req.get('full_name') or '—')}\n"
        f"ID: <code>{req['user_id']}</code>\n"
        f"Username: {uname_line}\n"
        f"📞 {escape_html(str(req.get('phone') or '—'))}\n"
        f"📋 {escape_html(str(req.get('category_codes') or '—'))}\n"
        f"🕐 {escape_html(str(req.get('created_at') or '—'))}\n\n"
        f"📎 Чек во вложении"
    )


async def send_admin_premium_request_alert(request_id: int):
    if not YOUR_USER_ID:
        return
    req = get_premium_request(request_id)
    if not req or req.get("status") != "pending":
        logger.warning(
            "premium alert skip #%s: status=%s",
            request_id,
            req.get("status") if req else None,
        )
        return
    caption_html = format_premium_request_admin_caption_html(req)
    caption_plain = re.sub(r"<[^>]+>", "", caption_html)
    markup = premium_request_admin_keyboard(request_id, req["user_id"])
    file_id = req.get("receipt_file_id")
    kind = req.get("receipt_kind")

    async def _deliver(caption: str, parse_mode: str | None):
        kwargs = {"caption": caption, "reply_markup": markup}
        if parse_mode:
            kwargs["parse_mode"] = parse_mode
        if file_id and kind == "photo":
            await bot.send_photo(YOUR_USER_ID, file_id, **kwargs)
        elif file_id and kind == "document":
            await bot.send_document(YOUR_USER_ID, file_id, **kwargs)
        else:
            await bot.send_message(YOUR_USER_ID, caption, reply_markup=markup, parse_mode=parse_mode)

    try:
        await _deliver(caption_html, "HTML")
    except TelegramBadRequest as e:
        logger.warning("premium alert #%s HTML failed: %s — retry plain", request_id, e)
        try:
            await _deliver(caption_plain, None)
        except Exception as e2:
            logger.exception("premium_request notify admin #%s: %s", request_id, e2)
    except Exception as e:
        logger.exception("premium_request notify admin #%s: %s", request_id, e)


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


def format_gift_premium_user_message(days: int, paid_until: str | None) -> str:
    until_line = f"\nДействует до: *{paid_until}*" if paid_until else ""
    return (
        f"🎁 *Premium продлён на {days} дн.*{until_line}\n\n"
        "Мы добавили дни в подарок — спасибо, что пользуетесь ботом!\n\n"
        "• моментальные push-уведомления\n"
        "• все категории без лимита\n"
        "• фильтр по метро (⚙️ Настройки → 📍 Станции метро)\n\n"
        "Категории — кнопка «⚙️ Настройки»."
    )


async def gift_premium_for_user(target_id: int, days: int) -> bool:
    """Продлить Premium админом с текстом «в подарок» (trial/компенсация)."""
    if days <= 0 or days > 365:
        return False
    set_user_plan(target_id, plan="premium", days=days, extend=True)
    resolve_premium_requests(target_id)
    profile = get_subscriber_profile(target_id)
    paid_until = format_db_date_short(profile.get("paid_until")) if profile else ""
    try:
        await bot.send_message(
            target_id,
            format_gift_premium_user_message(days, paid_until),
            parse_mode="Markdown",
        )
        return True
    except Exception as e:
        logger.warning(f"Не удалось уведомить {target_id} о подарке Premium: {e}")
        return False


def admin_gift_days_keyboard(user_id: int, cards_page: int = 0) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for days in GIFT_PRESET_DAYS:
        row.append(InlineKeyboardButton(
            text=f"🎁 +{days} дн.",
            callback_data=f"adm_gd_{user_id}_{days}_{cards_page}",
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton(
            text="✏️ Другое число дней",
            callback_data=f"adm_gx_{user_id}_{cards_page}",
        ),
    ])
    rows.append([
        InlineKeyboardButton(
            text="◀️ К карточке",
            callback_data=f"adm_u_{user_id}_{cards_page}",
        ),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    keyboard = build_vacancy_keyboard(vacancy_id, **_map_fields_from_push_row(row))
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
    elif text == "📋 Примеры отсева":
        await send_reject_samples_report(message)
    elif text == "📡 Покрытие каналов":
        await send_channel_coverage_report(message)
    elif text == "📡 Парсер":
        await send_admin_parser_intro(message)
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
    await answer_admin_report(message, format_chat_noise_report(get_stats_for_filter_reports()))


async def send_reject_samples_report(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await answer_admin_report(message, format_reject_samples_report(get_stats_for_filter_reports()))


async def send_channel_coverage_report(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    db_rows = await run_db(get_vacancy_counts_by_chat, 7)
    db_map = {
        r["source_chat_title"]: r["count"]
        for r in db_rows
        if r.get("source_chat_title")
    }
    await answer_admin_report(message, format_channel_coverage_report(get_stats_for_filter_reports(), db_map))

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


async def run_topic_broadcast(
    admin_chat_id: int,
    text: str,
    *,
    topic_key: str,
    body_prefix: str,
    status_msg: types.Message = None,
):
    """Рассылка всем подписчикам в forum-топик (support и т.д.)."""
    subscribers = get_all_subscribers()
    if not subscribers:
        return 0, 0, "Нет подписчиков."
    if not status_msg:
        status_msg = await bot.send_message(
            admin_chat_id,
            f"📣 Рассылка в топик «{topic_key}» — {len(subscribers)} подписчиков…",
        )
    sent, failed = 0, 0
    body = f"{body_prefix}{text}"
    for i, uid in enumerate(subscribers, 1):
        try:
            await send_user_message(uid, topic_key=topic_key, text=body, parse_mode="Markdown")
            sent += 1
        except Exception as e:
            failed += 1
            if "bot was blocked" in str(e).lower():
                logger.info(f"Тopic-рассылка: {uid} заблокировал бота")
                _mark_subscriber_blocked_if_needed(uid)
            else:
                logger.warning(f"Topic-рассылка: ошибка {uid}: {e}")
        if i % 25 == 0 or i == len(subscribers):
            try:
                await status_msg.edit_text(
                    f"📣 Рассылка… {i}/{len(subscribers)}\n✅ {sent} | ❌ {failed}"
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
            if PREMIUM_RENEWAL_REMIND_DAYS > 0 and days_left <= PREMIUM_RENEWAL_REMIND_DAYS:
                status += (
                    f"\n⏳ Осталось <b>{max(days_left, 0)}</b> дн. — "
                    "продлите кнопками ниже."
                )
    else:
        status = (
            "🆓 <b>Бесплатный доступ</b>\n"
            "Лента «🔍 Посмотреть новые вакансии» — без моментальных push"
        )
    from handlers.premium_filters import get_saved_filters_hint

    saved_hint = get_saved_filters_hint(user_id)
    if saved_hint:
        status += f"\n\n{saved_hint}"
    if is_user_premium(user_id):
        prefs = get_subscriber_filter_prefs_effective(user_id)
        if prefs:
            from services.filter_prefs import format_prefs_summary

            status += f"\n\n🎯 Фильтры: {escape_html(format_prefs_summary(prefs))}"
    pay_lines = [format_premium_payment_details_html(user_id)]
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
        f"• 🎯 фильтры Premium: география, ставка (⚙️ Настройки)\n\n"
        f"<b>Free:</b> {_free_category_hint_short()}, только лента без push\n\n"
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

class TechBroadcastState(StatesGroup):
    waiting_for_text = State()

class AdminSupportReplyState(StatesGroup):
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


class DeleteVacancyState(StatesGroup):
    waiting_for_id = State()


class ChannelPromoTextState(StatesGroup):
    waiting_text = State()


class AdminGiftPremiumState(StatesGroup):
    waiting_for_days = State()

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
        f"(хелпер, промо, грузчик…). Free: {_free_category_hint_short()}, "
        f"2+ категории — Premium.\n"
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


def build_admin_parser_help_html() -> str:
    return (
        "<b>📡 Парсер — как пользоваться</b>\n\n"
        "<b>Зачем что</b>\n"
        "• <b>🔍 Ручная проверка</b> — забрать <i>новые</i> посты в ленту и БД "
        "(incremental, только то, что ещё не обработано).\n"
        "• <b>🔬 Аудит фильтра</b> — диагностика: последние ~20 постов из <i>каждого</i> чата "
        "прогоняются через фильтр <b>без сохранения</b>. Нужен, когда непонятно, что отсеивается.\n"
        "• <b>📡 Покрытие каналов</b> — кто реально дал вакансии в БД за 7 дней и кто «молчит».\n"
        "• <b>📋 Примеры отсева</b> — тексты отброшенных постов + причина "
        "(нет контакта, кастинг, качество роли и т.д.).\n"
        "• <b>📊 Шум по чатам</b> — доля отсева vs попаданий в ленту по каждому чату.\n"
        "• <b>📝 Отчёт парсера</b> — цифры последнего прогона (старт, финиш, причины).\n"
        "• <b>📋 Список чатов парсинга</b> — доступ Telethon (✅/❌) и мониторинг 📡.\n\n"
        "<b>Сценарий: «мало вакансий / только 2–3 чата»</b>\n"
        "1. <b>📊 Статистика</b> или /status — парсер «подключён», чатов в мониторинге ≈ числу в БД.\n"
        "2. <b>📡 Покрытие каналов</b> — кто даёт в БД; «молчащие» — норма, если там редко постят подходящие роли.\n"
        "3. <b>🔬 Аудит фильтра</b> — подождать 5–15 мин (36 чатов). После — пункты 4–5.\n"
        "4. <b>📋 Примеры отсева</b> — если пост выглядит вакансией, а отсеян — запомните номер примера.\n"
        "5. <b>📊 Шум по чатам</b> — какой чат шумный, топ-причина отсева.\n\n"
        "<b>Сценарий: забрать свежие посты в бот</b>\n"
        "• <b>🔍 Ручная проверка</b> (/check_now) — после перезапуска может писать «ожидание» "
        "5–15 мин (идёт стартовая синхронизация), затем итог.\n"
        "• Realtime и плановый прогон (~5 мин) работают сами; ручная проверка — если нужно сейчас.\n\n"
        "<b>Чаты и роли</b>\n"
        "• Чат с ❌ в списке — аккаунт парсера не в группе или нет доступа.\n"
        "• <code>/setchatroles @channel promoter,helper,loader</code> — ожидаемые роли для чата.\n"
        "• <code>/addchat</code>, <code>/removechat</code> — добавить/убрать чат.\n\n"
        "<b>Команды парсера</b>\n"
        "/check_now — ручная проверка · /audit_filter — аудит · /debug_last — отчёт"
    )


def build_admin_help_html() -> str:
    return (
        "<b>📖 Админ: как пользоваться ботом</b>\n\n"
        "Главное меню — разделы «📡 Парсер», «👥 Пользователи», «📥 Excel», «📝 Модерация». "
        "«◀️ Назад» — наверх. Справка всегда здесь: /help или «📖 Как пользоваться».\n\n"
        + build_admin_parser_help_html()
        + "\n\n"
        "<b>👥 Пользователи</b>\n"
        "• «🗂️ Карточки пользователей» — профиль, категории, Premium, отклики.\n"
        "• «⏳ Premium истекает» — trial/подписка кончается в ближайшие 7 дн.; кнопка «🎁 Подарить дни».\n"
        "• «💎 Запросы Premium» — чек на оплату, ✅ оплата или 🎁 подарок N дн.\n"
        "• «📋 Список откликов» — отклики кандидатов на вакансии.\n"
        "• «⚠️ Жалобы», «❓ Поддержка (админ)» — обращения; push + кнопка «✉️ Ответить».\n"
        "• «📣 Техсообщение» — рассылка в топик «❓ Поддержка» (техработы, важное).\n"
        "• /user USER_ID — карточка по ID · /setplan USER_ID premium 30 — выдать Premium.\n\n"
        "<b>📥 Excel</b>\n"
        "Выгрузки: подписчики, вакансии, заказчики, отклики, «не подходит» (feedback с причинами).\n\n"
        "<b>📝 Модерация и канал</b>\n"
        "• «📝 Модерация вакансий» — очередь на approve/reject.\n"
        "• «🗑 Удалить вакансию» — полное удаление из базы и у подписчиков (спам/ошибка парсера).\n"
        "• «📺 Канал» — лимиты автопоста, промо, новости, статистика @promostaff_agency_job.\n"
        "• «📣 В канал» — ручная публикация вакансии по ID (без лимитов).\n"
        "• /delvac ID — удалить вакансию по ID.\n"
        "• /enrich_backfill — пересчитать адрес/ставку/координаты (3 дня).\n\n"
        "<b>Общие команды</b>\n"
        "/start — админ-меню · /admin — дашборд · /status — статус парсера и счётчики · /help — эта справка"
    )


async def send_admin_parser_intro(message: types.Message):
    """Экран раздела «Парсер»: краткая инструкция + клавиатура."""
    text = build_admin_parser_help_html()
    try:
        if len(text) > 3800:
            await send_long_message(message.chat.id, text, parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    except TelegramBadRequest:
        plain = re.sub(r"<[^>]*>", "", text)
        await message.answer(plain, disable_web_page_preview=True)
    await message.answer("👇 Кнопки парсера", reply_markup=get_admin_parser_keyboard())


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
    prefs = get_subscriber_filter_prefs_raw(user_id)
    if prefs:
        from services.filter_prefs import format_prefs_summary
        filters_line = format_prefs_summary(prefs)
    else:
        metro = (profile.get("metro_zones") or "").strip()
        filters_line = f"метро: {metro}" if metro else "все локации"
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
        f"Фильтры: {escape_html(filters_line)}",
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
        [InlineKeyboardButton(
            text="🎁 Подарить Premium",
            callback_data=f"adm_gt_{user_id}_{p}",
        )],
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


def build_response_draft_message(
    *,
    employer_contact: str,
    required_fields: list,
    draft_text: str,
    contact_link: str | None,
) -> str:
    req_line = ", ".join(required_fields) if required_fields else "явных требований не найдено"
    msg = (
        "📨 *Черновик отклика готов*\n\n"
        f"👨‍💼 Контакт заказчика: `{escape_markdown(employer_contact)}`\n"
        f"🧾 Что просит вакансия: {escape_markdown(req_line)}\n\n"
    )
    if contact_link:
        msg += (
            "Нажмите кнопку ниже — откроется чат с заказчиком и готовым текстом.\n"
            "Перед отправкой можно отредактировать сообщение.\n\n"
            "_Карточка отклика — в «📨 Мои отклики». Заказчику нужно отправить сообщение вручную кнопкой выше._"
        )
    else:
        msg += manual_contact_hint(employer_contact, draft_text).lstrip("\n")
    return msg


def build_response_action_keyboard(
    user_id: int,
    vacancy_id: str,
    *,
    contact_link: str | None,
    include_stars: bool = True,
) -> InlineKeyboardMarkup:
    buttons = []
    if contact_link:
        buttons.append([InlineKeyboardButton(text="✅ Открыть чат и отправить", url=contact_link)])
    buttons.append([InlineKeyboardButton(text="✏️ Добавить комментарий", callback_data=f"respond_add_{vacancy_id}")])
    if LLM_ENABLED and is_user_premium(user_id):
        buttons.append([_inline_btn("✨ Улучшить текст", callback_data=f"respond_llm_{vacancy_id}", style="primary")])
    if include_stars and STARS_ENABLED and not has_star_purchase_for_vacancy(user_id, vacancy_id):
        buttons.append([_inline_btn("⭐ Расширенный отклик", callback_data=f"star_resp_{vacancy_id}")])
    buttons.append([InlineKeyboardButton(text="🚫 Отмена", callback_data="respond_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def deliver_response_draft(
    user_id: int,
    *,
    employer_contact: str,
    source_chat: str | None,
    required_fields: list,
    draft_text: str,
    vacancy_id: str,
) -> str:
    """Отправить черновик пользователю. Возвращает draft_status: delivered | manual."""
    from services.forum_topics import TOPIC_RESPONSES

    contact_link = build_contact_link(employer_contact, draft_text)
    msg = build_response_draft_message(
        employer_contact=employer_contact,
        required_fields=required_fields,
        draft_text=draft_text,
        contact_link=contact_link,
    )
    markup = build_response_action_keyboard(user_id, vacancy_id, contact_link=contact_link)
    await send_user_message_safe_buttons(
        user_id,
        topic_key=TOPIC_RESPONSES,
        text=msg,
        parse_mode="Markdown",
        reply_markup=markup,
    )
    return "manual" if not contact_link else "delivered"


async def notify_admin_response_issue(
    user_id: int,
    vacancy_id: str,
    *,
    source_chat: str | None,
    employer_contact: str | None,
    reason: str,
):
    profile = get_subscriber_profile(user_id)
    name = (profile or {}).get("full_name") or (profile or {}).get("first_name") or str(user_id)
    text = (
        "⚠️ *Проблема с откликом*\n\n"
        f"👤 {escape_markdown(name)} · `{user_id}`\n"
        f"🆔 вакансия: `{escape_markdown(vacancy_id)}`\n"
        f"📢 {escape_markdown(source_chat or '—')}\n"
        f"👨‍💼 {escape_markdown(employer_contact or '—')}\n\n"
        f"_{escape_markdown(reason)}_\n\n"
        f"Команда: `/user {user_id}` · карточка отклика в «📋 Список откликов»"
    )
    try:
        await bot.send_message(YOUR_USER_ID, text, parse_mode="Markdown")
    except Exception as e:
        logger.warning("notify_admin_response_issue: %s", e)


def _admin_response_user_label(resp: dict) -> str:
    return resp.get("user_full_name") or resp.get("user_first_name") or resp.get("user_username") or str(resp.get("user_id"))


def build_user_response_card_keyboard(resp: dict, *, for_admin: bool = False) -> InlineKeyboardMarkup:
    rows = []
    contact = resp.get("employer_contact") or resp.get("author_contact")
    profile = get_subscriber_profile(resp["user_id"]) if resp.get("user_id") else None
    draft_text = build_candidate_profile_text(profile) if profile else ""
    contact_link = build_contact_link(contact, draft_text) if contact and draft_text else None
    if for_admin and resp.get("vacancy_link"):
        rows.append([InlineKeyboardButton(text="🔗 Оригинал в группе", url=resp["vacancy_link"])])
    if contact_link:
        rows.append([InlineKeyboardButton(text="💬 Заказчик", url=contact_link)])
    if resp.get("vacancy_id") and not for_admin:
        rows.append([InlineKeyboardButton(text="⚠️ Пожаловаться", callback_data=f"complain_{resp['vacancy_id']}")])
    if for_admin:
        uid = resp["user_id"]
        rid = resp["id"]
        rows.append([
            InlineKeyboardButton(text="👤 Пользователь", callback_data=f"adm_u_{uid}_0"),
            InlineKeyboardButton(text="🔄 Черновик", callback_data=f"adm_resp_resend_{rid}"),
        ])
        rows.append([
            InlineKeyboardButton(text="🗑 Сбросить отклик", callback_data=f"adm_resp_reset_{rid}"),
        ])
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="resp_list_0" if not for_admin else "adm_resp_list_0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

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
    keyboard = build_vacancy_keyboard(
        vacancy_id,
        address=address,
        address_normalized=order.get("address_normalized"),
        location_lat=order.get("location_lat"),
        location_lon=order.get("location_lon"),
    )

    sent_count = 0
    skipped_free = 0
    skipped_filter = 0
    skipped_quiet = 0
    skipped_busy = 0
    skipped_feed_only = 0
    from services.subscriber_match import build_vacancy_match_dict, vacancy_matches_subscriber
    from services.push_notify import evaluate_push_delivery
    from db import add_push_digest_pending

    vac_match = build_vacancy_match_dict(
        message_text=msg_text,
        address=address,
        address_normalized=order.get("address_normalized") or (mod_row[13] if mod_row else None),
        category_code=category_code,
        geo_tags=order.get("geo_tags") or (mod_row[16] if mod_row and len(mod_row) > 16 else None),
        rate_hourly=order.get("rate_hourly") or (mod_row[17] if mod_row and len(mod_row) > 17 else None),
        rate_shift=order.get("rate_shift") or (mod_row[18] if mod_row and len(mod_row) > 18 else None),
        rate_effective_hourly=order.get("rate_effective_hourly") or (
            mod_row[19] if mod_row and len(mod_row) > 19 else None
        ),
        shift_date=order.get("shift_date") or (mod_row[20] if mod_row and len(mod_row) > 20 else None),
        shift_time_start=order.get("shift_time_start") or (
            mod_row[21] if mod_row and len(mod_row) > 21 else None
        ),
        location_lat=order.get("location_lat") or (mod_row[14] if mod_row else None),
        location_lon=order.get("location_lon") or (mod_row[15] if mod_row else None),
    )
    for subscriber in subscribers:
        if not is_user_premium(subscriber['user_id']):
            skipped_free += 1
            continue
        if has_user_received_vacancy(subscriber['user_id'], vacancy_id):
            continue
        prefs = get_subscriber_filter_prefs_effective(subscriber['user_id'])
        ok, filter_reason = vacancy_matches_subscriber(
            vac_match,
            prefs,
            legacy_metro_zones=subscriber.get('metro_zones'),
        )
        if not ok:
            skipped_filter += 1
            continue
        can_push, push_reason, queue_digest = evaluate_push_delivery(
            prefs or {}, category_code,
        )
        if not can_push:
            if push_reason == "quiet":
                skipped_quiet += 1
            elif push_reason == "busy":
                skipped_busy += 1
            elif push_reason == "feed_only":
                skipped_feed_only += 1
            if queue_digest and prefs:
                add_push_digest_pending(subscriber['user_id'], vacancy_id)
            continue
        if not try_reserve_vacancy_sent_to_user(vacancy_id, subscriber['user_id']):
            continue
        try:
            await send_vacancy_card(subscriber['user_id'], text, reply_markup=keyboard)
            sent_count += 1
            await asyncio.sleep(SEND_DELAY)  # небольшая пауза, чтобы не флудить
        except Exception as e:
            unreserve_vacancy_sent_to_user(vacancy_id, subscriber['user_id'])
            if "bot was blocked by the user" in str(e):
                logger.info(f"Пользователь {subscriber['user_id']} заблокировал бота")
                _mark_subscriber_blocked_if_needed(subscriber['user_id'])
            else:
                logger.error(f"Ошибка отправки {subscriber['user_id']}: {e}")

    logger.info(
        f"Вакансия {vacancy_id} (категория {category_code}): push {sent_count}, "
        f"free skip {skipped_free}, filter skip {skipped_filter}, "
        f"quiet {skipped_quiet}, busy {skipped_busy}, feed_only {skipped_feed_only}"
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
    from services.vacancy_closed_notify import deliver_closed_vacancy_notices

    await deliver_closed_vacancy_notices(bot, closed_data)

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
            [KeyboardButton(text=BTN_PREMIUM_FILTERS)],
            [KeyboardButton(text=BTN_SETTINGS_BACK)],
        ],
        resize_keyboard=True,
    )


USER_MENU_BUTTONS = {
    "🔍 Посмотреть новые вакансии",
    "📨 Мои отклики", BTN_SETTINGS, BTN_SETTINGS_LEGACY,
    BTN_SETTINGS_CATEGORIES, BTN_PREMIUM_FILTERS, BTN_METRO, BTN_SETTINGS_BACK,
    "📍 Мои районы",
    "💎 Подписка", BTN_MY_DATA, BTN_MY_DATA_LEGACY,
    "📖 Как пользоваться", "❓ Поддержка",
    "📋 Мои категории", "✏️ Изменить категории",
}

from services.ux_middleware import ChatActivityMiddleware
dp.update.outer_middleware(ChatActivityMiddleware())

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
            [KeyboardButton(text="🔍 Ручная проверка"), KeyboardButton(text="🔬 Аудит фильтра")],
            [KeyboardButton(text="📝 Отчёт парсера"), KeyboardButton(text="📋 Примеры отсева")],
            [KeyboardButton(text="📡 Покрытие каналов"), KeyboardButton(text="📊 Шум по чатам")],
            [KeyboardButton(text="📋 Список чатов парсинга")],
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
            [KeyboardButton(text="⏳ Premium истекает"), KeyboardButton(text="💎 Запросы Premium")],
            [KeyboardButton(text="📋 Список откликов")],
            [KeyboardButton(text="⚠️ Жалобы"), KeyboardButton(text="❓ Поддержка (админ)")],
            [KeyboardButton(text="📣 Техсообщение")],
            [KeyboardButton(text=ADMIN_BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def get_admin_export_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📥 Excel: подписчики"), KeyboardButton(text="📥 Excel: вакансии")],
            [KeyboardButton(text="📥 Excel: заказчики"), KeyboardButton(text="📥 Excel: отклики")],
            [KeyboardButton(text="📥 Excel: не подходит")],
            [KeyboardButton(text=ADMIN_BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def get_admin_mod_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Модерация вакансий"), KeyboardButton(text="📺 Канал")],
            [KeyboardButton(text="🗑 Удалить вакансию")],
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
    "🧭 Маппинг категорий", "⚠️ Жалобы", "❓ Поддержка (админ)", "📣 Техсообщение", "➕ Добавить чат",
    "📋 Список чатов парсинга", "💬 Чаты парсинга", "📤 Отправить вакансию",
    "📥 Excel: подписчики", "📥 Excel: вакансии", "📥 Excel: заказчики",
    "📥 Excel: отклики", "📥 Excel: не подходит", "📊 Шум по чатам", "📝 Модерация вакансий",
    "🗑 Удалить вакансию",
    "🔬 Аудит фильтра", "📋 Примеры отсева", "📡 Покрытие каналов",
    "📺 Канал", "📺 Статус канала", "📊 Статистика канала",
    "📣 Вакансия в канал", "📣 В канал", "📝 Новость в канал", "📢 Промо в канал",
    "✏️ Тексты промо",
    "📣 В канал", "📣 Вакансия в канал", "📝 Новость в канал", "📢 Промо в канал",
    "📖 Как пользоваться", "📖 Справка", "❌ Закрыть меню",
}

# ========== КОМАНДЫ И ОБРАБОТЧИКИ ==========

async def send_admin_help(message: types.Message):
    text = build_admin_help_html()
    try:
        if len(text) > 3800:
            await send_long_message(message.chat.id, text, parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    except TelegramBadRequest:
        plain = re.sub(r"<[^>]*>", "", text)
        if len(plain) > 3800:
            await send_long_message(message.chat.id, plain, parse_mode=None)
        else:
            await message.answer(plain, disable_web_page_preview=True)


@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    user_id = message.from_user.id
    if user_id == YOUR_USER_ID:
        await send_admin_help(message)
        return
    await send_user_help(message, user_id)


@dp.message(lambda m: m.text in ("📖 Как пользоваться", "📖 Справка"))
async def help_menu_button(message: types.Message):
    if message.from_user.id == YOUR_USER_ID:
        await send_admin_help(message)
        return
    await send_user_help(message, message.from_user.id)


@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
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

    deeplink_category = None
    if start_payload.startswith("vac_"):
        vacancy_id = start_payload[4:].strip()
        if vacancy_id:
            row = get_vacancy_push_row(vacancy_id)
            if row:
                deeplink_category = row[5] or "promoter"
            await show_vacancy_by_deeplink(message, user_id, vacancy_id)
    if deeplink_category:
        await state.update_data(deeplink_category=deeplink_category)

    expired_msg = downgrade_expired_premium(user_id)
    if expired_msg:
        await message.answer(expired_msg, parse_mode="Markdown")
    elif enforce_free_category_limit(user_id, FREE_CATEGORY_LIMIT):
        await message.answer(
            f"ℹ️ На Free доступна *{_free_category_hint_short()}*. "
            "Лишние категории сняты — проверьте «⚙️ Настройки».",
            parse_mode="Markdown",
        )

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
        fsm_data = await state.get_data()
        await send_category_picker(
            message.chat.id,
            user_id,
            deeplink_category=fsm_data.get("deeplink_category"),
        )
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
        "👋 *Добро пожаловать в PromoStaff Hunter!*\n\n"
        "Бот присылает вакансии из Telegram-групп: промо, хелперы, грузчики и др.\n\n"
        "Выберите, как будете пользоваться:",
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
        "Помогу находить вакансии и откликаться в один клик.\n\n"
        "*Зачем анкета:* ФИО, возраст и телефон уходят работодателю при отклике — "
        "без этого «✋ Откликнуться» не сработает.\n\n"
        f"*Тариф:* бесплатно — *1 категория* (лента без push). "
        f"*2 категории и больше* + push — Premium ({escape_markdown(SUBSCRIPTION_PRICE_RUB)} ₽/мес).\n"
        f"🎁 Один раз — пробный Premium *{TRIAL_DAYS} дн.* после выбора категорий "
        "(все роли + push + метро).\n\n"
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
    await send_category_picker(
        message.chat.id,
        user_id,
        deeplink_category=data.get("deeplink_category"),
    )
    await state.clear()


# ========== ОБРАБОТКА КАТЕГОРИЙ (с отметками) ==========

@dp.callback_query(lambda c: c.data and c.data.startswith("cat_"))
async def select_category(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    category_code = callback.data.replace("cat_", "")
    current_codes, blocked = await run_db(
        toggle_user_category,
        user_id,
        category_code,
        free_limit=FREE_CATEGORY_LIMIT,
    )
    cat_name = get_category_name(category_code)
    if blocked:
        await safe_callback_answer(
            callback,
            f"На Free — {_free_category_hint_short()}. Нужно больше — Premium.",
            show_alert=True,
        )
    elif category_code in current_codes:
        await safe_callback_answer(callback, f"✅ {cat_name}")
    else:
        await safe_callback_answer(callback, f"− {cat_name}")
    hint = ""
    if blocked:
        hint = (
            f"⚠️ На Free — {_free_category_hint_short()}.\n"
            "Нужно больше? Нажмите *💎 Premium* ниже."
        )
    await edit_category_picker(callback.message, current_codes, user_id, hint=hint)


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
                f"На Free — {_free_category_hint_short()}. Оформите Premium.",
                show_alert=True,
            )
            return
        await safe_callback_answer(callback, "⏳ Оформляем…")
        categories_text = "\n".join([f"• {c['emoji']} {c['name']}" for c in categories])
        keyboard, _ = get_main_keyboard(user_id)
        from services.chat_feedback import typing_keepalive
        async with typing_keepalive(bot, callback.message.chat.id):
            trial_granted = await run_db(grant_trial_if_eligible, user_id, TRIAL_DAYS)
            await setup_forum_topics_for_user(user_id)
        trial_line = ""
        if trial_granted:
            trial_line = (
                f"\n\n🎁 *Пробный Premium на {TRIAL_DAYS} дн.* — все категории, push и фильтр по метро!\n"
                f"_После trial без оплаты останется {_free_category_hint_short()} — оформите Premium, "
                "чтобы сохранить несколько категорий._"
            )
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
    from services.response_cards import format_response_list_row, response_short_title
    from services.chat_feedback import send_typing

    await send_typing(bot, message.chat.id)
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

    lines = [f"📨 *Мои отклики* — {page + 1}/{pages_total} (всего {total})", "", "Выберите карточку:"]
    rows = []
    for i, resp in enumerate(responses, start=start + 1):
        lines.append(format_response_list_row(resp, i))
        rows.append([
            InlineKeyboardButton(
                text=f"📋 {i}. {response_short_title(resp, max_len=24)}",
                callback_data=f"resp_card_{resp['id']}",
            )
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"resp_list_{page - 1}"))
    if start + len(responses) < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"resp_list_{page + 1}"))
    if nav:
        rows.append(nav)
    await message.answer(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def show_response_card(message: types.Message, response_id: int, *, user_id: int, for_admin: bool = False, edit: bool = False):
    from services.response_cards import format_admin_response_card, format_user_response_card

    resp = get_response_by_id(response_id)
    if not resp:
        text = "❌ Отклик не найден."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return
    if not for_admin and resp.get("user_id") != user_id:
        if edit:
            await message.edit_text("❌ Нет доступа.")
        else:
            await message.answer("❌ Нет доступа.")
        return
    if for_admin:
        text = format_admin_response_card(resp, _admin_response_user_label(resp))
    else:
        text = format_user_response_card(resp)
    markup = build_user_response_card_keyboard(resp, for_admin=for_admin)
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                await message.answer(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


async def send_admin_responses_page(message: types.Message, page: int = 0, *, edit: bool = False):
    from services.response_cards import format_admin_response_list_row

    total = count_admin_responses()
    if total == 0:
        text = "📭 Нет откликов."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return
    start = page * RESPONSES_PAGE_SIZE
    responses = get_admin_responses(limit=RESPONSES_PAGE_SIZE, offset=start)
    pages_total = (total - 1) // RESPONSES_PAGE_SIZE + 1
    lines = [
        f"📋 *Отклики* — {page + 1}/{pages_total} (всего {total})",
        "",
        "Строка: группа · статус черновика · кандидат",
    ]
    rows = []
    for i, resp in enumerate(responses, start=start + 1):
        label = _admin_response_user_label(resp)
        rows.append([
            InlineKeyboardButton(
                text=f"{i}. {label[:14]}{'…' if len(label) > 14 else ''}",
                callback_data=f"adm_resp_{resp['id']}",
            )
        ])
        lines.append(format_admin_response_list_row(resp, i, label))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_resp_list_{page - 1}"))
    if start + len(responses) < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_resp_list_{page + 1}"))
    if nav:
        rows.append(nav)
    body = "\n".join(lines)
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    if edit:
        try:
            await message.edit_text(body, parse_mode="Markdown", reply_markup=markup)
        except TelegramBadRequest:
            await message.answer(body, parse_mode="Markdown", reply_markup=markup)
    else:
        await message.answer(body, parse_mode="Markdown", reply_markup=markup)


@dp.message(lambda m: m.text == "📨 Мои отклики")
async def show_my_responses(message: types.Message):
    await send_responses_page(message, message.from_user.id, page=0)


@dp.callback_query(lambda c: c.data and c.data.startswith("resp_page_"))
async def responses_page_callback(callback: types.CallbackQuery):
    await safe_callback_answer(callback, "Открываю…")
    try:
        page = int(callback.data.replace("resp_page_", ""))
    except ValueError:
        return
    await send_responses_page(callback.message, callback.from_user.id, page=page)


@dp.callback_query(lambda c: c.data and c.data.startswith("resp_list_"))
async def responses_list_callback(callback: types.CallbackQuery):
    await safe_callback_answer(callback, "Открываю…")
    try:
        page = int(callback.data.replace("resp_list_", ""))
    except ValueError:
        return
    await send_responses_page(callback.message, callback.from_user.id, page=page)


@dp.callback_query(lambda c: c.data and c.data.startswith("resp_card_"))
async def response_card_callback(callback: types.CallbackQuery):
    await safe_callback_answer(callback, "Загружаю карточку…")
    try:
        response_id = int(callback.data.replace("resp_card_", ""))
    except ValueError:
        return
    await show_response_card(
        callback.message, response_id,
        user_id=callback.from_user.id,
        for_admin=callback.from_user.id == YOUR_USER_ID,
        edit=True,
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_resp_list_"))
async def admin_responses_list_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    await safe_callback_answer(callback)
    page = int(callback.data.replace("adm_resp_list_", ""))
    await send_admin_responses_page(callback.message, page=page, edit=True)


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_resp_resend_"))
async def admin_response_resend_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    resp = get_response_by_id(int(callback.data.replace("adm_resp_resend_", "")))
    if not resp:
        await callback.answer("Не найден", show_alert=True)
        return
    profile = get_subscriber_profile(resp["user_id"])
    if not profile:
        await callback.answer("Нет профиля", show_alert=True)
        return
    contact = resp.get("employer_contact") or resp.get("author_contact")
    draft_text = build_candidate_profile_text(profile)
    try:
        await deliver_response_draft(
            resp["user_id"],
            employer_contact=contact or "—",
            source_chat=resp.get("source_chat_title"),
            required_fields=extract_required_fields_from_vacancy(resp.get("vacancy_text") or ""),
            draft_text=draft_text,
            vacancy_id=resp["vacancy_id"],
        )
        await callback.answer("Черновик отправлен пользователю", show_alert=True)
    except Exception as e:
        logger.exception("adm_resp_resend: %s", e)
        await callback.answer(f"Ошибка: {str(e)[:80]}", show_alert=True)


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_resp_reset_"))
async def admin_response_reset_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    resp = get_response_by_id(int(callback.data.replace("adm_resp_reset_", "")))
    if not resp:
        await callback.answer("Не найден", show_alert=True)
        return
    delete_response(resp["user_id"], resp["vacancy_id"])
    await safe_callback_answer(callback, "Отклик сброшен — пользователь может откликнуться снова")
    await send_admin_responses_page(callback.message, page=0, edit=True)


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_resp_") and not c.data.startswith("adm_resp_list_") and not c.data.startswith("adm_resp_resend_") and not c.data.startswith("adm_resp_reset_"))
async def admin_response_card_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    await safe_callback_answer(callback)
    response_id = int(callback.data.replace("adm_resp_", ""))
    await show_response_card(callback.message, response_id, user_id=callback.from_user.id, for_admin=True, edit=True)


# ========== ПАГИНАЦИЯ ДЛЯ ПРОСМОТРА ВАКАНСИЙ ==========

def _cache_user_feed(user_id: int, data: dict, *, page: int | None = None) -> None:
    if page is not None:
        data = {**data, "page": page}
    user_pages[user_id] = data
    save_user_feed_session(
        user_id,
        data.get("feed_mode") or "fresh",
        data.get("feed_filter"),
        [v["id"] for v in data["vacancies"]],
        data.get("page", 0),
    )


def _get_user_feed(user_id: int) -> dict | None:
    cached = user_pages.get(user_id)
    if cached:
        return cached
    session = load_user_feed_session(user_id)
    if not session:
        return None
    vacancies = get_feed_vacancies_by_ids(session["vacancy_ids"])
    if not vacancies:
        return None
    data = {
        "vacancies": vacancies,
        "total": len(vacancies),
        "page": session.get("page", 0),
        "feed_filter": session.get("feed_filter"),
        "feed_mode": session.get("feed_mode") or "fresh",
    }
    user_pages[user_id] = data
    return data


def _feed_filter_context(user_id: int) -> tuple[bool, dict | None]:
    if not is_user_premium(user_id):
        return False, None
    prefs = get_subscriber_filter_prefs_effective(user_id)
    if not prefs or not prefs.get("apply_to_feed"):
        return False, prefs
    return True, prefs


def _vacancy_passes_feed_filters(vac: dict, cat_code: str, prefs: dict | None) -> bool:
    if not prefs:
        return True
    from services.subscriber_match import vacancy_matches_subscriber

    vac_match = {
        "message_text": vac.get("text") or "",
        "address": vac.get("address"),
        "address_normalized": vac.get("address_normalized"),
        "category_code": cat_code,
        "geo_tags": vac.get("geo_tags"),
        "rate_hourly": vac.get("rate_hourly"),
        "rate_shift": vac.get("rate_shift"),
        "rate_effective_hourly": vac.get("rate_effective_hourly"),
        "shift_date": vac.get("shift_date"),
        "shift_time_start": vac.get("shift_time_start"),
        "location_lat": vac.get("location_lat"),
        "location_lon": vac.get("location_lon"),
    }
    ok, _ = vacancy_matches_subscriber(vac_match, prefs)
    return ok


def _feed_vacancies_for_category(
    user_id: int, cat: dict, apply_filters: bool, prefs: dict | None, feed_mode: str = "fresh",
) -> list:
    vacancies = get_feed_vacancies_for_user(user_id, cat["code"])
    result = []
    for vac in vacancies:
        if not _vacancy_in_feed_mode(vac, feed_mode):
            continue
        if not vacancy_matches_category(vac.get("text") or "", cat["code"]):
            continue
        if apply_filters and not _vacancy_passes_feed_filters(vac, cat["code"], prefs):
            continue
        vac["category"] = cat
        result.append(vac)
    return result


def _collect_feed_vacancies(
    user_id: int, category_codes: list[str] | None = None, feed_mode: str = "fresh",
) -> list:
    apply_filters, prefs = _feed_filter_context(user_id)
    user_categories = get_user_categories(user_id)
    if category_codes is not None:
        codes = set(category_codes)
        user_categories = [c for c in user_categories if c["code"] in codes]
    all_vacancies = []
    for cat in user_categories:
        all_vacancies.extend(
            _feed_vacancies_for_category(user_id, cat, apply_filters, prefs, feed_mode)
        )
    all_vacancies.sort(
        key=lambda v: v.get("published_at") or v.get("found_at") or "",
        reverse=True,
    )
    return all_vacancies


def _feed_count_for_category(
    user_id: int, cat: dict, apply_filters: bool, prefs: dict | None, feed_mode: str = "fresh",
) -> int:
    return len(_feed_vacancies_for_category(user_id, cat, apply_filters, prefs, feed_mode))


def _feed_mode_totals(user_id: int) -> tuple[int, int]:
    apply_filters, prefs = _feed_filter_context(user_id)
    fresh_total = archive_total = 0
    for cat in get_user_categories(user_id):
        fresh_total += _feed_count_for_category(user_id, cat, apply_filters, prefs, "fresh")
        archive_total += _feed_count_for_category(user_id, cat, apply_filters, prefs, "archive")
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
    apply_filters, prefs = _feed_filter_context(user_id)
    buttons, row = [], []
    total = 0
    mode_label = "свежие" if feed_mode == "fresh" else "ранее"
    for i, cat in enumerate(user_categories):
        count = _feed_count_for_category(user_id, cat, apply_filters, prefs, feed_mode)
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
    apply_filters, _ = _feed_filter_context(user_id)
    hint = "\n\n🎯 Учитываются фильтры Premium в ленте." if apply_filters else ""
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
    apply_filters, _ = _feed_filter_context(user_id)
    hint = "\n\n🎯 Учитываются фильтры Premium в ленте." if apply_filters else ""
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
    from services.chat_feedback import typing_keepalive

    async with typing_keepalive(bot, message.chat.id):
        all_vacancies = _collect_feed_vacancies(user_id, category_codes, feed_mode)
    if not all_vacancies:
        apply_filters, _ = _feed_filter_context(user_id)
        hint = (
            "\n\nПопробуйте ослабить фильтры в ⚙️ Настройки → 🎯 Фильтры Premium."
            if apply_filters else ""
        )
        await message.answer(f"🔍 В этой категории вакансий нет.{hint}", parse_mode="Markdown")
        return
    user_pages[user_id] = {
        "vacancies": all_vacancies,
        "page": 0,
        "total": len(all_vacancies),
        "feed_filter": category_codes,
        "feed_mode": feed_mode,
    }
    _cache_user_feed(user_id, user_pages[user_id])
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
    await safe_callback_answer(callback, "Загружаю ленту…")
    suffix = callback.data.replace("feed_fresh_", "", 1)
    if suffix == "all":
        await open_feed_vacancies(callback.message, callback.from_user.id, "fresh", None)
    else:
        await open_feed_vacancies(callback.message, callback.from_user.id, "fresh", [suffix])


@dp.callback_query(lambda c: c.data and c.data.startswith("feed_archive_"))
async def feed_archive_routes(callback: types.CallbackQuery):
    await safe_callback_answer(callback, "Загружаю архив…")
    suffix = callback.data.replace("feed_archive_", "", 1)
    if suffix == "all":
        await open_feed_vacancies(callback.message, callback.from_user.id, "archive", None)
    else:
        await open_feed_vacancies(callback.message, callback.from_user.id, "archive", [suffix])


@dp.callback_query(lambda c: c.data == "feed_menu")
async def feed_menu_callback(callback: types.CallbackQuery):
    await safe_callback_answer(callback)
    data = _get_user_feed(callback.from_user.id) or {}
    mode = data.get("feed_mode") or "fresh"
    await show_feed_category_menu(callback.message, callback.from_user.id, mode)


async def send_vacancy_page(message: types.Message, user_id: int, page: int):
    from services.chat_feedback import typing_keepalive

    data = _get_user_feed(user_id)
    if not data:
        await _offer_feed_session_restart(message)
        return
    _cache_user_feed(user_id, data, page=page)
    vacancies = data["vacancies"]
    total = data["total"]
    start = page * 10
    end = min(start + 10, total)
    if start >= total:
        await message.answer("📭 Это последняя страница.")
        return

    await message.answer(f"📬 *Вакансии (страница {page+1} из {(total-1)//10 + 1})*", parse_mode="Markdown")
    async with typing_keepalive(bot, message.chat.id):
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
            keyboard = build_vacancy_keyboard(vac["id"], **_map_fields_from_vacancy(vac))
            try:
                await send_vacancy_card(message.chat.id, text, reply_markup=keyboard)
                try_reserve_vacancy_sent_to_user(vac["id"], user_id)
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
    if user_id not in user_pages and _get_user_feed(user_id) is None:
        await safe_callback_answer(
            callback,
            "Сессия ленты истекла — нажмите «Открыть ленту»",
            show_alert=True,
        )
        await _offer_feed_session_restart(callback.message)
        return
    page = int(callback.data.split("_")[2])
    await safe_callback_answer(callback, "Загружаю страницу…")
    await send_vacancy_page(callback.message, user_id, page)


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
    pay_hint = format_premium_payment_details_html(user_id) + "\n\n"
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


@dp.callback_query(lambda c: c.data and c.data.startswith("pr_g_"))
async def premium_request_gift_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await safe_callback_answer(callback, "Нет доступа", show_alert=True)
        return
    try:
        parts = callback.data.split("_")
        request_id = int(parts[2])
        days = int(parts[3])
    except (ValueError, IndexError):
        await safe_callback_answer(callback, "Неверный запрос", show_alert=True)
        return
    req = approve_premium_request(request_id)
    if not req:
        await safe_callback_answer(callback, "Запрос уже обработан", show_alert=True)
        return
    target_id = req["user_id"]
    notified = await gift_premium_for_user(target_id, days)
    await safe_callback_answer(
        callback,
        f"🎁 Подарено +{days} дн." + ("" if notified else " (уведомление не доставлено)"),
        show_alert=not notified,
    )
    try:
        suffix = f"\n\n🎁 Подарено +{days} дн. Premium"
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
    req = approve_premium_request(request_id)
    if not req:
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


async def user_fsm_menu_escape(message: types.Message, state: FSMContext) -> bool:
    """Отмена активного FSM и переход по кнопке reply-меню."""
    text = (message.text or "").strip()
    if text not in USER_MENU_BUTTONS:
        return False
    if message.from_user.id == YOUR_USER_ID:
        return False
    await state.clear()
    user_id = message.from_user.id
    if text == "🔍 Посмотреть новые вакансии":
        await show_feed_mode_menu(message, user_id)
    elif text == "📨 Мои отклики":
        await send_responses_page(message, user_id, 0)
    elif text in {BTN_MY_DATA, BTN_MY_DATA_LEGACY}:
        await send_profile_data_screen(message.chat.id, user_id)
    elif text == BTN_SETTINGS_CATEGORIES:
        await send_category_picker(message.chat.id, user_id)
    elif text in {BTN_SETTINGS, BTN_SETTINGS_LEGACY, BTN_SETTINGS_BACK}:
        await message.answer("⚙️ Настройки", reply_markup=get_settings_keyboard())
    elif text in {BTN_PREMIUM_FILTERS, BTN_METRO, "📍 Мои районы"}:
        from handlers.premium_filters import show_premium_filters_screen
        await show_premium_filters_screen(message, user_id)
    elif text == "💎 Подписка":
        await subscription_menu(message)
    elif text in {"📋 Мои категории", "✏️ Изменить категории"}:
        await open_settings_menu(message, state)
    elif text == "📖 Как пользоваться":
        await send_user_help(message, user_id)
    elif text == "❓ Поддержка":
        keyboard, _ = get_main_keyboard(user_id)
        await message.answer(
            "↩️ Шаг отменён. Напишите «❓ Поддержка» ещё раз, чтобы задать вопрос.",
            reply_markup=keyboard,
        )
    else:
        keyboard, _ = get_main_keyboard(user_id)
        await message.answer("↩️ Шаг отменён.", reply_markup=keyboard)
    return True


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
    from services.chat_feedback import send_typing
    await send_typing(bot, message.chat.id)
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
    req_id = add_support_request(user_id, message.text, username)
    await send_user_message(
        user_id,
        topic_key="support",
        text="✅ Ваш вопрос отправлен администратору. Ответ придёт в эту тему.",
    )
    from services.admin_inbox_alerts import notify_admin_support_request

    await notify_admin_support_request(bot, req_id, user_id, username, message.text or "")
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
    row = get_vacancy_push_row(vacancy_id)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=build_vacancy_keyboard(vacancy_id, **_map_fields_from_push_row(row)),
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
    await safe_callback_answer(callback, "Публикую…")
    if not set_vacancy_moderation_if_pending(vacancy_id, "approved"):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer("ℹ️ Вакансия уже обработана ранее.")
        return
    row = get_vacancy_push_row(vacancy_id)
    if not row:
        await callback.message.answer("Вакансия не найдена")
        return
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
    try:
        await callback.message.edit_text(
            f"✅ Вакансия `{vacancy_id}` опубликована.",
            parse_mode="Markdown",
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass


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
    await safe_callback_answer(callback, "Отклоняю…")
    row = get_vacancy_push_row(vacancy_id)
    if not set_vacancy_moderation_if_pending(vacancy_id, "rejected"):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await callback.message.answer("ℹ️ Вакансия уже обработана ранее.")
        return
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
    try:
        await callback.message.edit_text(
            f"❌ Вакансия `{vacancy_id}` отклонена.",
            parse_mode="Markdown",
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass


def _delete_vacancy_confirm_keyboard(vacancy_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"delvac_yes_{vacancy_id}"),
        InlineKeyboardButton(text="Отмена", callback_data="delvac_no"),
    ]])


async def _prompt_delete_vacancy(chat_id: int, vacancy_id: str, *, reply_markup=None) -> bool:
    """Показывает превью и кнопки подтверждения. False — вакансия не найдена."""
    row = get_vacancy_push_row(vacancy_id)
    if not row:
        await bot.send_message(
            chat_id,
            f"❌ Вакансия `{vacancy_id}` не найдена.",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        return False
    preview = escape_markdown((row[0] or "")[:250])
    cat_name = get_category_name(row[5] or "")
    push_n = len(get_users_who_received_vacancy(vacancy_id))
    in_channel = is_vacancy_channel_posted(vacancy_id)
    channel_note = "да" if in_channel else "нет"
    text = (
        f"🗑 *Удалить вакансию из бота?*\n\n"
        f"ID: `{vacancy_id}`\n"
        f"Категория: {escape_markdown(cat_name)}\n"
        f"Push подписчикам: {push_n}\n"
        f"В канале: {channel_note}\n\n"
        f"{preview}\n\n"
        "Будет удалена из базы, снята с учёта у подписчиков и лент. "
        "Пост в канале тоже попробуем удалить."
    )
    await bot.send_message(
        chat_id,
        text,
        parse_mode="Markdown",
        reply_markup=_delete_vacancy_confirm_keyboard(vacancy_id),
    )
    return True


async def _execute_admin_delete_vacancy(vacancy_id: str) -> str:
    channel_msg_id = get_vacancy_channel_message_id(vacancy_id)
    stats = await run_db(delete_vacancy_completely, vacancy_id)
    if not stats:
        return f"❌ Вакансия <code>{escape_html(vacancy_id)}</code> не найдена или уже удалена."

    channel_deleted = False
    if channel_msg_id and HUNTER_CHANNEL_ID:
        try:
            await bot.delete_message(HUNTER_CHANNEL_ID, channel_msg_id)
            channel_deleted = True
        except Exception as e:
            logger.warning("delete channel post vacancy=%s msg=%s: %s", vacancy_id, channel_msg_id, e)

    vid = escape_html(vacancy_id)
    lines = [
        f"🗑 Вакансия <code>{vid}</code> удалена из базы.",
        f"• Снято с учёта push: {stats['push_recipients']} подписчиков",
        f"• записей push: {stats.get('deleted_sent_vacancies', 0)}",
        f"• откликов: {stats.get('deleted_responses', 0)}",
        f"• лент: {stats.get('feed_sessions_updated', 0)}",
    ]
    if channel_msg_id:
        if channel_deleted:
            lines.append(f"• Пост в канале (msg {channel_msg_id}) удалён.")
        else:
            lines.append(f"• Пост в канале (msg {channel_msg_id}) — удалите вручную, если нужно.")
    return "\n".join(lines)


async def _send_admin_delete_vacancy_result(callback: types.CallbackQuery, result: str) -> None:
    try:
        await callback.message.edit_text(result, parse_mode="HTML", reply_markup=None)
    except TelegramBadRequest:
        try:
            await callback.message.answer(
                result, parse_mode="HTML", reply_markup=get_admin_mod_keyboard(),
            )
        except TelegramBadRequest:
            plain = re.sub(r"<[^>]*>", "", result)
            await callback.message.answer(plain, reply_markup=get_admin_mod_keyboard())


@dp.message(Command("enrich_backfill"))
async def enrich_backfill_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    from db import backfill_vacancy_enrichment

    await message.answer("⏳ Пересчитываю enrichment за 3 дня…")
    updated = await run_db(backfill_vacancy_enrichment, 3)
    await message.answer(f"✅ Enrichment backfill: обновлено {updated} вакансий.")


@dp.message(Command("delvac"))
async def delvac_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "🗑 *Удаление вакансии*\n\n"
            "Использование: `/delvac ID`\n"
            "ID — из карточки вакансии или модерации.\n\n"
            "Или кнопка «🗑 Удалить вакансию» в разделе модерации.",
            parse_mode="Markdown",
            reply_markup=get_admin_mod_keyboard(),
        )
        return
    vacancy_id = parts[1].strip()
    await _prompt_delete_vacancy(message.chat.id, vacancy_id, reply_markup=get_admin_mod_keyboard())


@dp.message(lambda m: m.text == "🗑 Удалить вакансию")
async def admin_delete_vacancy_button(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    await message.answer(
        "🗑 Отправьте *ID вакансии* для удаления из базы и у подписчиков.\n"
        "ID можно скопировать из push-сообщения или модерации.",
        parse_mode="Markdown",
        reply_markup=get_admin_mod_keyboard(),
    )
    await state.set_state(DeleteVacancyState.waiting_for_id)


@dp.message(DeleteVacancyState.waiting_for_id)
async def admin_delete_vacancy_id(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    if message.text in ADMIN_MENU_BUTTONS:
        await state.clear()
        return
    vacancy_id = (message.text or "").strip()
    if not vacancy_id:
        await message.answer("❌ Укажите ID вакансии или нажмите «◀️ Назад».")
        return
    await state.clear()
    await _prompt_delete_vacancy(message.chat.id, vacancy_id, reply_markup=get_admin_mod_keyboard())


@dp.callback_query(lambda c: c.data and c.data.startswith("mod_del_"))
async def moderation_delete_prompt(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    vacancy_id = callback.data.replace("mod_del_", "", 1)
    await callback.answer()
    await _prompt_delete_vacancy(callback.message.chat.id, vacancy_id)


@dp.callback_query(lambda c: c.data and c.data.startswith("delvac_yes_"))
async def delete_vacancy_confirm(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    vacancy_id = callback.data.replace("delvac_yes_", "", 1)
    await safe_callback_answer(callback, "Удаляю…")
    result = await _execute_admin_delete_vacancy(vacancy_id)
    await _send_admin_delete_vacancy_result(callback, result)


@dp.callback_query(lambda c: c.data == "delvac_no")
async def delete_vacancy_cancel(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        await callback.message.edit_text("❌ Удаление отменено.", reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer()


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


async def _submit_complaint_and_notify(
    user_id: int,
    vacancy_id: str,
    reason: str,
    complaint_text: str | None = None,
) -> None:
    from services.admin_inbox_alerts import notify_admin_complaint

    cid = add_complaint(user_id, vacancy_id, reason, complaint_text)
    await notify_admin_complaint(
        bot,
        cid,
        user_id,
        vacancy_id or "",
        reason,
        complaint_text,
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
    await safe_callback_answer(callback, "Выберите причину")

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
        await safe_callback_answer(callback, "Жду текст…")
    else:
        data = await state.get_data()
        vacancy_id = data.get("vacancy_id")
        user_id = callback.from_user.id
        await _submit_complaint_and_notify(user_id, vacancy_id, reason)
        await callback.message.answer("✅ Жалоба отправлена администратору. Спасибо, что помогаете улучшить сервис!")
        await state.clear()
        await safe_callback_answer(callback, "Жалоба принята")

@dp.message(ComplaintState.waiting_for_text)
async def complaint_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    vacancy_id = data.get("vacancy_id")
    reason = data.get("reason")
    user_id = message.from_user.id
    await _submit_complaint_and_notify(user_id, vacancy_id, reason, message.text)
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
    vacancy_text, vacancy_link, source_chat, saved_contact, address = unpack_vacancy_row_basic(vacancy_row)
    vac_snippet = vacancy_text[:200] if vacancy_text else None
    employer_contact = saved_contact or extract_contact_from_text(vacancy_text or "")
    if not employer_contact:
        if not add_response(
            user_id,
            vacancy_id,
            vac_snippet,
            vacancy_link,
            profile.get("photo_file_id"),
            employer_contact=None,
            source_chat_title=source_chat,
            draft_status="admin_forward",
        ):
            await callback.answer("❌ Вы уже откликались на эту вакансию!", show_alert=True)
            return
        await _mark_vacancy_card_responded(callback, vacancy_id, address)
        await send_to_admin(
            callback, profile, vacancy_row, build_candidate_profile_text(profile), profile.get("photo_file_id"),
        )
        return
    if not add_response(
        user_id,
        vacancy_id,
        vac_snippet,
        vacancy_link,
        profile.get("photo_file_id"),
        employer_contact=employer_contact,
        source_chat_title=source_chat,
        draft_status="pending",
    ):
        await callback.answer("❌ Вы уже откликались на эту вакансию!", show_alert=True)
        return
    await _mark_vacancy_card_responded(callback, vacancy_id, address)
    required_fields = extract_required_fields_from_vacancy(vacancy_text or "")
    draft_text = build_candidate_profile_text(profile)
    await state.update_data(vacancy_id=vacancy_id, contact=employer_contact, draft_text=draft_text)
    await callback.answer("Готовлю черновик…")
    from services.forum_topics import TOPIC_RESPONSES
    from services.chat_feedback import typing_keepalive
    draft_status = "failed"
    try:
        async with typing_keepalive(bot, callback.message.chat.id):
            draft_status = await deliver_response_draft(
                user_id,
                employer_contact=employer_contact,
                source_chat=source_chat,
                required_fields=required_fields,
                draft_text=draft_text,
                vacancy_id=vacancy_id,
            )
            update_response_delivery(
                user_id,
                vacancy_id,
                draft_status=draft_status,
                vacancy_link=vacancy_link,
                user_photo_file_id=profile.get("photo_file_id"),
                employer_contact=employer_contact,
                source_chat_title=source_chat,
            )
    except Exception as e:
        logger.exception("handle_response user=%s vac=%s: %s", user_id, vacancy_id, e)
        try:
            plain = build_response_draft_message(
                employer_contact=employer_contact,
                required_fields=required_fields,
                draft_text=draft_text,
                contact_link=None,
            )
            await send_user_message(
                user_id,
                topic_key=TOPIC_RESPONSES,
                text=plain + "\n\n_Черновик сохранён текстом — кнопка чата недоступна._",
                parse_mode="Markdown",
            )
            update_response_delivery(
                user_id,
                vacancy_id,
                draft_status="manual",
                vacancy_link=vacancy_link,
                user_photo_file_id=profile.get("photo_file_id"),
                employer_contact=employer_contact,
                source_chat_title=source_chat,
            )
            await send_user_message(
                user_id,
                topic_key=TOPIC_RESPONSES,
                text="📨 Отклик сохранён — «📨 Мои отклики». Отправьте сообщение заказчику вручную.",
            )
        except Exception as e2:
            logger.exception("handle_response fallback failed: %s", e2)
            await notify_admin_response_issue(
                user_id,
                vacancy_id,
                source_chat=source_chat,
                employer_contact=employer_contact,
                reason=f"Не удалось доставить черновик: {e2}",
            )
            try:
                await send_user_message(
                    user_id,
                    topic_key=TOPIC_RESPONSES,
                    text=(
                        "⚠️ Не удалось отправить черновик автоматически.\n"
                        "Напишите в «❓ Поддержка» — администратор поможет.\n\n"
                        f"Скопируйте текст:\n\n```\n{draft_text}\n```"
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            return
        await notify_admin_response_issue(
            user_id,
            vacancy_id,
            source_chat=source_chat,
            employer_contact=employer_contact,
            reason=f"Первичная ошибка ({e}), черновик доставлен текстом.",
        )

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
    from services.chat_feedback import typing_keepalive
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
    async with typing_keepalive(bot, callback.message.chat.id):
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
    if not add_response(
        user_id, vacancy_id, vacancy_text[:200] if vacancy_text else None, None, profile.get("photo_file_id"),
        draft_status="admin_vacancy",
    ):
        await callback.answer("❌ Вы уже откликались на эту вакансию!", show_alert=True)
        return
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

@dp.callback_query(lambda c: c.data and c.data.startswith("respond_add_"))
async def respond_add_comment(callback: types.CallbackQuery, state: FSMContext):
    vacancy_id = callback.data.replace("respond_add_", "")
    await state.update_data(vacancy_id=vacancy_id)
    await callback.message.answer("✏️ Напишите, что добавить в отклик одним сообщением:")
    await state.set_state(ResponseDraftState.waiting_for_comment)
    await safe_callback_answer(callback, "Жду ваш текст…")

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
    vacancy_text = row[0]
    saved_contact = row[3]
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
    vacancy_text, vacancy_link, source_chat, saved_contact, address = unpack_vacancy_row_basic(vacancy_row)
    if not add_response(
        user_id,
        vacancy_id,
        vacancy_text[:200] if vacancy_text else None,
        vacancy_link,
        photo_file_id,
        draft_status="photo_application",
    ):
        if hasattr(target, "answer"):
            await target.answer("❌ Вы уже откликались на эту вакансию!", show_alert=True)
        return
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
    vacancy_text, vacancy_link, source_chat, _, address = unpack_vacancy_row_basic(vacancy_row)
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

async def _parser_wait_progress_loop(status_msg: types.Message):
    """Пока lock занят — обновляем статус по LAST_DEBUG_STATS."""
    while parser_scan_in_progress():
        try:
            await status_msg.edit_text(
                format_parser_wait_message(LAST_DEBUG_STATS),
                parse_mode="Markdown",
            )
        except TelegramBadRequest:
            pass
        except Exception as e:
            logger.debug("parser wait progress edit failed: %s", e)
        await asyncio.sleep(15)


async def _wait_parser_lock_release():
    while parser_scan_in_progress():
        await asyncio.sleep(1)


@dp.message(Command("check_now"))
async def check_now_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    status_msg = await message.answer("🔍 Начинаю проверку...")
    progress_task = None
    if parser_scan_in_progress():
        await status_msg.edit_text(
            format_parser_wait_message(LAST_DEBUG_STATS),
            parse_mode="Markdown",
        )
        progress_task = asyncio.create_task(_parser_wait_progress_loop(status_msg))
        await _wait_parser_lock_release()
        if progress_task:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
    try:
        await status_msg.edit_text("🔍 Ручная проверка чатов…")
        orders, closed_data, stats = await run_parser()
        summary = format_scan_finished_summary(stats)
        if stats.get("error") == "timeout":
            await status_msg.edit_text(
                f"❌ Ручная проверка прервана по таймауту ({PARSER_SCAN_TIMEOUT_SEC // 60} мин).\n\n{summary}",
                parse_mode="Markdown",
            )
            return
        if not orders and not closed_data:
            await status_msg.edit_text(summary, parse_mode="Markdown")
            return
        if closed_data:
            await notify_closed_vacancies(closed_data)
        for order in orders:
            await send_vacancy_to_subscribers(order)
        await status_msg.edit_text(
            f"✅ Проверка завершена. Найдено вакансий: {len(orders)}. "
            f"Уведомлений о закрытии: {len(closed_data)}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Ошибка в check_now_cmd: {e}")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
    finally:
        if progress_task and not progress_task.done():
            progress_task.cancel()


@dp.message(Command("audit_filter"))
async def audit_filter_cmd(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    status_msg = await message.answer("🔬 Запускаю аудит фильтра…")
    progress_task = None
    if parser_scan_in_progress():
        await status_msg.edit_text(
            format_parser_wait_message(LAST_DEBUG_STATS),
            parse_mode="Markdown",
        )
        progress_task = asyncio.create_task(_parser_wait_progress_loop(status_msg))
        await _wait_parser_lock_release()
        if progress_task:
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
    try:
        await status_msg.edit_text(
            "🔬 Аудит: последние посты из каждого чата через фильтр (без сохранения)…"
        )
        stats = await run_parser_audit()
        summary = (
            f"🔬 *Аудит завершён*\n\n"
            f"Просмотрено: {stats.get('messages_scanned', 0)} постов\n"
            f"Прошли фильтр: {stats.get('matched', 0)} | отсеяно: {stats.get('non_relevant', 0)}\n\n"
            "Смотрите «📋 Примеры отсева», «📡 Покрытие каналов», «📊 Шум по чатам»."
        )
        await status_msg.edit_text(summary, parse_mode="Markdown")
    except Exception as e:
        logger.error("audit_filter_cmd: %s", e)
        await status_msg.edit_text(f"❌ Ошибка аудита: {str(e)[:100]}")
    finally:
        if progress_task and not progress_task.done():
            progress_task.cancel()

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
    if _broadcast_lock.locked():
        await callback.answer("Рассылка уже выполняется", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        await callback.answer("Текст не найден", show_alert=True)
        return
    await state.clear()
    await callback.answer("Рассылка запущена")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    status_msg = await callback.message.edit_text("📢 Рассылка началась...")
    async with _broadcast_lock:
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
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.clear()
    try:
        await callback.message.edit_text("❌ Рассылка отменена.", reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer()

@dp.message(lambda m: m.text == "📣 Техсообщение")
async def admin_tech_broadcast_button(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    await message.answer(
        "📣 *Техсообщение всем подписчикам*\n\n"
        "Текст уйдёт в топик «❓ Поддержка» у каждого пользователя "
        "(техработы, тестирование, важные объявления).\n\n"
        "Введите текст одним сообщением:",
        parse_mode="Markdown",
    )
    await state.set_state(TechBroadcastState.waiting_for_text)


@dp.message(TechBroadcastState.waiting_for_text)
async def tech_broadcast_text_received(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    if await admin_fsm_menu_escape(message, state):
        return
    preview = message.text[:500]
    await state.update_data(tech_broadcast_text=message.text)
    await message.answer(
        f"📣 *Предпросмотр техсообщения* (топик «Поддержка»):\n\n{escape_markdown(preview)}"
        + ("…" if len(message.text) > 500 else "")
        + "\n\nОтправить всем подписчикам?",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Отправить", callback_data="tech_broadcast_confirm"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="tech_broadcast_cancel"),
                ]
            ]
        ),
    )


@dp.callback_query(lambda c: c.data == "tech_broadcast_confirm")
async def tech_broadcast_confirm(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    if _broadcast_lock.locked():
        await callback.answer("Рассылка уже выполняется", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("tech_broadcast_text")
    if not text:
        await callback.answer("Текст не найден", show_alert=True)
        return
    await state.clear()
    await callback.answer("Рассылка запущена")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    status_msg = await callback.message.edit_text("📣 Техсообщение отправляется…")
    async with _broadcast_lock:
        sent, failed, err = await run_topic_broadcast(
            callback.from_user.id,
            text,
            topic_key="support",
            body_prefix="🔧 *Сообщение от техподдержки:*\n\n",
            status_msg=status_msg,
        )
    if err:
        await status_msg.edit_text(err)
    else:
        await status_msg.edit_text(
            f"✅ Техсообщение отправлено.\nДоставлено: {sent}\nОшибок: {failed}",
            reply_markup=None,
        )


@dp.callback_query(lambda c: c.data == "tech_broadcast_cancel")
async def tech_broadcast_cancel(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.clear()
    try:
        await callback.message.edit_text("❌ Техсообщение отменено.", reply_markup=None)
    except TelegramBadRequest:
        pass
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


@dp.message(lambda m: m.text == "🔬 Аудит фильтра")
async def admin_audit_filter_button(message: types.Message):
    if message.from_user.id == YOUR_USER_ID:
        await audit_filter_cmd(message)

@dp.message(lambda m: m.text == "📋 Список откликов")
async def admin_responses_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await send_admin_responses_page(message, page=0)

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

async def show_admin_premium_expiring(message: types.Message, within_days: int = 7, edit: bool = False):
    await bot.send_chat_action(message.chat.id, "typing")
    candidates = await run_db(list_premium_renewal_reminder_candidates, within_days)
    if not candidates:
        text = f"✅ Нет активных Premium, которые истекают в ближайшие {within_days} дн."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return
    lines = [
        f"⏳ *Premium истекает* (≤ {within_days} дн.) — {len(candidates)} чел.",
        "",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for item in candidates[:15]:
        uid = item["user_id"]
        profile = get_subscriber_profile(uid)
        if not profile:
            continue
        label = _admin_user_short_label(profile)
        until = format_db_date_short(item.get("paid_until"))
        left = item.get("days_left", "?")
        trial = " · trial" if item.get("trial_used") else ""
        lines.append(f"• *{escape_markdown(label)}* — {left} дн.{trial}, до {escape_markdown(until or '—')}")
        buttons.append([
            InlineKeyboardButton(text=f"👤 {label}", callback_data=f"adm_u_{uid}_0"),
            InlineKeyboardButton(text="🎁 +7", callback_data=f"adm_gd_{uid}_7_0"),
            InlineKeyboardButton(text="+14", callback_data=f"adm_gd_{uid}_14_0"),
        ])
    if len(candidates) > 15:
        lines.append(f"\n… ещё {len(candidates) - 15} — смотрите карточки или /user ID")
    markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    body = "\n".join(lines)
    if edit:
        await message.edit_text(body, parse_mode="Markdown", reply_markup=markup)
    else:
        await message.answer(body, parse_mode="Markdown", reply_markup=markup)


@dp.message(lambda m: m.text == "⏳ Premium истекает")
async def admin_premium_expiring_button(message: types.Message):
    if message.from_user.id != YOUR_USER_ID:
        return
    await show_admin_premium_expiring(message)


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
        caption = format_premium_request_admin_caption_html(req)
        markup = premium_request_admin_keyboard(req["id"], req["user_id"])
        file_id = req.get("receipt_file_id")
        kind = req.get("receipt_kind")
        try:
            if file_id and kind == "photo":
                await bot.send_photo(
                    message.chat.id, file_id, caption=caption,
                    parse_mode="HTML", reply_markup=markup,
                )
            elif file_id and kind == "document":
                await bot.send_document(
                    message.chat.id, file_id, caption=caption,
                    parse_mode="HTML", reply_markup=markup,
                )
            else:
                await message.answer(caption, parse_mode="HTML", reply_markup=markup)
        except TelegramBadRequest:
            plain = re.sub(r"<[^>]+>", "", caption)
            try:
                if file_id and kind == "photo":
                    await bot.send_photo(message.chat.id, file_id, caption=plain, reply_markup=markup)
                elif file_id and kind == "document":
                    await bot.send_document(message.chat.id, file_id, caption=plain, reply_markup=markup)
                else:
                    await message.answer(plain, reply_markup=markup)
            except Exception as e:
                logger.warning(f"admin premium request card #{req['id']}: {e}")
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


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_gt_"))
async def admin_gift_menu_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    parts = callback.data.split("_")
    target_id = int(parts[2])
    cards_page = int(parts[3]) if len(parts) > 3 else 0
    profile = get_subscriber_profile(target_id)
    label = _admin_user_short_label(profile or {"user_id": target_id})
    await safe_callback_answer(callback)
    text = (
        f"🎁 *Подарить Premium*\n\n"
        f"Пользователь: *{escape_markdown(label)}*\n"
        f"ID: `{target_id}`\n\n"
        "Выберите срок или «✏️ Другое число дней»."
    )
    try:
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=admin_gift_days_keyboard(target_id, cards_page),
        )
    except TelegramBadRequest:
        await callback.message.answer(
            text,
            parse_mode="Markdown",
            reply_markup=admin_gift_days_keyboard(target_id, cards_page),
        )


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_gd_"))
async def admin_gift_days_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    parts = callback.data.split("_")
    target_id = int(parts[2])
    days = int(parts[3])
    cards_page = int(parts[4]) if len(parts) > 4 else 0
    notified = await gift_premium_for_user(target_id, days)
    await safe_callback_answer(
        callback,
        f"🎁 Подарено +{days} дн." + ("" if notified else " (уведомление не доставлено)"),
        show_alert=not notified,
    )
    await show_admin_user_detail(callback.message, target_id, cards_page=cards_page, edit=True)


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_gx_"))
async def admin_gift_custom_start_callback(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    parts = callback.data.split("_")
    target_id = int(parts[2])
    cards_page = int(parts[3]) if len(parts) > 3 else 0
    await state.set_state(AdminGiftPremiumState.waiting_for_days)
    await state.update_data(gift_user_id=target_id, gift_cards_page=cards_page)
    await safe_callback_answer(callback)
    await callback.message.answer(
        f"✏️ Сколько дней Premium подарить пользователю `{target_id}`?\n"
        "Отправьте число от 1 до 365 или «◀️ Назад» для отмены.",
        parse_mode="Markdown",
    )


@dp.message(AdminGiftPremiumState.waiting_for_days)
async def admin_gift_custom_days_message(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    if await admin_fsm_menu_escape(message, state):
        return
    if message.text == ADMIN_BTN_BACK:
        await state.clear()
        await message.answer("Отменено.", reply_markup=get_admin_users_keyboard())
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Введите число дней (1–365) или «◀️ Назад».")
        return
    days = int(raw)
    if days < 1 or days > 365:
        await message.answer("Допустимо от 1 до 365 дней.")
        return
    data = await state.get_data()
    target_id = int(data.get("gift_user_id", 0))
    cards_page = int(data.get("gift_cards_page", 0))
    await state.clear()
    if not target_id:
        await message.answer("Сессия истекла — откройте карточку пользователя снова.")
        return
    notified = await gift_premium_for_user(target_id, days)
    note = "" if notified else " (уведомление пользователю не доставлено)"
    await message.answer(
        f"🎁 Пользователю `{target_id}` подарено +{days} дн. Premium{note}.",
        parse_mode="Markdown",
        reply_markup=get_admin_users_keyboard(),
    )
    await show_admin_user_detail(message, target_id, cards_page=cards_page, edit=False)


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_p_"))
async def admin_user_premium_callback(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    parts = callback.data.split("_")
    target_id = int(parts[2])
    days = int(parts[3])
    cards_page = int(parts[4]) if len(parts) > 4 else 0
    notified = await gift_premium_for_user(target_id, days)
    await safe_callback_answer(
        callback,
        f"🎁 Подарено +{days} дн." + ("" if notified else " (уведомление не доставлено)"),
        show_alert=not notified,
    )
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
    await message.answer(
        "Новые обращения приходят push-уведомлением с кнопкой «✉️ Ответить».\n"
        "Или команда: `/answer ID_обращения текст_ответа`",
        parse_mode="Markdown",
    )


async def deliver_support_answer(req_id: int, answer_text: str) -> tuple[bool, str | int]:
    """Отправляет ответ пользователю. (ok, user_id или код ошибки)."""
    from db import get_support_request

    req = get_support_request(req_id)
    if not req or req.get("answered"):
        return False, "not_found"
    user_id = req["user_id"]
    mark_support_answered(req_id, answer_text)
    await send_user_message(
        user_id,
        topic_key="support",
        text=f"📞 *Ответ от администратора:*\n\n{answer_text}",
        parse_mode="Markdown",
    )
    return True, user_id


@dp.callback_query(lambda c: c.data and c.data.startswith("sup_r:"))
async def admin_support_reply_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        req_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Неверный ID", show_alert=True)
        return
    from db import get_support_request

    req = get_support_request(req_id)
    if not req or req.get("answered"):
        await callback.answer("Обращение не найдено или уже отвечено", show_alert=True)
        return
    await state.update_data(support_reply_id=req_id)
    await state.set_state(AdminSupportReplyState.waiting_for_text)
    await callback.answer()
    preview = (req.get("message_text") or "")[:200]
    await callback.message.answer(
        f"✉️ *Ответ на обращение #{req_id}*\n"
        f"От: `{req['user_id']}`\n"
        f"Вопрос: _{escape_markdown(preview)}_\n\n"
        "Введите текст ответа одним сообщением:",
        parse_mode="Markdown",
    )


@dp.message(AdminSupportReplyState.waiting_for_text)
async def admin_support_reply_text(message: types.Message, state: FSMContext):
    if message.from_user.id != YOUR_USER_ID:
        return
    if await admin_fsm_menu_escape(message, state):
        return
    data = await state.get_data()
    req_id = data.get("support_reply_id")
    if not req_id:
        await state.clear()
        await message.answer("❌ Обращение не выбрано.")
        return
    ok, result = await deliver_support_answer(int(req_id), message.text or "")
    await state.clear()
    if ok:
        await message.answer(f"✅ Ответ на #{req_id} отправлен пользователю {result}")
    else:
        await message.answer("❌ Обращение не найдено или уже отвечено.")


@dp.callback_query(lambda c: c.data and c.data.startswith("cmp_ok:"))
async def admin_complaint_resolve(callback: types.CallbackQuery):
    if callback.from_user.id != YOUR_USER_ID:
        await callback.answer("Недоступно", show_alert=True)
        return
    try:
        complaint_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Неверный ID", show_alert=True)
        return
    from db import get_complaint, resolve_complaint

    complaint = get_complaint(complaint_id)
    if not complaint or complaint.get("resolved"):
        await callback.answer("Жалоба не найдена или уже обработана", show_alert=True)
        return
    resolve_complaint(complaint_id)
    await callback.answer("Отмечено как обработано")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.message.answer(f"✅ Жалоба #{complaint_id} отмечена обработанной.")


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
    except Exception:
        await message.answer("❌ Неверный ID обращения")
        return
    ok, result = await deliver_support_answer(req_id, answer_text)
    if ok:
        await message.answer(f"✅ Ответ отправлен пользователю {result}")
    else:
        await message.answer("❌ Обращение не найдено или уже отвечено.")

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


@dp.message(lambda m: m.text == "📥 Excel: отклики")
async def admin_export_responses(message: types.Message):
    await _send_admin_xlsx(
        message, "📥 *Отклики* — Excel",
        "responses", build_responses_xlsx, get_responses_export_rows,
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
    await send_admin_parser_intro(message)


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


@dp.message(lambda m: m.text == "📋 Примеры отсева")
async def admin_reject_samples_button(message: types.Message):
    await send_reject_samples_report(message)


@dp.message(lambda m: m.text == "📡 Покрытие каналов")
async def admin_channel_coverage_button(message: types.Message):
    await send_channel_coverage_report(message)


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
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [
                _inline_btn("Опубликовать", callback_data=f"mod_ok_{v['id']}", style="success"),
                _inline_btn("Отклонить", callback_data=f"mod_no_{v['id']}", style="danger"),
                _inline_btn("📣 Канал", callback_data=f"mod_ch_{v['id']}"),
            ],
            [
                _inline_btn("🗑 Удалить", callback_data=f"mod_del_{v['id']}", style="danger"),
            ],
        ])
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

    async def _startup_enrichment_backfill():
        try:
            from db import backfill_vacancy_enrichment
            updated = await run_db(backfill_vacancy_enrichment, 3)
            if updated:
                logger.info("Enrichment backfill on startup: %s vacancies", updated)
        except Exception as e:
            logger.warning("Enrichment backfill on startup failed: %s", e)

    spawn_background_task(_startup_enrichment_backfill())

    from services.channel_images import log_channel_images_status
    from services.channel_promo_texts import log_promo_texts_status

    log_channel_images_status()
    log_promo_texts_status()

    from profile_photos import prepare_user_photos_storage

    prepare_user_photos_storage()

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

    async def notify_photo_issue(user_id: int, text: str):
        try:
            await bot.send_message(user_id, text, parse_mode="Markdown")
        except Exception as e:
            logger.warning("notify_photo_issue user=%s: %s", user_id, e)

    spawn_background_task(photo_health_loop(bot, notify_photo_issue))

    from services.channel_promo import channel_promo_scheduler_loop
    spawn_background_task(channel_promo_scheduler_loop(bot))
    logger.info("📺 Планировщик промо канала: 09:00, 14:00, 20:00 МСК")

    from services.premium_scheduler import premium_scheduler_loop
    from config import PREMIUM_RENEWAL_REMIND_DAYS
    spawn_background_task(premium_scheduler_loop(bot))
    logger.info(
        "💎 Планировщик Premium: истечение + напоминание за %s дн. (каждый час)",
        PREMIUM_RENEWAL_REMIND_DAYS,
    )

    from services.push_digest_scheduler import push_digest_scheduler_loop
    spawn_background_task(push_digest_scheduler_loop(bot))
    logger.info("🔔 Планировщик push-digest: каждую минуту (quiet / «занят»)")

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


from services.fsm_escape import UserMenuFsmEscapeMiddleware

dp.message.middleware(UserMenuFsmEscapeMiddleware(USER_MENU_BUTTONS, user_fsm_menu_escape))


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
