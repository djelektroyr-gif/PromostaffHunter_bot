import re
import logging
from telethon import TelegramClient, events
from telethon import errors
import asyncio
from datetime import datetime
from config import API_ID, API_HASH, HELPER_KEYWORDS, EXCLUDE_CATEGORIES, STOP_PHRASES, HIRING_VERBS, ONE_TIME_JOB_KEYWORDS, PAYMENT_INDICATORS
from db import (
    get_target_chats, is_message_processed, mark_vacancy_closed,
    get_last_processed_id, update_last_processed_id,
    save_vacancy, mark_message_processed
)

logger = logging.getLogger(__name__)

# Глобальный клиент для real-time режима (один на весь бот)
_realtime_client = None

async def start_realtime_listener(bot_callback):
    """
    Запускает real-time прослушивание новых сообщений.
    bot_callback – асинхронная функция, которая будет вызвана для каждого нового сообщения.
    Она должна принимать параметр order (словарь с вакансией).
    """
    global _realtime_client
    _realtime_client = TelegramClient('user_session', API_ID, API_HASH)
    await _realtime_client.start()
    logger.info("✅ Real-time listener подключён")

    target_chats = get_target_chats()
    for link in target_chats:
        try:
            entity = await _realtime_client.get_entity(link)
            last_id = get_last_processed_id(str(entity.id))
            # Не передаём offset_id, Telethon сам будет получать только новые сообщения
        except Exception as e:
            logger.warning(f"Не удалось получить entity для {link}: {e}")

        @_realtime_client.on(events.NewMessage(chats=target_chats))
    async def handler(event):
        logger.info(f"⚡ Real-time: получено новое сообщение из чата {event.chat_id}")
        message = event.message
        chat = await event.get_chat()
        chat_id = str(chat.id)
        message_id = message.id

        # Пропускаем уже обработанные
        if is_message_processed(str(message_id), chat_id):
            return

        # Проверяем, не старше ли сообщение 3 дней
        if (datetime.now(message.date.tzinfo) - message.date).days > 3:
            return

        # Проверяем, не является ли сообщение ответом с маркером "закрыто"
        if message.is_reply:
            original = await message.get_reply_message()
            if original:
                close_markers = ["закрыт", "закрыта", "❌", "набор завершён", "вакансия закрыта", "не актуально"]
                if any(m in message.text.lower() for m in close_markers):
                    original_id = str(original.id)
                    users = mark_vacancy_closed(original_id, chat_id)
                    if users:
                        logger.info(f"🔒 Вакансия {original_id} закрыта, уведомлены {len(users)} пользователей")
                    return

        # Проверка релевантности
        is_rel, reason, keywords = is_helper_message(message.text)
        if not is_rel:
            return

        # Определение категории, адреса, контакта
        category = detect_category(message.text)
        cleaned_text = clean_message_text(message.text)
        message_link = get_message_link(chat.id, message.id)
        vacancy_id = f"{chat_id}_{message_id}"
        author_contact = extract_contact_from_text(message.text)
        address = extract_address_from_text(message.text)

        # Сохраняем в БД
        save_vacancy(
            vacancy_id=vacancy_id,
            source_chat=chat_id,
            source_chat_title=chat.title or "Без названия",
            category_code=category,
            message_text=cleaned_text[:2000],
            message_link=message_link,
            author_contact=author_contact,
            address=address,
            is_closed=False
        )

        # Формируем order для отправки подписчикам
        order = {
            "chat_title": chat.title or "Без названия",
            "message_text": cleaned_text,
            "message_link": message_link,
            "category": category,
            "chat_id": chat_id,
            "message_id": str(message_id),
            "address": address,
            "author_contact": author_contact,
        }

        # Вызываем колбэк для отправки подписчикам
        await bot_callback(order)

        # Отмечаем сообщение как обработанное
        mark_message_processed(str(message_id), chat_id)
        update_last_processed_id(chat_id, message_id)
        logger.info(f"⚡ Real-time: новая вакансия из {chat.title}")


# ========== ОСТАЛЬНЫЕ ФУНКЦИИ ПАРСЕРА (НЕ ИЗМЕНЯЮТСЯ) ==========

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
    text_lower = text.lower()
    metro_match = re.search(r'м\.\s*([А-Яа-яёЁ\-]+)', text)
    if metro_match:
        return f"метро {metro_match.group(1)}"
    street_match = re.search(r'(ул\.|улица)\s+([А-Яа-яёЁ\-\.\s]+?)(?:\s|$)', text)
    if street_match:
        return f"{street_match.group(1)} {street_match.group(2)}".strip()
    city_match = re.search(r'(?:в\s+)?(Москва|МО|Подольск|Химки|Мытищи|Красногорск|Люберцы|Балашиха|Королёв|Одинцово|Домодедово|Железнодорожный|Видное|Щёлково|Электросталь|Коломна|Серпухов)', text_lower)
    if city_match:
        return city_match.group(0)
    return None


def detect_category(text: str) -> str:
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


def is_helper_message(text: str) -> tuple[bool, str, list]:
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


async def safe_get_entity(client: TelegramClient, chat_link: str):
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


async def get_new_messages(limit_per_chat: int = 500) -> tuple[list, list]:
    global LAST_DEBUG_STATS
    LAST_DEBUG_STATS = _new_stats()
    client = TelegramClient('user_session', API_ID, API_HASH)
    all_results = []
    closed_vacancies_users = []
    MAX_AGE_DAYS = 3

    target_chats = get_target_chats()
    if not target_chats:
        logger.warning("Нет чатов для парсинга. Добавьте чаты через /addchat")
        return [], []

    try:
        await client.start()
        logger.info("✅ Telethon client started")
        logger.info(f"🎯 ИЩЕМ ТОЛЬКО: {', '.join(HELPER_KEYWORDS[:10])}...")
        logger.info(f"🚫 ИСКЛЮЧАЕМ: {', '.join(EXCLUDE_CATEGORIES[:10])}...")
        logger.info(f"📋 Всего каналов для проверки: {len(target_chats)}")
        logger.info(f"⏳ Пропускаем сообщения старше {MAX_AGE_DAYS} дней")

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

                    if (datetime.now(message.date.tzinfo) - message.date).days > MAX_AGE_DAYS:
                        LAST_DEBUG_STATS["old_messages"] += 1
                        continue

                    # Обработка закрытых вакансий
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
                                logger.info(f"🔒 Вакансия {original_id} в {chat_title} помечена как закрытая, уведомлены {len(users)} пользователей")
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

                    save_vacancy(
                        vacancy_id=vacancy_id,
                        source_chat=chat_id,
                        source_chat_title=chat_title,
                        category_code=category,
                        message_text=cleaned_text[:2000],
                        message_link=message_link,
                        author_contact=author_contact,
                        address=address,
                        is_closed=False
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
        if client.is_connected():
            await client.disconnect()

    return all_results, closed_vacancies_users


async def run_parser() -> tuple[list, list]:
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