import re
import logging
import hashlib
import os
import time
os.environ['TZ'] = 'Europe/Moscow'
time.tzset()
from difflib import SequenceMatcher
from telethon import TelegramClient, events
from telethon import errors
import asyncio
from datetime import datetime
from config import API_ID, API_HASH, HELPER_KEYWORDS, EXCLUDE_CATEGORIES, STOP_PHRASES, HIRING_VERBS, ONE_TIME_JOB_KEYWORDS, PAYMENT_INDICATORS
from db import (
    get_target_chats, is_message_processed, mark_vacancy_closed,
    get_last_processed_id, update_last_processed_id,
    save_vacancy, mark_message_processed, has_recent_duplicate_vacancy,
    get_recent_open_vacancies_for_dedupe
)

_client = None

async def get_telethon_client() -> TelegramClient:
    global _client
    if _client is None:
        _client = TelegramClient('user_session', API_ID, API_HASH)
        await _client.start()
    return _client

async def close_telethon_client():
    global _client
    if _client:
        await _client.disconnect()
        _client = None

logger = logging.getLogger(__name__)

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
    # ... (без изменений, код тот же, что был)
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
    # ... (без изменений)
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
    now = datetime.now(message_dt.tzinfo)
    return message_dt.date() == now.date()

def detect_category(text: str) -> str:
    # ... (без изменений, код длинный, оставляем как есть)
    if not text:
        return "helper"
    text_lower = text.lower()
    category_map = {
        "promoter": ["промоутер", "промо", "раздача листовок", "промоутеры", "промоутерша",
                     "привлекать внимание", "приглашать клиентов", "распространение листовок",
                     "промо-акция", "промоакция", "листовки", "промоутером"],
        "hostess": ["хостес", "встреча гостей", "приветствие", "встреча guests",
                    "встречать гостей", "администратор ресепшн"],
        "wardrobe": ["гардеробщик", "гардероб", "гардеробщица", "раздевалка",
                     "прием верхней одежды", "выдача номерков"],
        "animator": ["аниматор", "анимация", "детский праздник", "аниматоры", "аниматорша",
                     "проведение праздников", "клоун", "ростовые куклы", "активный"],
        "helper": ["хелпер", "хэлпер", "помощник на мероприятие", "хелперы", "хэлперы",
                   "helper", "helpers", "помощник организатора", "волонтер", "ассистент"],
        "loader": ["грузчик", "погрузка", "разгрузка", "грузчики", "такелаж",
                   "выгрузить", "загрузить", "разгрузить", "таскать", "переносить",
                   "физическая работа", "тяжелая работа", "подъем", "спуск",
                   "такелажник", "разнорабочий", "подсобный рабочий", "склад"],
        "waiter": ["официант", "официантка", "сервис", "официанты", "бармен",
                   "обслуживание гостей", "ресторан", "кафе", "банкет"],
        "driver": ["водитель", "доставка", "водители", "курьер", "экспедитор",
                   "на автомобиле", "категория", "водительские права"],
        "security": ["охранник", "безопасность", "контролёр", "охрана", "секьюрити",
                     "контроль доступа", "пропускной режим"],
        "parking": ["парковщик", "парковка", "паркинг", "автомобиль",
                    "парковочный", "паковщик"],
        "supervisor": ["супервайзер", "координатор", "менеджер", "супервизор", "тимлид",
                       "руководитель", "старший смены"]
    }
    for category, keywords in category_map.items():
        for kw in keywords:
            if kw in text_lower:
                return category
    return "helper"

def is_helper_message(text: str):
    # ... (без изменений)
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

