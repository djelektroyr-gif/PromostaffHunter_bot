import re
import logging
import hashlib
from difflib import SequenceMatcher
from telethon import TelegramClient, events
from telethon import errors
import asyncio
from datetime import datetime, timezone, timedelta
from config import API_ID, API_HASH, HELPER_KEYWORDS, EXCLUDE_CATEGORIES, STOP_PHRASES, HIRING_VERBS, ONE_TIME_JOB_KEYWORDS, PAYMENT_INDICATORS
from db import (
    get_target_chats, is_message_processed, mark_vacancy_closed,
    get_last_processed_id, update_last_processed_id,
    save_vacancy, mark_message_processed, has_recent_duplicate_vacancy,
    get_recent_open_vacancies_for_dedupe
)

logger = logging.getLogger(__name__)

_realtime_client = None
_monitored_chat_ids = set()
_parser_lock = asyncio.Lock()
PARSER_POLL_INTERVAL_SEC = 300
PER_CHAT_SCAN_LIMIT = 120
PARSER_LABEL = "Парсер групп (Telethon)"
MOSCOW_TZ = timezone(timedelta(hours=3))

def make_vacancy_id(chat_id: str, message_id: str, dedupe_key: str = None) -> str:
    unique_str = dedupe_key or f"{chat_id}_{message_id}"
    return hashlib.md5(unique_str.encode()).hexdigest()[:16]

async def refresh_monitored_chat_ids(client) -> set:
    """Резолвит ссылки из БД в numeric chat_id — без этого Telethon-парсер не видит группы."""
    global _monitored_chat_ids
    ids = set()
    for link in get_target_chats():
        try:
            entity = await client.get_entity(link)
            ids.add(str(entity.id))
        except Exception as e:
            logger.warning(f"Не удалось резолвить чат {link}: {e}")
    _monitored_chat_ids = ids
    logger.info(f"📡 Мониторинг {len(_monitored_chat_ids)} чатов по chat_id")
    return ids

def is_chat_monitored(chat_id) -> bool:
    return str(chat_id) in _monitored_chat_ids

async def _process_single_message(message, chat, chat_id: str, chat_title: str, stats: dict = None):
    """Обрабатывает одно сообщение: фильтр, категория, дедуп. Возвращает order или None."""
    if not message.text:
        if stats is not None:
            stats["no_text"] += 1
        return None
    message_id = str(message.id)
    if is_message_processed(message_id, chat_id):
        if stats is not None:
            stats["already_sent"] += 1
        return None
    if not is_message_for_today(message.date):
        if stats is not None:
            stats["old_messages"] += 1
        return None

    if message.is_reply:
        original_message = await message.get_reply_message()
        if original_message and original_message.id:
            close_markers = ["закрыт", "закрыта", "❌", "набор завершён", "вакансия закрыта", "не актуально"]
            if any(marker in message.text.lower() for marker in close_markers):
                original_id = str(original_message.id)
                vacancy_id, users = mark_vacancy_closed(original_id, chat_id)
                if stats is not None:
                    stats["closed_vacancies"] += 1
                return {"type": "closed", "vacancy_id": vacancy_id, "users": users}

    is_relevant, reason, keywords = is_helper_message(message.text)
    if stats is not None:
        stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
    if not is_relevant:
        if stats is not None:
            stats["non_relevant"] += 1
        return None

    category = detect_category(message.text)
    if stats is not None:
        stats["categories"][category] = stats["categories"].get(category, 0) + 1
    cleaned_text = clean_message_text(message.text)
    message_link = get_message_link(chat.id, message.id)
    author_contact = extract_contact_from_text(message.text)
    address = extract_address_from_text(message.text)
    dedupe_key = build_vacancy_dedupe_key(message.text, author_contact)

    duplicate_type = detect_duplicate_type(message.text, author_contact, dedupe_key)
    if duplicate_type:
        if stats is not None:
            if duplicate_type == "exact":
                stats["duplicates_exact"] += 1
            else:
                stats["duplicates_fuzzy"] += 1
        mark_message_processed(message_id, chat_id)
        return None

    vacancy_id = make_vacancy_id(chat_id, message_id, dedupe_key)
    save_vacancy(
        vacancy_id=vacancy_id,
        source_chat=chat_id,
        source_chat_title=chat_title,
        category_code=category,
        message_text=cleaned_text[:2000],
        message_link=message_link,
        author_contact=author_contact,
        address=address,
        is_closed=False,
        dedupe_key=dedupe_key,
        published_at=message.date.strftime("%Y-%m-%d %H:%M:%S"),
    )
    mark_message_processed(message_id, chat_id)
    update_last_processed_id(chat_id, message.id)
    if stats is not None:
        stats["matched"] += 1

    return {
        "vacancy_id": vacancy_id,
        "chat_title": chat_title,
        "message_text": cleaned_text,
        "message_link": message_link,
        "category": category,
        "chat_id": chat_id,
        "message_id": message_id,
        "keywords": keywords[:5],
        "reason": reason,
        "author_contact": author_contact,
        "address": address,
        "dedupe_key": dedupe_key,
        "published_at": message.date.strftime("%Y-%m-%d %H:%M:%S"),
    }

