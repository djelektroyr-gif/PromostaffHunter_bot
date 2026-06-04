import re
import os
import logging
import hashlib
from difflib import SequenceMatcher
from telethon import TelegramClient, events
from telethon import errors
import asyncio
from datetime import datetime, timezone, timedelta
from config import (
    API_ID, API_HASH, get_telegram_session_name, describe_session_search,
    HELPER_KEYWORDS, EXCLUDE_CATEGORIES, STOP_PHRASES,
    HIRING_VERBS, ONE_TIME_JOB_KEYWORDS, PAYMENT_INDICATORS, VACANCY_MAX_AGE_HOURS,
)
from db import (
    get_target_chats, is_message_processed, mark_vacancy_closed,
    get_last_processed_id, update_last_processed_id,
    save_vacancy, mark_message_processed, has_recent_duplicate_vacancy,
    get_recent_open_vacancies_for_dedupe
)
from db_backend import run_db

logger = logging.getLogger(__name__)

_realtime_client = None
_monitored_chat_ids = set()
_parser_lock = asyncio.Lock()
_last_health_alert = {}
_background_tasks: set[asyncio.Task] = set()
PARSER_POLL_INTERVAL_SEC = 300
PARSER_HEALTH_INTERVAL_SEC = 600
PARSER_RECONNECT_DELAY_SEC = 30
PARSER_SESSION_MISSING_BACKOFF_SEC = 1800
PER_CHAT_SCAN_LIMIT = 120
PARSER_LABEL = "Парсер групп (Telethon)"
MOSCOW_TZ = timezone(timedelta(hours=3))
_session_config_alert_sent = False