# ========== ОСНОВНАЯ ФУНКЦИЯ ПАРСИНГА (ПРИНИМАЕТ КЛИЕНТ) ==========
async def get_new_messages(client: TelegramClient, limit_per_chat: int = 500):
    global LAST_DEBUG_STATS
    LAST_DEBUG_STATS = _new_stats()
    all_results = []                     # <-- добавлено
    closed_vacancies_users = []
    target_chats = get_target_chats()
    if not target_chats:
        logger.warning("Нет чатов для парсинга. Добавьте чаты через /addchat")
        return [], []

    try:
        # Клиент уже запущен, не вызываем client.start()
        logger.info("✅ Telethon client ready")
        logger.info(f"🎯 ИЩЕМ ТОЛЬКО: {', '.join(HELPER_KEYWORDS[:10])}...")
        logger.info(f"🚫 ИСКЛЮЧАЕМ: {', '.join(EXCLUDE_CATEGORIES[:10])}...")
        logger.info(f"📋 Всего каналов для проверки: {len(target_chats)}")
        logger.info("⏳ Пропускаем сообщения не за сегодня")

        for i, chat_link in enumerate(target_chats, 1):
            logger.info(f"🔍 [{i}/{len(target_chats)}] Проверяю: {chat_link}")
            entity = await safe_get_entity(client, chat_link)
            if not entity:
                LAST_DEBUG_STATS["chats_failed"] += 1
                LAST_DEBUG_STATS["errors_by_chat"][chat_link] = LAST_DEBUG_STATS["errors_by_chat"].get(chat_link, 0) + 1
                continue

            chat_title = getattr(entity, 'title', None) or 'Без названия'
            chat_id = str(entity.id)
            LAST_DEBUG_STATS["chats_ok"] += 1
            logger.info(f"✅ Успешно подключился к: {chat_title}")

            message_count = 0
            async for message in client.iter_messages(entity, limit=limit_per_chat):
                try:
                    LAST_DEBUG_STATS["messages_scanned"] += 1
                    message_count += 1
                    if not message.text:
                        LAST_DEBUG_STATS["no_text"] += 1
                        continue
                    message_id = str(message.id)

                    if is_message_processed(message_id, chat_id):
                        LAST_DEBUG_STATS["already_sent"] += 1
                        continue

                    if not is_message_for_today(message.date):
                        LAST_DEBUG_STATS["old_messages"] += 1
                        continue

                    if message.is_reply:
                        original_message = await message.get_reply_message()
                        if original_message and original_message.id:
                            original_id = str(original_message.id)
                            close_markers = ["закрыт", "закрыта", "❌", "набор завершён", "вакансия закрыта", "не актуально"]
                            if any(marker in message.text.lower() for marker in close_markers):
                                users = mark_vacancy_closed(original_id, chat_id)
                                if users:
                                    closed_vacancies_users.append((f"{chat_id}_{original_id}", users))
                                LAST_DEBUG_STATS["closed_vacancies"] += 1
                                logger.info(f"🔒 Вакансия {original_id} в {chat_title} помечена как закрытая, уведомлены {len(users)}")
                                continue

                    is_relevant, reason, keywords = is_helper_message(message.text)
                    LAST_DEBUG_STATS["reasons"][reason] = LAST_DEBUG_STATS["reasons"].get(reason, 0) + 1
                    if not is_relevant:
                        LAST_DEBUG_STATS["non_relevant"] += 1
                        continue

                    category = detect_category(message.text)
                    LAST_DEBUG_STATS["categories"][category] = LAST_DEBUG_STATS["categories"].get(category, 0) + 1
                    cleaned_text = clean_message_text(message.text)
                    message_link = get_message_link(entity.id, message.id)
                    vacancy_id = f"{chat_id}_{message_id}"
                    author_contact = extract_contact_from_text(message.text)
                    address = extract_address_from_text(message.text)
                    dedupe_key = build_vacancy_dedupe_key(message.text, author_contact)

                    duplicate_type = detect_duplicate_type(message.text, author_contact, dedupe_key)
                    if duplicate_type:
                        if duplicate_type == "exact":
                            LAST_DEBUG_STATS["duplicates_exact"] += 1
                        else:
                            LAST_DEBUG_STATS["duplicates_fuzzy"] += 1
                        mark_message_processed(message_id, chat_id)
                        continue

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
                        published_at=message.date.strftime("%Y-%m-%d %H:%M:%S")
                    )

                    result = {
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
                    all_results.append(result)
                    LAST_DEBUG_STATS["matched"] += 1
                    mark_message_processed(message_id, chat_id)
                    logger.info(f"✅ {chat_title} [{category}]: {cleaned_text[:60]}... (причина: {reason})")
                    await asyncio.sleep(0.2)

                except Exception as e:
                    LAST_DEBUG_STATS["errors"] += 1
                    LAST_DEBUG_STATS["errors_by_chat"][chat_title] = LAST_DEBUG_STATS["errors_by_chat"].get(chat_title, 0) + 1
                    logger.warning(f"⚠️ Пропущено сообщение chat={chat_title} id={getattr(message, 'id', '?')}: {e}")
                    continue

            logger.info(f"📊 В канале {chat_title} просмотрено {message_count} сообщений")
            await asyncio.sleep(1)

        logger.info(f"🏁 Парсинг завершён! Найдено вакансий: {len(all_results)}")
        logger.info(f"📊 Статистика: успешно обработано {LAST_DEBUG_STATS['chats_ok']} из {LAST_DEBUG_STATS['chats_total']} каналов")
        if LAST_DEBUG_STATS["categories"]:
            logger.info(f"📊 Распределение по категориям: {LAST_DEBUG_STATS['categories']}")

    except Exception as e:
        logger.error(f"❌ Критическая ошибка в парсере: {e}", exc_info=True)
    finally:
        LAST_DEBUG_STATS["finished_at"] = _iso_now()
        # НЕ закрываем клиент здесь – он переиспользуется

    return all_results, closed_vacancies_users

async def run_parser(client: TelegramClient):
    orders, closed_data = await get_new_messages(client)
    return orders, closed_data

async def test_filter(chat_link: str, limit: int = 30):
    # Для теста создаём отдельного клиента, чтобы не мешать основному
    client = TelegramClient('test_session', API_ID, API_HASH)
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