async def _scan_all_chats(client, limit_per_chat: int = PER_CHAT_SCAN_LIMIT, stats: dict = None):
    all_results = []
    closed_vacancies_users = []
    target_chats = get_target_chats()
    if not target_chats:
        return [], []

    await refresh_monitored_chat_ids(client)

    for i, chat_link in enumerate(target_chats, 1):
        entity = await safe_get_entity(client, chat_link)
        if not entity:
            if stats is not None:
                stats["chats_failed"] += 1
                stats["errors_by_chat"][chat_link] = stats["errors_by_chat"].get(chat_link, 0) + 1
            continue

        chat_title = getattr(entity, 'title', None) or 'Без названия'
        chat_id = str(entity.id)
        if stats is not None:
            stats["chats_ok"] += 1

        async for message in client.iter_messages(entity, limit=limit_per_chat):
            if stats is not None:
                stats["messages_scanned"] += 1
            try:
                result = await _process_single_message(message, entity, chat_id, chat_title, stats)
                if result and result.get("type") == "closed":
                    closed_vacancies_users.append((result["vacancy_id"], result["users"]))
                elif result:
                    all_results.append(result)
                    await asyncio.sleep(0.05)
            except Exception as e:
                if stats is not None:
                    stats["errors"] += 1
                    stats["errors_by_chat"][chat_title] = stats["errors_by_chat"].get(chat_title, 0) + 1
                logger.warning(f"⚠️ Пропущено сообщение chat={chat_title} id={getattr(message, 'id', '?')}: {e}")

        await asyncio.sleep(0.3)

    return all_results, closed_vacancies_users

async def _periodic_scan_loop(bot_callback, closed_callback=None):
    await asyncio.sleep(60)
    while _realtime_client and _realtime_client.is_connected():
        try:
            async with _parser_lock:
                logger.info("🔄 Плановая проверка новых вакансий...")
                orders, closed_data = await _scan_all_chats(_realtime_client, limit_per_chat=PER_CHAT_SCAN_LIMIT)
            if closed_data and closed_callback:
                await closed_callback(closed_data)
            for order in orders:
                await bot_callback(order)
            if orders or closed_data:
                logger.info(
                    f"🔄 Плановая проверка: отправлено {len(orders)} вакансий, "
                    f"закрыто {len(closed_data or [])}"
                )
        except Exception as e:
            logger.error(f"Ошибка плановой проверки: {e}", exc_info=True)
        await asyncio.sleep(PARSER_POLL_INTERVAL_SEC)

# ===================== ПАРСЕР ГРУПП (TELETHON) =====================