def spawn_background_task(coro) -> asyncio.Task:
    """create_task + ссылка, иначе GC убивает задачу (asyncio docs)."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


class SessionNotConfiguredError(Exception):
    """Сессия Telethon отсутствует или не авторизована — на сервере нельзя вводить телефон интерактивно."""


def session_file_path() -> str:
    return f"{get_telegram_session_name()}.session"


def is_session_file_present() -> bool:
    return os.path.isfile(session_file_path())


async def create_authorized_client() -> TelegramClient:
    """Подключение без input() — только если .session уже авторизован."""
    session_name = get_telegram_session_name()
    path = f"{session_name}.session"
    if not os.path.isfile(path):
        raise SessionNotConfiguredError(
            f"Файл {path} не найден. {describe_session_search()}\n"
            "Bothost: включите «Общее хранилище» и положите *.session в /app/shared "
            "(или один раз в /app — перенесётся автоматически)."
        )
    if not API_ID or not API_HASH:
        raise SessionNotConfiguredError("Задайте API_ID и API_HASH в переменных окружения.")

    logger.info(f"Telethon session: {path}")
    client = TelegramClient(session_name, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SessionNotConfiguredError(
            f"Файл {path} есть, но сессия не авторизована. "
            "Пересоздайте локально (интерактивный вход) и снова загрузите на сервер."
        )
    return client

def make_vacancy_id(chat_id: str, message_id: str, dedupe_key: str = None) -> str:
    unique_str = dedupe_key or f"{chat_id}_{message_id}"
    return hashlib.md5(unique_str.encode()).hexdigest()[:16]


def chat_id_aliases(chat_id) -> set:
    """Telethon отдаёт -100…, в difference иногда голый id канала — храним все формы."""
    raw = str(chat_id).strip()
    aliases = {raw}
    if raw.startswith("-100") and len(raw) > 4:
        aliases.add(raw[4:])
    elif raw.isdigit():
        aliases.add(f"-100{raw}")
    return aliases


async def refresh_monitored_chat_ids(client) -> set:
    """Резолвит ссылки из БД в numeric chat_id — без этого Telethon-парсер не видит группы."""
    global _monitored_chat_ids
    ids = set()
    for link in await run_db(get_target_chats):
        try:
            entity = await client.get_entity(link)
            ids.update(chat_id_aliases(entity.id))
            await asyncio.sleep(1.2)
        except Exception as e:
            logger.warning(f"Не удалось резолвить чат {link}: {e}")
    _monitored_chat_ids = ids
    logger.info(f"📡 Мониторинг {len(await run_db(get_target_chats))} чатов ({len(_monitored_chat_ids)} id-алиасов)")
    return ids


def is_chat_monitored(chat_id) -> bool:
    if not _monitored_chat_ids:
        return False
    return bool(chat_id_aliases(chat_id) & _monitored_chat_ids)

async def _process_single_message(message, chat, chat_id: str, chat_title: str, stats: dict = None):
    """Обрабатывает одно сообщение: фильтр, категория, дедуп. Возвращает order или None."""
    if not message.text:
        if stats is not None:
            stats["no_text"] += 1
        return None
    message_id = str(message.id)
    if await run_db(is_message_processed, message_id, chat_id):
        if stats is not None:
            stats["already_sent"] += 1
        return None
    if not is_message_recent(message.date):
        if stats is not None:
            stats["old_messages"] += 1
        return None

    if message.is_reply:
        original_message = await message.get_reply_message()
        if original_message and original_message.id:
            close_markers = ["закрыт", "закрыта", "❌", "набор завершён", "вакансия закрыта", "не актуально"]
            if any(marker in message.text.lower() for marker in close_markers):
                original_id = str(original_message.id)
                vacancy_id, users = await run_db(mark_vacancy_closed, original_id, chat_id)
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

    duplicate_type = await run_db(detect_duplicate_type, message.text, author_contact, dedupe_key)
    if duplicate_type:
        if stats is not None:
            if duplicate_type == "exact":
                stats["duplicates_exact"] += 1
            else:
                stats["duplicates_fuzzy"] += 1
        await run_db(mark_message_processed, message_id, chat_id)
        return None

    vacancy_id = make_vacancy_id(chat_id, message_id, dedupe_key)
    await run_db(
        save_vacancy,
        vacancy_id,
        chat_id,
        chat_title,
        category,
        cleaned_text[:2000],
        message_link,
        author_contact,
        address,
        False,
        dedupe_key,
        message.date.strftime("%Y-%m-%d %H:%M:%S"),
    )
    await run_db(mark_message_processed, message_id, chat_id)
    await run_db(update_last_processed_id, chat_id, message.id)
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

async def _scan_all_chats(client, limit_per_chat: int = PER_CHAT_SCAN_LIMIT, stats: dict = None, *, incremental: bool = False):
    all_results = []
    closed_vacancies_users = []
    target_chats = await run_db(get_target_chats)
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

        iter_kwargs = {"limit": limit_per_chat}
        if incremental:
            last_id = await run_db(get_last_processed_id, chat_id)
            if last_id:
                iter_kwargs["min_id"] = last_id

        async for message in client.iter_messages(entity, **iter_kwargs):
            if stats is not None:
                stats["messages_scanned"] += 1
                if stats["messages_scanned"] % 25 == 0:
                    await asyncio.sleep(0)
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
    await asyncio.sleep(90)
    while _realtime_client and _realtime_client.is_connected():
        stats = _new_stats("periodic")
        try:
            async with _parser_lock:
                logger.info("🔄 Плановая проверка новых вакансий (incremental)...")
                orders, closed_data = await _scan_all_chats(
                    _realtime_client,
                    limit_per_chat=PER_CHAT_SCAN_LIMIT,
                    stats=stats,
                    incremental=True,
                )
            stats["finished_at"] = _iso_now()
            global LAST_DEBUG_STATS
            LAST_DEBUG_STATS = stats
            if closed_data and closed_callback:
                await closed_callback(closed_data)
            for order in orders:
                bot_callback(order)
            logger.info(
                f"🔄 Плановая проверка: новых вакансий {len(orders)}, "
                f"просмотрено сообщений {stats['messages_scanned']}, "
                f"отсеяно {stats['non_relevant']}, дубли exact/fuzzy "
                f"{stats['duplicates_exact']}/{stats['duplicates_fuzzy']}, "
                f"закрыто {len(closed_data or [])}"
            )
        except Exception as e:
            logger.error(f"Ошибка плановой проверки: {e}", exc_info=True)
        await asyncio.sleep(PARSER_POLL_INTERVAL_SEC)


async def _parser_health_loop(health_notify_callback=None):
    """Следит за online/offline и резолвом чатов; алерт админу не чаще раза в час."""
    await asyncio.sleep(180)
    while True:
        try:
            snap = get_parser_status_snapshot()
            issues = []
            if not snap["online"]:
                issues.append("offline")
            elif snap["active_chats"] and snap["monitored"] < snap["active_chats"]:
                issues.append(f"unresolved:{snap['active_chats'] - snap['monitored']}")

            if issues and _realtime_client and _realtime_client.is_connected():
                if snap["monitored"] < snap["active_chats"]:
                    try:
                        async with _parser_lock:
                            await refresh_monitored_chat_ids(_realtime_client)
                    except Exception as e:
                        logger.warning(f"Health: не удалось обновить chat_id: {e}")

            if issues and health_notify_callback:
                key = ",".join(issues)
                now = datetime.now(timezone.utc)
                last = _last_health_alert.get(key)
                if not last or (now - last).total_seconds() > 3600:
                    if "offline" in key:
                        text = (
                            f"⚠️ *{PARSER_LABEL} offline*\n\n"
                            f"Бот переподключается автоматически. "
                            f"Если алерт повторяется — проверьте `user_session` и логи."
                        )
                    else:
                        unresolved = snap["active_chats"] - snap["monitored"]
                        text = (
                            f"⚠️ *Парсер не видит {unresolved} чат(ов)*\n\n"
                            f"В БД: {snap['active_chats']}, в мониторинге: {snap['monitored']}.\n"
                            f"Откройте «📋 Список чатов парсинга» или `/listchats`."
                        )
                    try:
                        await health_notify_callback(text)
                    except Exception as e:
                        logger.warning(f"Health notify failed: {e}")
                    _last_health_alert[key] = now
        except Exception as e:
            logger.error(f"Health loop error: {e}", exc_info=True)
        await asyncio.sleep(PARSER_HEALTH_INTERVAL_SEC)

# ===================== ПАРСЕР ГРУПП (TELETHON) =====================

async def start_realtime_listener(bot_callback, closed_callback=None, health_notify_callback=None):
    global _realtime_client, _session_config_alert_sent
    from session_lock import acquire_session_lock, SessionLockError

    try:
        acquire_session_lock()
    except SessionLockError as e:
        logger.error(str(e))
        if health_notify_callback:
            try:
                await health_notify_callback(f"❌ *Не запущен {PARSER_LABEL}*\n\n{e}")
            except Exception:
                pass
        return

    spawn_background_task(_parser_health_loop(health_notify_callback))
    reconnect_delay = PARSER_RECONNECT_DELAY_SEC

    while True:
        try:
            _realtime_client = await create_authorized_client()
            reconnect_delay = PARSER_RECONNECT_DELAY_SEC
            logger.info(f"✅ {PARSER_LABEL} подключён")

            await refresh_monitored_chat_ids(_realtime_client)

            async with _parser_lock:
                logger.info("🔄 Стартовая синхронизация вакансий...")
                startup_stats = _new_stats("startup")
                startup_orders, startup_closed = await _scan_all_chats(
                    _realtime_client,
                    limit_per_chat=PER_CHAT_SCAN_LIMIT,
                    stats=startup_stats,
                    incremental=False,
                )
                startup_stats["finished_at"] = _iso_now()
                global LAST_DEBUG_STATS
                LAST_DEBUG_STATS = startup_stats
            if startup_closed and closed_callback:
                await closed_callback(startup_closed)
            for order in startup_orders:
                bot_callback(order)
            logger.info(
                f"✅ Стартовая синхронизация: {len(startup_orders)} вакансий, "
                f"просмотрено {startup_stats['messages_scanned']}, "
                f"отсеяно {startup_stats['non_relevant']}, "
                f"уже в БД {startup_stats['already_sent']}"
            )

            async def on_new_message(event):
                if not is_chat_monitored(event.chat_id):
                    logger.debug(
                        f"{PARSER_LABEL}: сообщение chat_id={event.chat_id} вне мониторинга"
                    )
                    return

                logger.info(f"⚡ {PARSER_LABEL}: новое сообщение chat_id={event.chat_id}")
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
                        bot_callback(result)
                        logger.info(
                            f"⚡ {PARSER_LABEL}: вакансия [{result.get('category')}] из «{chat_title}»"
                        )
                except Exception as e:
                    logger.warning(f"⚠️ {PARSER_LABEL}: ошибка chat={chat_title}: {e}")

            _realtime_client.add_event_handler(on_new_message, events.NewMessage())
            spawn_background_task(_periodic_scan_loop(bot_callback, closed_callback))
            await _realtime_client.run_until_disconnected()
        except SessionNotConfiguredError as e:
            logger.error(f"{PARSER_LABEL}: {e}")
            reconnect_delay = PARSER_SESSION_MISSING_BACKOFF_SEC
            if health_notify_callback and not _session_config_alert_sent:
                _session_config_alert_sent = True
                try:
                    await health_notify_callback(
                        f"❌ *{PARSER_LABEL} не запущен*\n\n{e}\n\n"
                        f"Бот (aiogram) работает, вакансии не парсятся.\n"
                        f"Повторная попытка через {PARSER_SESSION_MISSING_BACKOFF_SEC // 60} мин."
                    )
                except Exception:
                    pass
        except EOFError:
            msg = (
                "Telethon запросил телефон интерактивно (EOF) — на сервере нет TTY. "
                f"Загрузите авторизованный `{session_file_path()}`."
            )
            logger.error(f"{PARSER_LABEL}: {msg}")
            reconnect_delay = PARSER_SESSION_MISSING_BACKOFF_SEC
            if health_notify_callback and not _session_config_alert_sent:
                _session_config_alert_sent = True
                try:
                    await health_notify_callback(f"❌ *{PARSER_LABEL}*\n\n{msg}")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Парсер отключился: {e}", exc_info=True)
        finally:
            if _realtime_client and _realtime_client.is_connected():
                try:
                    await _realtime_client.disconnect()
                except Exception:
                    pass
            _realtime_client = None
            _monitored_chat_ids.clear()

        logger.warning(f"Переподключение {PARSER_LABEL} через {reconnect_delay} с...")
        await asyncio.sleep(reconnect_delay)

async def stop_realtime_listener():
    global _realtime_client
    for task in list(_background_tasks):
        if not task.done():
            task.cancel()
    if _background_tasks:
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)
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
        "session_file": is_session_file_present(),
    }


def format_parser_status_line(snapshot: dict) -> str:
    if not snapshot.get("session_file"):
        return (
            f"❌ {PARSER_LABEL}: нет файла `{session_file_path()}`\n"
            f"   Загрузите авторизованную сессию на сервер (см. docs/DEVELOPMENT.md §11)"
        )
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

def _empty_debug_stats() -> dict:
    return {
        "started_at": None,
        "finished_at": None,
        "chats_total": 0,
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
        "run_kind": None,
    }


def _new_stats(run_kind: str = "scan") -> dict:
    stats = _empty_debug_stats()
    stats["started_at"] = _iso_now()
    stats["run_kind"] = run_kind
    stats["chats_total"] = len(get_target_chats())
    return stats


LAST_DEBUG_STATS = _empty_debug_stats()


def get_last_debug_report() -> str:
    s = LAST_DEBUG_STATS
    snap = get_parser_status_snapshot()
    parser_line = format_parser_status_line(snap)

    if not s.get("started_at"):
        return (
            "🧪 *Последний прогон парсера*\n\n"
            "Ещё не было завершённого прогона после перезапуска.\n"
            f"{parser_line}\n\n"
            "Запустите `/check_now` или дождитесь плановой проверки (~5 мин)."
        )

    lines = [
        "🧪 *Последний прогон парсера*",
        f"Тип: {s.get('run_kind') or '—'}",
        f"Старт: {s.get('started_at')}",
        f"Финиш: {s.get('finished_at') or '⏳ в процессе…'}",
        parser_line,
        f"Чатов: {s.get('chats_ok', 0)}/{s.get('chats_total', 0)} успешно, ошибок: {s.get('chats_failed', 0)}",
        f"Сообщений просмотрено: {s.get('messages_scanned', 0)}",
        f"Совпадений найдено: {s.get('matched', 0)}",
        f"Отсеяно: {s.get('non_relevant', 0)} | без текста: {s.get('no_text', 0)} | "
        f"уже обработано: {s.get('already_sent', 0)} | старых: {s.get('old_messages', 0)} | "
        f"закрыто: {s.get('closed_vacancies', 0)}",
        f"Дубли: exact={s.get('duplicates_exact', 0)} | fuzzy={s.get('duplicates_fuzzy', 0)}",
        f"Локальных ошибок: {s.get('errors', 0)}",
    ]

    if not s.get("finished_at"):
        try:
            started = datetime.strptime(s["started_at"], "%Y-%m-%d %H:%M:%S")
            age_min = (datetime.now() - started).total_seconds() / 60
            if age_min > 15:
                lines.append(
                    f"\n⚠️ *Прогон «в процессе» уже {int(age_min)} мин* — "
                    "скорее всего отчёт устарел (перезапуск или зависание lock). "
                    "Нажмите `/check_now`."
                )
        except ValueError:
            pass
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


def _normalize_for_fuzzy_dedupe(text: str) -> str:
    """Убирает дату/адрес — ловит повторы одной кампании с разными локациями."""
    normalized = _normalize_for_dedupe(text)
    normalized = re.sub(
        r"\b\d{1,2}[\.\-/]\d{1,2}(?:[\.\-/]\d{2,4})?\b",
        " ",
        normalized,
    )
    normalized = re.sub(
        r"\b(завтра|сегодня|послезавтра|метро|м\.|ул\.|улица|проспект|пр\.)\b",
        " ",
        normalized,
    )
    normalized = re.sub(
        r"\b(москва|мо|подмосков|лобня|немчиновка|калужская|русаковская|победы)\b",
        " ",
        normalized,
    )
    return re.sub(r"\s+", " ", normalized).strip()


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
    fuzzy_text = _normalize_for_fuzzy_dedupe(text)
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
        fuzzy_similarity = SequenceMatcher(
            None, fuzzy_text, _normalize_for_fuzzy_dedupe(row.get("message_text", ""))
        ).ratio()
        if similarity >= 0.82 or fuzzy_similarity >= 0.78:
            return "fuzzy"
    return None

def is_message_recent(message_dt: datetime, max_age_hours: int = None) -> bool:
    """Вакансия не старше max_age_hours (по умолчанию VACANCY_MAX_AGE_HOURS)."""
    if not message_dt:
        return False
    hours = max_age_hours if max_age_hours is not None else VACANCY_MAX_AGE_HOURS
    if message_dt.tzinfo is None:
        message_dt = message_dt.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - message_dt.astimezone(timezone.utc)
    return age <= timedelta(hours=hours)


def is_message_for_today(message_dt: datetime) -> bool:
    """Обратная совместимость — делегирует в is_message_recent."""
    return is_message_recent(message_dt)

def _normalize_metro_token(value: str) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"[^\w\s\-]", "", value.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    for prefix in ("станция ", "м ", "м."):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    return cleaned


def extract_metro_tokens(text: str) -> list:
    """Станции метро из текста вакансии (нормализованные)."""
    if not text:
        return []
    tokens = set()
    addr = extract_address_from_text(text)
    if addr:
        metro_in_addr = re.search(r"метро\s+([^\n,]{2,50})", addr, re.IGNORECASE)
        if metro_in_addr:
            tokens.add(_normalize_metro_token(metro_in_addr.group(1)))
    for match in re.finditer(
        r"(?:м\.|метро)\s*[:\-]?\s*(?:🚇\s*)?([А-Яа-яёЁ\-A-Za-z\s]{2,40})",
        text,
        re.IGNORECASE,
    ):
        token = _normalize_metro_token(match.group(1))
        if token and len(token) >= 3:
            tokens.add(token)
    return sorted(tokens)


def vacancy_matches_user_metro(vacancy_text: str, address: str, user_metro_csv: str) -> bool:
    """True если у пользователя нет фильтра, в вакансии нет метро, или есть пересечение."""
    if not user_metro_csv or not user_metro_csv.strip():
        return True
    user_zones = [_normalize_metro_token(z) for z in user_metro_csv.split(",") if z.strip()]
    if not user_zones:
        return True
    combined = f"{vacancy_text or ''} {address or ''}"
    vac_tokens = extract_metro_tokens(combined)
    if not vac_tokens:
        return True
    for vt in vac_tokens:
        for uz in user_zones:
            if uz in vt or vt in uz:
                return True
    return False


async def inspect_parser_chats() -> tuple:
    """Проверка доступа Telethon к чатам из БД. Возвращает (список, статус парсера)."""
    from db import list_target_chats

    chats_db = await run_db(list_target_chats)
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

_CATEGORY_TIEBREAK = (
    "loader", "promoter", "hostess", "waiter", "animator", "wardrobe",
    "driver", "security", "parking", "supervisor", "helper",
)

_CATEGORY_KEYWORDS = {
    "loader": [
        "грузчик", "грузчики", "разнорабочий", "разнорабочие", "подсобник", "подсобный рабочий",
        "погрузка", "разгрузка", "выгрузка", "выгрузк", "такелаж", "такелажник",
        "выгрузить", "загрузить", "разгрузить", "перемещение фур", "фасовочн", "конвейер",
        "упаковщик", "фасовщик", "комплектовщик", "комплектовка", "упаковка на склад",
        "складской работник", "на склад", "рохл", "паллет", "складирован",
        "производств", "фасовоч",
    ],
    "promoter": [
        "промоутер", "промоутеры", "промоутерша", "промоутером", "промо персонал", "промо",
        "раздача листовок", "промо-акция", "промоакция", "листовки", "анкетирован",
        "опрос людей", "опрос на улице",
        "привлекать внимание", "приглашать клиентов", "распространение листовок",
        "промо на", "промо в", "позиция: промо", "позиция промо",
    ],
    "hostess": ["хостес", "встреча гостей", "приветствие", "встречать гостей", "администратор ресепшн"],
    "wardrobe": ["гардеробщик", "гардеробщица", "гардероб", "раздевалка", "прием верхней одежды", "выдача номерков"],
    "animator": [
        "аниматор", "аниматоры", "аниматорша", "анимация", "детский праздник", "клоун",
        "ростовые куклы", "массовк", "массовка",
    ],
    "waiter": ["официант", "официантка", "официанты", "бармен", "обслуживание гостей", "ресторан", "кафе", "банкет"],
    "driver": ["водитель", "водители", "курьер", "экспедитор", "водительские права", "категория b", "категория с"],
    "security": ["охранник", "контролёр", "контролер", "охрана", "секьюрити", "контроль доступа", "пропускной режим"],
    "parking": ["парковщик", "парковка vip", "паркинг", "парковочный"],
    "supervisor": [
        "супервайзер", "супервизор", "тимлид", "старший смены",
        "координатор промо", "координатор проекта", "координатор мероприят",
        "контроль промо-персонала", "контроль промо персонала",
    ],
    "helper": [
        "хелпер", "хэлпер", "хелперы", "хэлперы", "helper", "helpers",
        "помощник на мероприятие", "помощник организатора", "волонтер",
        "помощь на площадке", "помощники на площадке", "бекфотограф", "бэкстейдж",
        "ассистент по акт",
    ],
}

_LABOR_HINTS = (
    "грузчик", "упаковщик", "фасовщик", "комплектовщик", "разгруз", "погруз", "выгруз",
    "склад", "рохл", "паллет", "фасовоч", "конвейер", "производств", "50 кг",
)
_PROMO_HINTS = ("промоутер", "листовок", "промо-акция", "раздача листовок", "промо персонал", "промо", "анкетирован")
_NON_SUPERVISOR_COORDINATOR = (
    "организатор", "координатор свад", "свадеб", "#организатора", "координатора",
    "event hunter", "ведущий", "фотограф", "видеограф",
)


def split_vacancy_blocks(text: str) -> list:
    """Digest-посты «1. … 2. …» — категория по первому блоку с явной ролью."""
    if not text:
        return []
    parts = re.split(r"(?=\n\s*\d+[\.\)]\s)", text)
    blocks = [p.strip() for p in parts if p.strip()]
    return blocks if len(blocks) > 1 else [text]


def _score_categories(text_lower: str) -> dict:
    scores = {}
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if _keyword_in_text(kw, text_lower):
                scores[category] = scores.get(category, 0) + len(kw)
    if any(marker in text_lower for marker in _NON_SUPERVISOR_COORDINATOR):
        scores.pop("supervisor", None)
    return scores


def _pick_category_from_scores(scores: dict, text_lower: str) -> str | None:
    if not scores:
        return None
    if scores.get("loader") and scores.get("helper") and any(w in text_lower for w in _LABOR_HINTS):
        return "loader"
    if scores.get("promoter") and scores.get("helper") and any(w in text_lower for w in _PROMO_HINTS):
        return "promoter"
    if scores.get("loader") and scores.get("parking") and any(w in text_lower for w in _LABOR_HINTS):
        return "loader"
    if scores.get("driver") and scores.get("helper") and "водител" in text_lower:
        return "driver"

    max_score = max(scores.values())
    winners = [cat for cat, score in scores.items() if score == max_score]
    if len(winners) == 1:
        return winners[0]
    for preferred in _CATEGORY_TIEBREAK:
        if preferred in winners:
            return preferred
    return winners[0]


def _fallback_category(text_lower: str) -> str:
    if any(w in text_lower for w in ("хелпер", "хэлпер", "helper", "бекфотограф", "бэкстейдж")):
        return "helper"
    if any(w in text_lower for w in _LABOR_HINTS):
        return "loader"
    if any(w in text_lower for w in _PROMO_HINTS):
        return "promoter"
    if "массовк" in text_lower:
        return "animator"
    if "аниматор" in text_lower:
        return "animator"
    if "водител" in text_lower:
        return "driver"
    if "супервайзер" in text_lower or "супервизор" in text_lower:
        return "supervisor"
    return "helper"


def detect_category(text: str) -> str:
    """Категория только по тексту поста; название группы/канала не учитывается."""
    if not text:
        return "helper"
    blocks = split_vacancy_blocks(text)
    for block in blocks:
        text_lower = block.lower()
        cat = _pick_category_from_scores(_score_categories(text_lower), text_lower)
        if cat:
            return cat
    text_lower = text.lower()
    cat = _pick_category_from_scores(_score_categories(text_lower), text_lower)
    return cat if cat else _fallback_category(text_lower)


def is_unpaid_vacancy(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    if re.search(r"оплат\w*\s*[💵:]?\s*нет\b", text_lower):
        return True
    if re.search(r"💵\s*нет\b", text_lower):
        return True
    if any(p in text_lower for p in ("безмерную благодарность", "без оплаты", "бесплатно", "волонтер")):
        return True
    return False


def is_helper_message(text: str):
    if not text:
        return False, "empty", []
    text_lower = text.lower()
    if is_unpaid_vacancy(text):
        return False, "unpaid", []
    if any(p in text_lower for p in ("организатор", "координатор свад", "свадеб")) and "супервайзер" not in text_lower:
        if not any(w in text_lower for w in ("хелпер", "хэлпер", "промоутер", "аниматор", "грузчик", "промо")):
            return False, "excluded_organizer", []
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
    try:
        async with _parser_lock:
            stats = _new_stats("manual")
            LAST_DEBUG_STATS = stats
            if _realtime_client and _realtime_client.is_connected():
                logger.info("🔍 Ручная проверка через shared Telethon client")
                result = await _scan_all_chats(
                    _realtime_client, limit_per_chat=limit_per_chat, stats=stats,
                )
                stats["finished_at"] = _iso_now()
                return result

            logger.error(
                f"❌ {PARSER_LABEL} offline — ручная проверка пропущена (не создаём второй user_session)"
            )
            stats["finished_at"] = _iso_now()
            return [], []
    except Exception as e:
        logger.error(f"❌ Критическая ошибка в парсере: {e}", exc_info=True)
        if LAST_DEBUG_STATS.get("started_at"):
            LAST_DEBUG_STATS["finished_at"] = _iso_now()
        return [], []

async def run_parser():
    orders, closed_data = await get_new_messages()
    return orders, closed_data

async def test_filter(chat_link: str, limit: int = 30):
    try:
        client = await create_authorized_client()
    except SessionNotConfiguredError as e:
        logger.error(str(e))
        return
    try:
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