async def start_realtime_listener(bot_callback, closed_callback=None):
    global _realtime_client
    _realtime_client = TelegramClient('user_session', API_ID, API_HASH)
    await _realtime_client.start()
    logger.info(f"✅ {PARSER_LABEL} подключён")

    await refresh_monitored_chat_ids(_realtime_client)

    async with _parser_lock:
        logger.info("🔄 Стартовая синхронизация вакансий...")
        startup_orders, startup_closed = await _scan_all_chats(_realtime_client, limit_per_chat=PER_CHAT_SCAN_LIMIT)
    if startup_closed and closed_callback:
        await closed_callback(startup_closed)
    for order in startup_orders:
        await bot_callback(order)
    logger.info(f"✅ Стартовая синхронизация: {len(startup_orders)} вакансий")

    asyncio.create_task(_periodic_scan_loop(bot_callback, closed_callback))

    @_realtime_client.on(events.NewMessage())
    async def handler(event):
        if not is_chat_monitored(event.chat_id):
            return

        logger.info(f"⚡ {PARSER_LABEL}: новое сообщение из чата {event.chat_id}")
        message = event.message
        chat = await event.get_chat()
        chat_id = str(chat.id)
        chat_title = chat.title or "Без названия"

        try:
            result = await _process_single_message(message, chat, chat_id, chat_title)
            if result and result.get("type") == "closed":
                if closed_callback and result.get("users"):
                    await closed_callback([(result["vacancy_id"], result["users"])])
            elif result:
                await bot_callback(result)
                logger.info(f"⚡ {PARSER_LABEL}: вакансия из {chat_title}")
        except Exception as e:
            logger.warning(f"⚠️ {PARSER_LABEL}: ошибка chat={chat_title}: {e}")

    await _realtime_client.run_until_disconnected()

async def stop_realtime_listener():
    global _realtime_client
    if _realtime_client and _realtime_client.is_connected():
        await _realtime_client.disconnect()
        logger.info(f"🛑 {PARSER_LABEL} остановлен")

def get_parser_status_snapshot() -> dict:
    """Быстрый снимок для админ-статистики без повторного resolve всех чатов."""
    active = len(get_target_chats())
    online = bool(_realtime_client and _realtime_client.is_connected())
    monitored = len(_monitored_chat_ids) if online else 0
    return {
        "online": online,
        "active_chats": active,
        "monitored": monitored,
    }

def format_parser_status_line(snapshot: dict) -> str:
    if snapshot["online"]:
        line = f"✅ {PARSER_LABEL}: подключён"
    else:
        line = f"⏳ {PARSER_LABEL}: не подключён"
    line += f"\n   Чатов в БД: {snapshot['active_chats']}"
    if snapshot["online"]:
        line += f" | в мониторинге: {snapshot['monitored']}"
        if snapshot["monitored"] < snapshot["active_chats"]:
            line += "\n   ⚠️ Не все чаты резолвятся — см. «Список чатов парсинга»"
    return line

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================

def _iso_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _new_stats() -> dict:
    return {
        "started_at": _iso_now(),
        "finished_at": None,
        "chats_total": len(get_target_chats()),
        "chats_ok": 0,
        "chats_failed": 0,
        "messages_scanned": 0,
        "already_sent": 0,
        "no_text": 0,
        "non_relevant": 0,
        "matched": 0,
        "errors": 0,
        "reasons": {},
        "errors_by_chat": {},
        "categories": {},
        "old_messages": 0,
        "closed_vacancies": 0,
        "duplicates_exact": 0,
        "duplicates_fuzzy": 0,
    }

LAST_DEBUG_STATS = _new_stats()

def get_last_debug_report() -> str:
    s = LAST_DEBUG_STATS
    if not s.get("started_at"):
        return "ℹ️ Отладочных данных пока нет. Запустите /check_now или дождитесь авто-проверки."
    lines = [
        "🧪 Последний прогон парсера:",
        f"Старт: {s.get('started_at')}",
        f"Финиш: {s.get('finished_at') or 'в процессе'}",
        f"Чатов: {s.get('chats_ok', 0)}/{s.get('chats_total', 0)} успешно, ошибок: {s.get('chats_failed', 0)}",
        f"Сообщений просмотрено: {s.get('messages_scanned', 0)}",
        f"Совпадений найдено: {s.get('matched', 0)}",
        f"Отсеяно: {s.get('non_relevant', 0)} | без текста: {s.get('no_text', 0)} | уже обработано: {s.get('already_sent', 0)} | старых: {s.get('old_messages', 0)} | закрыто: {s.get('closed_vacancies', 0)}",
        f"Дубли: exact={s.get('duplicates_exact', 0)} | fuzzy={s.get('duplicates_fuzzy', 0)}",
        f"Локальных ошибок: {s.get('errors', 0)}",
    ]
    categories = s.get("categories") or {}
    if categories:
        lines.append("\n📊 *Распределение по категориям:*")
        for cat, count in sorted(categories.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  • {cat}: {count}")
    reasons = s.get("reasons") or {}
    if reasons:
        top = sorted(reasons.items(), key=lambda x: x[1], reverse=True)[:5]
        lines.append("\n📋 Топ причин фильтра:")
        for reason, count in top:
            lines.append(f"  • {reason}: {count}")
    chat_errors = s.get("errors_by_chat") or {}
    if chat_errors:
        lines.append("\n⚠️ Ошибки по чатам:")
        for chat, count in sorted(chat_errors.items(), key=lambda x: x[1], reverse=True)[:5]:
            lines.append(f"  • {chat}: {count}")
    return "\n".join(lines)

def extract_contact_from_text(text: str) -> str:
    if not text:
        return None
    resolve_match = re.search(r'tg://resolve\?domain=([a-zA-Z0-9_]{5,32})', text, re.IGNORECASE)
    if resolve_match:
        return f"@{resolve_match.group(1)}"
    username_match = re.search(r'@([a-zA-Z0-9_]{5,32})', text)
    if username_match:
        return username_match.group(0)
    tg_link_match = re.search(r't\.me/([a-zA-Z0-9_]+)', text)
    if tg_link_match:
        return f"@{tg_link_match.group(1)}"
    phone_match = re.search(r'(\+7|8)[\s\-]?\(?(\d{3})\)?[\s\-]?(\d{3})[\s\-]?(\d{2})[\s\-]?(\d{2})', text)
    if phone_match:
        return phone_match.group(0)
    ls_match = re.search(r'[вВ] [лЛ][сС] @?([a-zA-Z0-9_]+)', text)
    if ls_match:
        return f"@{ls_match.group(1)}"
    return None

def extract_address_from_text(text: str) -> str:
    if not text:
        return None
    direct_match = re.search(r'(?:адрес|локация|место)\s*[:\-]\s*([^\n]{6,120})', text, re.IGNORECASE)
    if direct_match:
        return direct_match.group(1).strip(" .,")
    metro_match = re.search(r'(?:м\.|метро)\s*[:\-]?\s*(?:🚇\s*)?([А-Яа-яёЁ\-\s]{2,50})', text, re.IGNORECASE)
    if metro_match:
        return f"метро {metro_match.group(1).strip(' .,')}"
    street_match = re.search(
        r'((?:ул\.|улица|пр-т|проспект|пер\.|переулок|шоссе|наб\.|набережная)\s+[А-Яа-яёЁ0-9\-\.\s]{3,80}(?:,\s*\d+[А-Яа-яёЁA-Za-z0-9\/-]*)?)',
        text,
        re.IGNORECASE
    )
    if street_match:
        return street_match.group(1).strip(" .,")
    city_match = re.search(
        r'\b(Москва|МО|Подольск|Химки|Мытищи|Красногорск|Люберцы|Балашиха|Корол[её]в|Одинцово|Домодедово|Железнодорожный|Видное|Щ[её]лково|Электросталь|Коломна|Серпухов)\b',
        text,
        re.IGNORECASE
    )
    if city_match:
        return city_match.group(1)
    return None

def _normalize_for_dedupe(text: str) -> str:
    if not text:
        return ""
    normalized = re.sub(r'https?://\S+|t\.me/\S+', ' ', text.lower())
    normalized = re.sub(r'@\w+', ' ', normalized)
    normalized = re.sub(r'[\W_]+', ' ', normalized, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', normalized).strip()

def build_vacancy_dedupe_key(text: str, author_contact: str) -> str:
    normalized_text = _normalize_for_dedupe(text)[:280]
    normalized_contact = (author_contact or "").strip().lower()
    payload = f"{normalized_contact}|{normalized_text}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]

def _extract_phone_digits(text: str) -> str:
    if not text:
        return None
    match = re.search(r'(\+7|8)[\s\-]?\(?(\d{3})\)?[\s\-]?(\d{3})[\s\-]?(\d{2})[\s\-]?(\d{2})', text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits if len(digits) == 11 else None

def detect_duplicate_type(text: str, author_contact: str, dedupe_key: str) -> str:
    if has_recent_duplicate_vacancy(dedupe_key, max_age_days=1):
        return "exact"
    normalized_text = _normalize_for_dedupe(text)
    if not normalized_text:
        return None
    phone_digits = _extract_phone_digits(text)
    normalized_contact = (author_contact or "").strip().lower()
    recent = get_recent_open_vacancies_for_dedupe(max_age_days=1, limit=250)
    for row in recent:
        candidate_text = _normalize_for_dedupe(row.get("message_text", ""))
        if not candidate_text:
            continue
        same_contact = normalized_contact and normalized_contact == (row.get("author_contact") or "").strip().lower()
        same_phone = phone_digits and phone_digits == _extract_phone_digits(row.get("message_text", ""))
        if not (same_contact or same_phone):
            continue
        similarity = SequenceMatcher(None, normalized_text, candidate_text).ratio()
        if similarity >= 0.82:
            return "fuzzy"
    return None

def is_message_for_today(message_dt: datetime) -> bool:
    if not message_dt:
        return False
    if message_dt.tzinfo is None:
        message_dt = message_dt.replace(tzinfo=timezone.utc)
    msg_msk = message_dt.astimezone(MOSCOW_TZ)
    now_msk = datetime.now(MOSCOW_TZ)
    return msg_msk.date() == now_msk.date()

async def inspect_parser_chats() -> tuple:
    """Проверка доступа Telethon к чатам из БД. Возвращает (список, статус парсера)."""
    from db import list_target_chats

    chats_db = list_target_chats()
    if not chats_db:
        return [], "empty"

    if not (_realtime_client and _realtime_client.is_connected()):
        offline = []
        for row in chats_db:
            offline.append({
                **row,
                "status": "parser_offline",
                "title": None,
                "chat_id": None,
                "monitored": False,
            })
        return offline, "offline"

    await refresh_monitored_chat_ids(_realtime_client)
    results = []
    for row in chats_db:
        link = row["chat_link"]
        if not row["is_active"]:
            results.append({**row, "status": "disabled", "title": None, "chat_id": None, "monitored": False})
            continue
        entity = await safe_get_entity(_realtime_client, link)
        if entity:
            chat_id = str(entity.id)
            title = getattr(entity, "title", None) or getattr(entity, "username", None) or link
            results.append({
                **row,
                "status": "ok",
                "title": title,
                "chat_id": chat_id,
                "monitored": chat_id in _monitored_chat_ids,
            })
        else:
            results.append({**row, "status": "no_access", "title": None, "chat_id": None, "monitored": False})
    return results, "online"

def format_parser_chats_report(chats: list, parser_status: str) -> str:
    status_labels = {
        "online": "✅ подключён",
        "offline": "⏳ ещё не подключён (перезапустите или подождите)",
        "empty": "📭 чатов нет",
    }
    lines = [
        f"💬 *Чаты парсинга* ({PARSER_LABEL})",
        f"Статус: {status_labels.get(parser_status, parser_status)}",
        "",
    ]
    if not chats:
        lines.append("Добавьте чат: `/addchat @channel`")
        return "\n".join(lines)

    ok = sum(1 for c in chats if c.get("status") == "ok")
    bad = sum(1 for c in chats if c.get("status") in ("no_access", "parser_offline"))
    lines.append(f"Всего: {len(chats)} | ✅ доступ: {ok} | ⚠️ проблемы: {bad}")
    lines.append("")

    icons = {"ok": "✅", "no_access": "❌", "parser_offline": "⏳", "disabled": "🚫"}
    for i, chat in enumerate(chats, 1):
        icon = icons.get(chat.get("status"), "❓")
        title = chat.get("title") or "—"
        link = chat["chat_link"]
        cid = chat.get("chat_id") or "—"
        monitored = "📡" if chat.get("monitored") else "—"
        if chat.get("status") == "disabled":
            lines.append(f"{i}. {icon} `{link}` (отключён)")
        else:
            lines.append(f"{i}. {icon} *{title}* {monitored}")
            lines.append(f"   `{link}` → id `{cid}`")
    lines.append("")
    lines.append("Добавить: `/addchat @channel` · Удалить: `/removechat`")
    return "\n".join(lines)

def _keyword_in_text(keyword: str, text_lower: str) -> bool:
    """Проверка ключевого слова с границами — чтобы «паковщик» не ловил «упаковщик»."""
    kw = keyword.lower()
    if len(kw) <= 5 or kw in ("промо", "склад", "сервис", "промо"):
        pattern = rf'(?<![a-zа-яё0-9]){re.escape(kw)}(?![a-zа-яё0-9])'
        return bool(re.search(pattern, text_lower, re.IGNORECASE))
    return kw in text_lower

def detect_category(text: str) -> str:
    if not text:
        return "helper"
    text_lower = text.lower()
    category_map = {
        "loader": [
            "грузчик", "грузчики", "разнорабочий", "разнорабочие", "подсобник", "подсобный рабочий",
            "погрузка", "разгрузка", "такелаж", "такелажник", "выгрузить", "загрузить", "разгрузить",
            "упаковщик", "фасовщик", "комплектовщик", "комплектовка", "упаковка",
        ],
        "promoter": [
            "промоутер", "промоутеры", "промоутерша", "промоутером",
            "раздача листовок", "промо-акция", "промоакция", "листовки",
            "привлекать внимание", "приглашать клиентов", "распространение листовок",
        ],
        "hostess": ["хостес", "встреча гостей", "приветствие", "встречать гостей", "администратор ресепшн"],
        "wardrobe": ["гардеробщик", "гардеробщица", "гардероб", "раздевалка", "прием верхней одежды", "выдача номерков"],
        "animator": ["аниматор", "аниматоры", "аниматорша", "анимация", "детский праздник", "клоун", "ростовые куклы"],
        "waiter": ["официант", "официантка", "официанты", "бармен", "обслуживание гостей", "ресторан", "кафе", "банкет"],
        "driver": ["водитель", "водители", "курьер", "экспедитор", "водительские права", "категория b", "категория с"],
        "security": ["охранник", "контролёр", "контролер", "охрана", "секьюрити", "контроль доступа", "пропускной режим"],
        "parking": ["парковщик", "парковка", "паркинг", "парковочный"],
        "supervisor": ["супервайзер", "супервизор", "координатор", "тимлид", "старший смены"],
        "helper": [
            "хелпер", "хэлпер", "хелперы", "хэлперы", "helper", "helpers",
            "помощник на мероприятие", "помощник организатора", "волонтер", "ассистент",
        ],
    }
    best_category = "helper"
    best_len = 0
    for category, keywords in category_map.items():
        for kw in keywords:
            if _keyword_in_text(kw, text_lower) and len(kw) > best_len:
                best_len = len(kw)
                best_category = category
    return best_category

def is_helper_message(text: str):
    if not text:
        return False, "empty", []
    text_lower = text.lower()
    for phrase in STOP_PHRASES:
        if phrase.lower() in text_lower:
            return False, f"stop_phrase: {phrase}", []
    labor_keywords = ["грузчик", "грузчики", "разнорабочий", "такелажник", "погрузка", "разгрузка", "такелаж"]
    for kw in labor_keywords:
        if kw in text_lower:
            return True, "labor_work", [kw]
    for category in EXCLUDE_CATEGORIES:
        if category.lower() in text_lower:
            if not any(hw in text_lower for hw in ["хелпер", "хэлпер", "промоутер", "аниматор", "грузчик"]):
                return False, f"excluded_category: {category}", []
    found_helpers = [hw for hw in HELPER_KEYWORDS if hw.lower() in text_lower]
    found_hiring = [hv for hv in HIRING_VERBS if hv.lower() in text_lower]
    found_one_time = [ot for ot in ONE_TIME_JOB_KEYWORDS if ot.lower() in text_lower]
    found_payment = [pi for pi in PAYMENT_INDICATORS if pi.lower() in text_lower]
    if found_helpers and found_hiring:
        return True, "helper_plus_hiring", found_helpers + found_hiring[:2]
    if found_helpers and found_one_time:
        return True, "helper_plus_one_time", found_helpers + found_one_time[:2]
    if found_helpers and found_payment:
        return True, "helper_plus_payment", found_helpers + found_payment[:2]
    if found_hiring and "хелпер" in text_lower:
        return True, "hiring_plus_helper_text", found_hiring + ["хелпер"]
    if found_hiring and "промоутер" in text_lower:
        return True, "hiring_plus_promoter_text", found_hiring + ["промоутер"]
    return False, "no_match", []

def clean_message_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def get_message_link(chat_id: int, message_id: int) -> str:
    str_id = str(chat_id)
    if str_id.startswith('-100'):
        clean_id = str_id[4:]
        return f"https://t.me/c/{clean_id}/{message_id}"
    elif str_id.startswith('-'):
        clean_id = str_id[1:]
        return f"https://t.me/c/{clean_id}/{message_id}"
    return f"https://t.me/c/{chat_id}/{message_id}"

async def safe_get_entity(client, chat_link: str):
    try:
        entity = await client.get_entity(chat_link)
        await asyncio.sleep(0.5)
        return entity
    except errors.rpcerrorlist.ChannelPrivateError:
        logger.warning(f"⚠️ Приватный канал (нет доступа): {chat_link}")
        return None
    except errors.rpcerrorlist.UsernameNotOccupiedError:
        logger.warning(f"⚠️ Канал не найден: {chat_link}")
        return None
    except Exception as e:
        logger.warning(f"⚠️ Ошибка доступа к {chat_link}: {type(e).__name__}")
        return None

async def get_new_messages(limit_per_chat: int = PER_CHAT_SCAN_LIMIT):
    global LAST_DEBUG_STATS
    LAST_DEBUG_STATS = _new_stats()
    try:
        async with _parser_lock:
            if _realtime_client and _realtime_client.is_connected():
                logger.info("🔍 Ручная проверка через shared Telethon client")
                return await _scan_all_chats(_realtime_client, limit_per_chat=limit_per_chat, stats=LAST_DEBUG_STATS)

            logger.warning(
                "⚠️ Парсер ещё не подключён — временный Telethon-клиент (может конфликтовать с user_session)"
            )
            client = TelegramClient('user_session', API_ID, API_HASH)
            try:
                await client.start()
                logger.info("🔍 Ручная проверка (отдельный Telethon client)")
                return await _scan_all_chats(client, limit_per_chat=limit_per_chat, stats=LAST_DEBUG_STATS)
            finally:
                if client.is_connected():
                    await client.disconnect()
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в парсере: {e}", exc_info=True)
        return [], []
    finally:
        LAST_DEBUG_STATS["finished_at"] = _iso_now()

async def run_parser():
    orders, closed_data = await get_new_messages()
    return orders, closed_data

async def test_filter(chat_link: str, limit: int = 30):
    client = TelegramClient('user_session', API_ID, API_HASH)
    try:
        await client.start()
        logger.info(f"\n{'='*60}")
        logger.info(f"🧪 ТЕСТ ФИЛЬТРА: {chat_link}")
        logger.info(f"{'='*60}\n")
        entity = await safe_get_entity(client, chat_link)
        if not entity:
            logger.error("❌ Не удалось получить доступ к каналу")
            return
        passed = 0
        blocked = 0
        category_stats = {}
        async for message in client.iter_messages(entity, limit=limit):
            if not message.text:
                continue
            is_rel, reason, keywords = is_helper_message(message.text)
            if is_rel:
                cat = detect_category(message.text)
                category_stats[cat] = category_stats.get(cat, 0) + 1
                passed += 1
                logger.info(f"✅ [{cat}] [{reason}] {message.text[:80]}...")
            else:
                blocked += 1
        logger.info(f"\n{'='*60}")
        logger.info(f"📊 ПРОПУЩЕНО: {passed} | ОТСЕЯНО: {blocked}")
        logger.info(f"📊 Категории: {category_stats}")
        logger.info(f"{'='*60}")
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
    finally:
        if client.is_connected():
            await client.disconnect()
